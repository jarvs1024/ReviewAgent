"""Unit tests for process_dismiss (/dismiss command flow)."""
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


def _seed(s, *, note_id, state="open", head_sha="abc", line=10):
    s.record_suggestion(
        project_id=34, mr_iid=263, note_id=note_id,
        file_path="a.py", target_line=line, target_line_end=line,
        existing_code="old\n", improved_code="new\n",
        header="h", label="l",
        fingerprint=f"fp-{note_id}", cohort_key=f"cohort-{note_id}",
        severity="medium", severity_source="rule", head_sha=head_sha,
    )
    if state != "open":
        s.update_suggestion_state(note_id, state, actor_username="prev-actor")


def test_dismiss_happy_path(tmp_telemetry):
    """正常 dismiss: state→dismissed, GitLab resolve, audit, overview 刷新."""
    from reviewagent.commands.suggestion_actions import process_dismiss

    s = _get_store()
    _seed(s, note_id="d-1")

    gl = MagicMock()
    gl.resolve_discussion.return_value = True
    gl.reply_to_discussion.return_value = 99999
    gl.list_mr_notes.return_value = []
    gl.post_mr_comment.return_value = 99999

    with patch("reviewagent.commands.suggestion_actions.GitLabClient", return_value=gl), \
         patch("reviewagent.commands._common.GitLabClient", return_value=gl), \
         patch("reviewagent.commands.suggestion_actions.get_store", return_value=s):
        result = process_dismiss(
            project_id=34, mr_iid=263,
            suggestion_note_id="d-1",
            actor_username="root",
            reason="误报, 不是 bug",
        )

    assert result["action"] == "dismissed"
    sug = s.get_suggestion_by_note_id("d-1")
    assert sug["state"] == "dismissed"
    assert sug["dismissed_by"] == "root"
    assert sug["dismissed_reason"] == "误报, 不是 bug"
    gl.resolve_discussion.assert_called_once()
    gl.reply_to_discussion.assert_called_once()
    # Reply 应该提到 reason
    reply_text = gl.reply_to_discussion.call_args[0][3]
    assert "误报" in reply_text


def test_dismiss_skips_already_dismissed(tmp_telemetry):
    """已 dismissed 的 suggestion, 再 dismiss → skip, 不重复 resolve."""
    from reviewagent.commands.suggestion_actions import process_dismiss

    s = _get_store()
    _seed(s, note_id="d-2", state="dismissed")

    gl = MagicMock()
    gl.resolve_discussion.return_value = True
    gl.reply_to_discussion.return_value = 99999

    with patch("reviewagent.commands.suggestion_actions.GitLabClient", return_value=gl), \
         patch("reviewagent.commands._common.GitLabClient", return_value=gl), \
         patch("reviewagent.commands.suggestion_actions.get_store", return_value=s):
        result = process_dismiss(
            project_id=34, mr_iid=263,
            suggestion_note_id="d-2",
            actor_username="root", reason="再 dismiss",
        )

    assert result["action"] == "dismiss-skipped"
    gl.resolve_discussion.assert_not_called()
    gl.reply_to_discussion.assert_not_called()


def test_dismiss_skips_already_applied(tmp_telemetry):
    """已 applied 的 suggestion, dismiss → skip (避免覆盖采纳状态)."""
    from reviewagent.commands.suggestion_actions import process_dismiss

    s = _get_store()
    _seed(s, note_id="d-3", state="applied")

    gl = MagicMock()
    gl.resolve_discussion.return_value = True

    with patch("reviewagent.commands.suggestion_actions.GitLabClient", return_value=gl), \
         patch("reviewagent.commands._common.GitLabClient", return_value=gl), \
         patch("reviewagent.commands.suggestion_actions.get_store", return_value=s):
        result = process_dismiss(
            project_id=34, mr_iid=263,
            suggestion_note_id="d-3",
            actor_username="root", reason="x",
        )

    assert result["action"] == "dismiss-skipped"
    sug = s.get_suggestion_by_note_id("d-3")
    # state 不应被覆盖为 dismissed
    assert sug["state"] == "applied"


def test_dismiss_handles_resolve_failure(tmp_telemetry):
    """GitLab resolve 失败 → action=dismiss-failed, state 不变."""
    from reviewagent.commands.suggestion_actions import process_dismiss

    s = _get_store()
    _seed(s, note_id="d-4")

    gl = MagicMock()
    gl.resolve_discussion.return_value = False

    with patch("reviewagent.commands.suggestion_actions.GitLabClient", return_value=gl), \
         patch("reviewagent.commands._common.GitLabClient", return_value=gl), \
         patch("reviewagent.commands.suggestion_actions.get_store", return_value=s):
        result = process_dismiss(
            project_id=34, mr_iid=263,
            suggestion_note_id="d-4",
            actor_username="root", reason="x",
        )

    assert result["action"] == "dismiss-failed"
    sug = s.get_suggestion_by_note_id("d-4")
    assert sug["state"] == "open"


def test_dismiss_writes_audit_action(tmp_telemetry):
    """/dismiss 必须写入 suggestion_actions 表 (审计可追溯)."""
    from reviewagent.commands.suggestion_actions import process_dismiss

    s = _get_store()
    _seed(s, note_id="d-5")

    gl = MagicMock()
    gl.resolve_discussion.return_value = True
    gl.list_mr_notes.return_value = []
    gl.post_mr_comment.return_value = 99999

    with patch("reviewagent.commands.suggestion_actions.GitLabClient", return_value=gl), \
         patch("reviewagent.commands._common.GitLabClient", return_value=gl), \
         patch("reviewagent.commands.suggestion_actions.get_store", return_value=s):
        process_dismiss(
            project_id=34, mr_iid=263,
            suggestion_note_id="d-5",
            actor_username="root", reason="测试 reason",
        )

    actions = [a for a in s.list_suggestion_actions(project_id=34, mr_iid=263, limit=100)
               if a.get("suggestion_note_id") == "d-5"]
    assert len(actions) >= 1
    dismiss_action = next((a for a in actions if a["action"] == "dismissed"), None)
    assert dismiss_action is not None
    assert dismiss_action["actor_username"] == "root"
    assert dismiss_action["reason"] == "测试 reason"


def _get_store():
    from reviewagent.telemetry.store import get_store
    return get_store()
