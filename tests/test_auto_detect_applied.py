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
    # current file at new head_sha — the target line was changed (user applied)
    gl.get_file_at_sha.return_value = (
        "def foo():\n"
        "    pass\n"
        "    return 2\n"  # was "    return 1"
        "print('after')\n"
    )

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
    # file unchanged — still has the original target line
    gl.get_file_at_sha.return_value = (
        "def foo():\n"
        "    pass\n"
        "    return 1\n"  # unchanged
        "print('after')\n"
    )

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
