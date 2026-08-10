"""Unit tests for publish_overview pre-reconcile behavior.

Background:
    GitLab 17.5 occasionally does NOT send a "marked this discussion as resolved"
    note webhook when user clicks ✓ in the UI. Before this fix, that meant:
        - DB state stuck at 'open' forever
        - 检视汇总 (persisted overview note) didn't refresh
    Fix: publish_overview now scans all open suggestions via GitLab
    is_discussion_resolved API and marks any that GitLab has resolved but DB
    still has as 'open'. Silent helper avoids recursion with publish_overview.
"""
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


def _seed_open_suggestion(s, *, note_id="note-x", line=10, severity="medium",
                           head_sha="abc123"):
    s.record_suggestion(
        project_id=34, mr_iid=263, note_id=note_id,
        file_path="a.py", target_line=line, target_line_end=line,
        existing_code="old\n", improved_code="new\n",
        header="h", label="l",
        fingerprint=f"fp-{note_id}", cohort_key=f"cohort-{note_id}",
        severity=severity, severity_source="rule", head_sha=head_sha,
    )


def _list_actions_for_note(s, *, note_id, project_id=34, mr_iid=263):
    """Filter suggestion_actions for a specific note_id."""
    out = []
    for a in s.list_suggestion_actions(project_id=project_id, mr_iid=mr_iid, limit=500):
        if a.get("suggestion_note_id") == note_id:
            out.append(a)
    return out


# ---------- _scan_and_mark_resolved_silent ----------

def test_silent_marks_orphan_resolved(tmp_telemetry):
    """Silent helper marks DB→resolved when GitLab already resolved but DB open."""
    from reviewagent.commands.suggestion_actions import _scan_and_mark_resolved_silent
    from reviewagent.telemetry.store import get_store

    s = get_store()
    _seed_open_suggestion(s, note_id="orphan-1", line=10)
    _seed_open_suggestion(s, note_id="orphan-2", line=20)

    gl = MagicMock()
    def is_resolved(project_id, mr_iid, note_id):
        return note_id == "orphan-1"
    gl.is_discussion_resolved.side_effect = is_resolved

    with patch("reviewagent.commands.suggestion_actions.GitLabClient", return_value=gl), \
         patch("reviewagent.commands.suggestion_actions.get_store", return_value=s):
        result = _scan_and_mark_resolved_silent(
            project_id=34, mr_iid=263,
            actor_username="tester",
            adoption_source="test_source",
            reason="unit test",
            validation_status="test_status",
        )

    assert result["scanned"] == 2
    assert result["updated"] == 1
    assert result["note_ids"] == ["orphan-1"]

    sug1 = s.get_suggestion_by_note_id("orphan-1")
    sug2 = s.get_suggestion_by_note_id("orphan-2")
    assert sug1["state"] == "resolved"
    assert sug1["resolution_source"] == "test_source"
    assert sug2["state"] == "open"


def test_silent_does_not_call_publish_overview(tmp_telemetry):
    """Silent helper 不会触发 publish_overview (避免递归)."""
    from reviewagent.commands.suggestion_actions import _scan_and_mark_resolved_silent
    from reviewagent.telemetry.store import get_store

    s = get_store()
    _seed_open_suggestion(s, note_id="x")

    gl = MagicMock()
    gl.is_discussion_resolved.return_value = True

    with patch("reviewagent.commands.suggestion_actions.GitLabClient", return_value=gl), \
         patch("reviewagent.commands.suggestion_actions.get_store", return_value=s), \
         patch("reviewagent.commands.suggestion_actions.publish_overview") as mock_pub:
        _scan_and_mark_resolved_silent(
            project_id=34, mr_iid=263,
            actor_username="t",
            adoption_source="t", reason="t", validation_status="t",
        )

    mock_pub.assert_not_called()


def test_silent_handles_is_discussion_resolved_failure(tmp_telemetry):
    """is_discussion_resolved 抛异常时, silent helper 不崩, 跳过该条."""
    from reviewagent.commands.suggestion_actions import _scan_and_mark_resolved_silent
    from reviewagent.telemetry.store import get_store

    s = get_store()
    _seed_open_suggestion(s, note_id="ok-note")
    _seed_open_suggestion(s, note_id="fail-note")

    gl = MagicMock()
    def side_effect(project_id, mr_iid, note_id):
        if note_id == "fail-note":
            raise RuntimeError("GitLab API timeout")
        return False
    gl.is_discussion_resolved.side_effect = side_effect

    with patch("reviewagent.commands.suggestion_actions.GitLabClient", return_value=gl), \
         patch("reviewagent.commands.suggestion_actions.get_store", return_value=s):
        result = _scan_and_mark_resolved_silent(
            project_id=34, mr_iid=263,
            actor_username="t",
            adoption_source="t", reason="t", validation_status="t",
        )

    assert result["scanned"] == 2
    assert result["updated"] == 0
    for n in ("ok-note", "fail-note"):
        assert s.get_suggestion_by_note_id(n)["state"] == "open"


