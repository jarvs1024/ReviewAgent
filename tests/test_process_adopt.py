"""Unit tests for process_adopt (/adopt command flow)."""
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


def _seed(s, *, note_id, state="open", head_sha="abc12345", line=2):
    """Seed a suggestion targeting line 2 (the `pass` / `return 1` line in the
    2-line file content below)."""
    s.record_suggestion(
        project_id=34, mr_iid=263, note_id=note_id,
        file_path="a.py", target_line=line, target_line_end=line,
        existing_code="def foo():\n    pass\n",
        improved_code="def foo():\n    return 1\n",
        header="h", label="l",
        fingerprint=f"fp-{note_id}", cohort_key=f"cohort-{note_id}",
        severity="medium", severity_source="rule", head_sha=head_sha,
    )
    if state != "open":
        s.update_suggestion_state(note_id, state, actor_username="prev-actor")


def _setup_gl(gl):
    """Configure GitLab mock for successful adopt path."""
    gl.get_mr_diff_refs.return_value = {"head_sha": "newsha999"}
    gl.get_file_at_sha.side_effect = [
        # posted_content (head_sha)
        "def foo():\n    pass\n",
        # current_content (head_sha_current) — target line changed
        "def foo():\n    return 1\n",
    ]
    gl.resolve_discussion.return_value = True
    gl.reply_to_discussion.return_value = 99999
    gl.list_mr_notes.return_value = []
    gl.post_mr_comment.return_value = 99999


def test_adopt_happy_path(tmp_telemetry):
    """目标行被改, /adopt 走通: state→applied, audit, overview 刷新."""
    from reviewagent.commands.suggestion_actions import process_adopt

    s = _get_store()
    _seed(s, note_id="a-1")

    gl = MagicMock()
    _setup_gl(gl)

    with patch("reviewagent.commands.suggestion_actions.GitLabClient", return_value=gl), \
         patch("reviewagent.commands._common.GitLabClient", return_value=gl), \
         patch("reviewagent.commands.suggestion_actions.get_store", return_value=s), \
         patch("reviewagent.commands.suggestion_actions._maybe_enqueue_reimprove", return_value=None):
        result = process_adopt(
            project_id=34, mr_iid=263,
            suggestion_note_id="a-1",
            actor_username="root", reason="手动改了",
        )

    assert result["action"] == "adopted"
    sug = s.get_suggestion_by_note_id("a-1")
    assert sug["state"] == "applied"
    assert sug["adoption_source"] == "adopt_command"
    gl.resolve_discussion.assert_called_once()


def test_adopt_rejects_same_head_sha(tmp_telemetry):
    """head_sha 没变 (用户没 push) → validation failed."""
    from reviewagent.commands.suggestion_actions import process_adopt

    s = _get_store()
    _seed(s, note_id="a-2", head_sha="samehead")

    gl = MagicMock()
    gl.get_mr_diff_refs.return_value = {"head_sha": "samehead"}  # 没变
    gl.reply_to_discussion.return_value = 99999

    with patch("reviewagent.commands.suggestion_actions.GitLabClient", return_value=gl), \
         patch("reviewagent.commands._common.GitLabClient", return_value=gl), \
         patch("reviewagent.commands.suggestion_actions.get_store", return_value=s), \
         patch("reviewagent.commands.suggestion_actions._maybe_enqueue_reimprove", return_value=None):
        result = process_adopt(
            project_id=34, mr_iid=263,
            suggestion_note_id="a-2",
            actor_username="root", reason="x",
        )

    assert result["action"] == "adopt-validation-failed"
    assert result["validation"] == "same-head"
    sug = s.get_suggestion_by_note_id("a-2")
    assert sug["state"] == "open"  # 没采纳


def test_adopt_rejects_unchanged_target_lines(tmp_telemetry):
    """head_sha 变了但目标行没改 → validation failed."""
    from reviewagent.commands.suggestion_actions import process_adopt

    s = _get_store()
    _seed(s, note_id="a-3")

    gl = MagicMock()
    gl.get_mr_diff_refs.return_value = {"head_sha": "newsha"}
    # 整文件改了, 但目标行 (line 2 "pass") 没动
    posted = "def foo():\n    pass\n"
    current = "def foo():\n    pass\n# add comment\n"
    gl.get_file_at_sha.side_effect = [posted, current]
    gl.reply_to_discussion.return_value = 99999

    with patch("reviewagent.commands.suggestion_actions.GitLabClient", return_value=gl), \
         patch("reviewagent.commands._common.GitLabClient", return_value=gl), \
         patch("reviewagent.commands.suggestion_actions.get_store", return_value=s), \
         patch("reviewagent.commands.suggestion_actions._maybe_enqueue_reimprove", return_value=None):
        result = process_adopt(
            project_id=34, mr_iid=263,
            suggestion_note_id="a-3",
            actor_username="root", reason="x",
        )

    assert result["action"] == "adopt-validation-failed"
    assert result["validation"] == "target-unchanged"


