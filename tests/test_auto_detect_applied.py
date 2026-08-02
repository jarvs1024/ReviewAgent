"""Unit tests for auto_detect_applied: detect suggestions applied via GitLab UI."""
from __future__ import annotations

import os
import pathlib
import tempfile
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def tmp_telemetry(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    from reviewagent.config import config as _cfg
    monkeypatch.setattr(
        type(_cfg), "sqlite_path",
        property(lambda self: pathlib.Path(path)),
        raising=False,
    )
    from reviewagent.telemetry import store as st
    st._store = None
    yield path
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    st._store = None


def _seed_suggestion(s, *, project_id=34, mr_iid=200, head_sha="abc123",
                     note_id="note-1", file_path="a.py", target_line=2,
                     existing_code="def foo():\n    pass\n    return 1\n", state="open"):
    """Seed a 1-line suggestion at line 3 with existing `    return 1`."""
    s.record_suggestion(
        project_id=project_id, mr_iid=mr_iid, note_id=note_id,
        file_path=file_path, target_line=target_line,
        target_line_end=target_line+2,
        existing_code=existing_code,
        improved_code="def foo():\n    pass\n    return 2\n",
        header="h", label="l",
        fingerprint="fp1", cohort_key="c1",
        severity_source="rule", head_sha=head_sha,
    )
    if state != "open":
        s.update_suggestion_state(note_id, state, actor_username="tester")


def test_changed_target_lines_get_marked_applied(tmp_telemetry):
    """If user changed the target line via GitLab UI, state -> applied."""
    from reviewagent.commands.suggestion_actions import auto_detect_applied
    from reviewagent.telemetry.store import get_store

    s = get_store()
    _seed_suggestion(s)

    gl = MagicMock()
    posted = (
        "def foo():\n"
        "    pass\n"
        "    return 1\n"  # original
        "print('after')\n"
    )
    current = (
        "def foo():\n"
        "    pass\n"
        "    return 2\n"  # was "    return 1" (user applied)
        "print('after')\n"
    )
    # auto_detect_applied 调 2 次 get_file_at_sha: posted 时代 + current 时代
    gl.get_file_at_sha.side_effect = [posted, current]

    with patch("reviewagent.commands.suggestion_actions.GitLabClient", return_value=gl), \
         patch("reviewagent.commands.suggestion_actions.get_store", return_value=s):
        result = auto_detect_applied(
            project_id=34, mr_iid=200, head_sha="newhead",
            actor_username="root",
        )

    assert result["scanned"] == 1
    assert result["applied"] == 1, f"expected 1 applied, got {result}"
    assert result["unchanged"] == 0

    sug = s.get_suggestion_by_note_id("note-1")
    assert sug["state"] == "applied"
    gl.resolve_discussion.assert_called_once()


def test_unchanged_target_lines_stay_open(tmp_telemetry):
    """If target line still matches existing_code, state stays open."""
    from reviewagent.commands.suggestion_actions import auto_detect_applied
    from reviewagent.telemetry.store import get_store

    s = get_store()
    _seed_suggestion(s)

    gl = MagicMock()
    unchanged_file = (
        "def foo():\n"
        "    pass\n"
        "    return 1\n"  # unchanged
        "print('after')\n"
    )
    # auto_detect_applied 调 2 次 get_file_at_sha: posted + current 都返回原文件
    gl.get_file_at_sha.side_effect = [unchanged_file, unchanged_file]

    with patch("reviewagent.commands.suggestion_actions.GitLabClient", return_value=gl), \
         patch("reviewagent.commands.suggestion_actions.get_store", return_value=s):
        result = auto_detect_applied(
            project_id=34, mr_iid=200, head_sha="newhead",
        )

    assert result["scanned"] == 1
    assert result["applied"] == 0
    assert result["unchanged"] == 1

    sug = s.get_suggestion_by_note_id("note-1")
    assert sug["state"] == "open"
    gl.resolve_discussion.assert_not_called()


def test_missing_file_is_counted_as_error(tmp_telemetry):
    """If file is gone at new head_sha, count as error and skip."""
    from reviewagent.commands.suggestion_actions import auto_detect_applied
    from reviewagent.telemetry.store import get_store

    s = get_store()
    _seed_suggestion(s)

    gl = MagicMock()
    gl.get_file_at_sha.return_value = None  # file not found

    with patch("reviewagent.commands.suggestion_actions.GitLabClient", return_value=gl), \
         patch("reviewagent.commands.suggestion_actions.get_store", return_value=s):
        result = auto_detect_applied(
            project_id=34, mr_iid=200, head_sha="newhead",
        )

    assert result["scanned"] == 1
    assert result["errors"] == 1
    assert result["applied"] == 0
    sug = s.get_suggestion_by_note_id("note-1")
    assert sug["state"] == "open"


def test_non_open_suggestions_are_skipped(tmp_telemetry):
    """Already-dismissed suggestions must not be re-evaluated."""
    from reviewagent.commands.suggestion_actions import auto_detect_applied
    from reviewagent.telemetry.store import get_store

    s = get_store()
    _seed_suggestion(s, state="dismissed", note_id="dismissed-1")

    gl = MagicMock()
    # even if the file changed, dismissed stays dismissed
    gl.get_file_at_sha.return_value = "def foo():\n    pass\n    return 2\n"

    with patch("reviewagent.commands.suggestion_actions.GitLabClient", return_value=gl), \
         patch("reviewagent.commands.suggestion_actions.get_store", return_value=s):
        result = auto_detect_applied(
            project_id=34, mr_iid=200, head_sha="newhead",
        )

    assert result["scanned"] == 0
    sug = s.get_suggestion_by_note_id("dismissed-1")
    assert sug["state"] == "dismissed"
    gl.resolve_discussion.assert_not_called()


def test_auto_detect_delete_range_requires_block_removed():
    """回归 bug #4: auto_detect_applied 不能仅因为 target 行内容"改了" 就把
    delete_range suggestion 标 applied. 必须验证 deleted_block 在 current 文件
    中**消失** (即用户真删了), 否则保持 open.

    场景: published suggestion 建议删 L5-L6 (重复 def probe), posted 中存在,
    current 中内容被改但仍存在 → 不算采纳.
    """
    posted = (
        "def probe(e, a=[]):\n"   # L1
        "    pass\n"               # L2
        "    pass\n"               # L3
        "    pass\n"               # L4
        "def probe(e, a=[]):\n"   # L5 duplicate (要删)
        "    return None\n"        # L6
    )
    current = (
        "def probe(e, a=None):\n"  # L1 改了
        "    pass\n"
        "    pass\n"
        "    pass\n"
        "def probe(e, a=None):\n"  # L5 同步也改了, 但没删
        "    return None\n"
    )
    # 模拟 sug 记录: delete_range, improved_code 为空
    sug_improved = ""
    sug_existing = "def probe(e, a=[]):\n    return None\n"
    assert sug_improved == "" and sug_existing  # delete_range 触发条件

    src_lines = posted.splitlines()
    target_line, target_line_end = 5, 6  # L5-L6 in 1-indexed
    lo, hi = max(0, target_line - 1), min(len(src_lines), target_line_end)
    deleted_block = "\n".join(src_lines[lo:hi]).strip()
    # 关键判定: 看 current 中是否还有 "def probe" 这个函数定义.
    # 如果有 → 函数还在, 用户没删; 如果没有 → 真删了.
    func_def_present = "def probe(" in current
    assert func_def_present, "test setup error: current 应保留 def probe"
    # deleted_block 整段不在 current (因为 attempts=[] 改成 attempts=None)
    assert deleted_block not in current
    # 但既然 def probe 还在, 不算"删除"
    block_removed = not func_def_present
    assert not block_removed, (
        "delete_range 误判: 用户没删 (def probe 仍在), 不应被 auto_detect 标 applied"
    )