def test_silent_records_action_log(tmp_telemetry):
    """Silent helper 写入 suggestion_actions 表 (审计可追溯)."""
    from reviewagent.commands.suggestion_actions import _scan_and_mark_resolved_silent
    from reviewagent.telemetry.store import get_store

    s = get_store()
    _seed_open_suggestion(s, note_id="audited")

    gl = MagicMock()
    gl.is_discussion_resolved.return_value = True

    with patch("reviewagent.commands.suggestion_actions.GitLabClient", return_value=gl), \
         patch("reviewagent.commands.suggestion_actions.get_store", return_value=s):
        _scan_and_mark_resolved_silent(
            project_id=34, mr_iid=263,
            actor_username="pre_reconcile_actor",
            adoption_source="publish_overview_reconcile",
            reason="pre-reconcile audit reason",
            validation_status="pre_reconcile_status",
        )

    actions = _list_actions_for_note(s, note_id="audited")
    assert len(actions) == 1
    a = actions[0]
    assert a["action"] == "resolved"
    assert a["actor_username"] == "pre_reconcile_actor"
    assert a["adoption_source"] == "publish_overview_reconcile"


# ---------- publish_overview pre-reconcile integration ----------

def test_publish_overview_catches_orphan(tmp_telemetry):
    """publish_overview 调用前, 即使 sync_resolved_from_gitlab 没跑过,
    也会自动 catch-up 已经在 GitLab 被 resolved 的 suggestion."""
    # patch _common 内部的 get_store import 来源 (reviewagent.telemetry.store)
    from reviewagent.commands._common import publish_overview
    from reviewagent.telemetry.store import get_store

    s = get_store()
    _seed_open_suggestion(s, note_id="orphan-pre", line=15)

    gl = MagicMock()
    gl.is_discussion_resolved.return_value = True

    # 没有现存 overview comment (避免 update 路径, 测 post 路径)
    gl.list_mr_notes.return_value = []
    gl.post_mr_comment.return_value = 99999

    # _scan_and_mark_resolved_silent 内部用 `from reviewagent.gitlab.client import GitLabClient`
    # 然后 `gl = GitLabClient()`, 必须在该模块 patch. _common 内的 lazy import 把这个 helper
    # 拉过来用, 所以 helper 内部的 gl 变量来自 suggestion_actions 模块的 GitLabClient 引用.
    with patch("reviewagent.commands.suggestion_actions.GitLabClient", return_value=gl), \
         patch("reviewagent.commands._common.GitLabClient", return_value=gl), \
         patch("reviewagent.commands.suggestion_actions.get_store", return_value=s):
        result = publish_overview(
            project_id=34, mr_iid=263,
            inline_posted_count=0,
            run_late_detect=False,
        )

    # DB 应该被 pre-reconcile catch-up
    sug = s.get_suggestion_by_note_id("orphan-pre")
    assert sug["state"] == "resolved"
    assert sug["resolution_source"] == "publish_overview_reconcile"
    # overview 创建成功
    assert result == 99999


def test_publish_overview_preserves_existing_path(tmp_telemetry):
    """publish_overview 仍然生成正常的 body (不破坏现有 summary)."""
    from reviewagent.commands._common import publish_overview
    from reviewagent.telemetry.store import get_store

    s = get_store()
    _seed_open_suggestion(s, note_id="normal-1", severity="high")
    _seed_open_suggestion(s, note_id="normal-2", severity="medium")

    gl = MagicMock()
    gl.is_discussion_resolved.return_value = False  # 都没 resolved
    gl.list_mr_notes.return_value = []
    posted_body = None

    def capture_post(project_id, mr_iid, body):
        nonlocal posted_body
        posted_body = body
        return 99999
    gl.post_mr_comment.side_effect = capture_post

    with patch("reviewagent.commands.suggestion_actions.GitLabClient", return_value=gl), \
         patch("reviewagent.commands._common.GitLabClient", return_value=gl), \
         patch("reviewagent.commands.suggestion_actions.get_store", return_value=s):
        result = publish_overview(
            project_id=34, mr_iid=263,
            inline_posted_count=0,
            run_late_detect=False,
        )

    assert result == 99999
    assert posted_body is not None
    assert "检视汇总" in posted_body
    assert "总建议数" in posted_body


def test_publish_overview_does_not_recurse():
    """silent helper 函数体内不含 publish_overview 调用 (避免递归).

    silent helper 是 publish_overview 自己调用的, 自身绝不能再调 publish_overview
    否则进入无限递归. 静态检查函数体源码 (跳过 docstring) 验证.
    """
    from reviewagent.commands import suggestion_actions as sa
    import inspect
    fn = sa._scan_and_mark_resolved_silent
    src = inspect.getsource(fn)
    # 拆掉 docstring 再查
    import textwrap
    import ast
    tree = ast.parse(textwrap.dedent(src))
    func_body = tree.body[0]
    body_src = ast.unparse(func_body)
    # 去掉 docstring
    if (func_body.body and isinstance(func_body.body[0], ast.Expr)
            and isinstance(func_body.body[0].value, ast.Constant)):
        func_body.body = func_body.body[1:]
        body_src = ast.unparse(func_body)
    assert "publish_overview" not in body_src, (
        f"silent helper 函数体内含 publish_overview — 会导致递归: {body_src}"
    )