def test_adopt_skips_already_applied_with_reply(tmp_telemetry):
    """已 applied 的 suggestion, /adopt → skip + reply 反馈 (不覆盖状态)."""
    from reviewagent.commands.suggestion_actions import process_adopt

    s = _get_store()
    _seed(s, note_id="a-4", state="applied")

    gl = MagicMock()
    gl.get_mr_diff_refs.return_value = {"head_sha": "newsha"}
    gl.resolve_discussion.return_value = True
    gl.reply_to_discussion.return_value = 99999
    gl.list_mr_notes.return_value = []
    gl.post_mr_comment.return_value = 99999

    with patch("reviewagent.commands.suggestion_actions.GitLabClient", return_value=gl), \
         patch("reviewagent.commands._common.GitLabClient", return_value=gl), \
         patch("reviewagent.commands.suggestion_actions.get_store", return_value=s), \
         patch("reviewagent.commands.suggestion_actions._maybe_enqueue_reimprove", return_value=None):
        result = process_adopt(
            project_id=34, mr_iid=263,
            suggestion_note_id="a-4",
            actor_username="root", reason="又来一次",
        )

    assert result["action"] == "adopt-skipped"
    assert "state=applied" in result["reason"]
    # state 仍是 applied (未被覆盖回 open 等)
    sug = s.get_suggestion_by_note_id("a-4")
    assert sug["state"] == "applied"
    # reply 必须明确告知用户当前状态 (回复文案可能用各种说法)
    reply = gl.reply_to_discussion.call_args[0][3]
    assert any(kw in reply for kw in ("自动检测", "applied", "已采纳", "已关闭", "ℹ"))


def test_adopt_handles_missing_metadata(tmp_telemetry):
    """Suggestion 缺 metadata (file_path/line/head_sha) → adopt-failed."""
    from reviewagent.commands.suggestion_actions import process_adopt

    s = _get_store()
    s.record_suggestion(
        project_id=34, mr_iid=263, note_id="a-5",
        file_path="", target_line=0,  # 缺 metadata
        existing_code="", improved_code="",
        header="h", label="l",
        fingerprint="fp5", cohort_key="c5",
        severity="medium", severity_source="rule", head_sha="",
    )

    gl = MagicMock()
    with patch("reviewagent.commands.suggestion_actions.GitLabClient", return_value=gl), \
         patch("reviewagent.commands._common.GitLabClient", return_value=gl), \
         patch("reviewagent.commands.suggestion_actions.get_store", return_value=s), \
         patch("reviewagent.commands.suggestion_actions._maybe_enqueue_reimprove", return_value=None):
        result = process_adopt(
            project_id=34, mr_iid=263,
            suggestion_note_id="a-5",
            actor_username="root", reason="x",
        )

    assert result["action"] == "adopt-failed"
    assert "metadata" in result["reason"]


def test_adopt_no_record_allows_unchecked(tmp_telemetry):
    """Suggestion 记录不存在 (历史 MR / 跨 project / 人工发 note) → adopted-unchecked."""
    from reviewagent.commands.suggestion_actions import process_adopt

    s = _get_store()
    # 故意不 seed — DB 里没这条

    gl = MagicMock()
    gl.resolve_discussion.return_value = True
    gl.reply_to_discussion.return_value = 99999

    with patch("reviewagent.commands.suggestion_actions.GitLabClient", return_value=gl), \
         patch("reviewagent.commands._common.GitLabClient", return_value=gl), \
         patch("reviewagent.commands.suggestion_actions.get_store", return_value=s), \
         patch("reviewagent.commands.suggestion_actions._maybe_enqueue_reimprove", return_value=None):
        result = process_adopt(
            project_id=34, mr_iid=263,
            suggestion_note_id="never-existed",
            actor_username="root", reason="x",
        )

    assert result["action"] == "adopted-unchecked"
    gl.resolve_discussion.assert_called_once()
    gl.reply_to_discussion.assert_called_once()


def test_adopt_writes_audit_action_with_reason(tmp_telemetry):
    """/adopt 必须写 audit, 含 validation_status='ok' 和 reason."""
    from reviewagent.commands.suggestion_actions import process_adopt

    s = _get_store()
    _seed(s, note_id="a-6")

    gl = MagicMock()
    _setup_gl(gl)

    with patch("reviewagent.commands.suggestion_actions.GitLabClient", return_value=gl), \
         patch("reviewagent.commands._common.GitLabClient", return_value=gl), \
         patch("reviewagent.commands.suggestion_actions.get_store", return_value=s), \
         patch("reviewagent.commands.suggestion_actions._maybe_enqueue_reimprove", return_value=None):
        process_adopt(
            project_id=34, mr_iid=263,
            suggestion_note_id="a-6",
            actor_username="root", reason="修复方案 X",
        )

    actions = [a for a in s.list_suggestion_actions(project_id=34, mr_iid=263, limit=100)
               if a.get("suggestion_note_id") == "a-6"]
    adopt_action = next((a for a in actions if a["action"] == "adopted"), None)
    assert adopt_action is not None
    assert adopt_action["validation_status"] == "ok"
    assert adopt_action["reason"] == "修复方案 X"
    assert adopt_action["adoption_source"] == "adopt_command"


def _get_store():
    from reviewagent.telemetry.store import get_store
    return get_store()
