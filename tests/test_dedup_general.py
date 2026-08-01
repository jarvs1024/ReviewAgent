"""Regression: general-action (shrinking) suggestions must also pass through
the cross-run dedup guard at MR 155 line 12 / line 19.

Before the fix, the dedup block lived inside `if decision["action"] == "post"`,
so `action == "general"` (删行建议) would short-circuit to a second
post_mr_discussion call without checking whether the same (file, line, head_sha)
was already published. This test drives ImproveCommand._publish with a faked
"general" decision and asserts it is skipped when a prior row exists.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import types
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def tmp_telemetry(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    # Config is a frozen dataclass; patch the Store class's sqlite_path
    # resolution directly by swapping the property at the class level.
    from reviewagent.config import config as _cfg
    monkeypatch.setattr(
        type(_cfg), "sqlite_path",
        property(lambda self: __import__("pathlib").Path(path)),
        raising=False,
    )
    from reviewagent.telemetry import store as st
    st._store = None  # reset cached singleton
    yield path
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    st._store = None


def _make_raw():
    return {
        "file": "tests/test_error_handling.py",
        "line": 12,
        "new_line": 12,
        "header": "静默吞异常",
        "label": "exception",
        "rationale": "bare except swallows real bugs",
        "severity": "high",
        "improved_code": "except (ValueError,):\n    raise",
        "existing_code": "except Exception:\n    pass",
        "rule_keys": ["R-ERR"],
    }


def test_general_action_skipped_when_already_published(tmp_telemetry):
    """Same (file, line, head_sha) already published → general suggestion must
    be skipped, NOT posted again."""
    from reviewagent.commands.improve import ImproveCommand
    from reviewagent.telemetry.store import get_store

    head_sha = "deadbeef" * 5

    # Pre-seed a prior suggestion at the same (file, line, head_sha)
    s = get_store()
    s.record_suggestion(
        project_id=34, mr_iid=155, note_id="seed-note",
        file_path="tests/test_error_handling.py", target_line=12,
        target_line_end=13,
        existing_code="except Exception:\n    pass",
        improved_code="except (ValueError,):\n    raise",
        header="静默吞异常", label="exception",
        fingerprint="abc123", cohort_key="cohort1",
        severity_source="rule",
        head_sha=head_sha,
    )
    # head_sha column required; update it directly
    conn = sqlite3.connect(tmp_telemetry)
    conn.execute("UPDATE suggestions SET head_sha=?, state='open' WHERE note_id='seed-note'", (head_sha,))
    conn.commit()
    conn.close()

    # Build a stub command instance
    cmd = ImproveCommand.__new__(ImproveCommand)
    cmd.project_id = 34
    cmd.mr_iid = 155
    cmd.gitlab = MagicMock()
    cmd.gitlab.post_mr_discussion.return_value = "should-not-be-called"
    cmd.HELP_TEXT_FOOTER = ""

    def _normalise(raw):
        return {
            "file": raw["file"], "new_line": raw["line"],
            "header": raw["header"], "label": raw["label"],
            "rationale": raw["rationale"], "severity": raw["severity"],
            "improved_code": raw["improved_code"],
            "body": "**body**",
        }

    cmd._normalise_suggestion = _normalise  # type: ignore
    cmd._validate_suggestion = lambda **kw: {  # type: ignore
        "action": "general",
        "new_line": kw["start_line"],
        "reason": "shrinking suggestion (5 -> 4 lines)",
    }
    cmd._get_mr_head_sha = lambda: head_sha  # type: ignore

    cmd._diff_line_map = lambda: {}  # type: ignore
    with patch("reviewagent.telemetry.store.get_store", return_value=s):
        result = cmd._publish({"summary_md": "", "suggestions": [_make_raw()]})

    assert result["inline_posted"] == 0
    assert result["inline_skipped"] == 1
    cmd.gitlab.post_mr_discussion.assert_not_called()


def test_post_action_still_skipped_at_line(tmp_telemetry):
    """Sanity: the original `post` path still gets deduped by ±2 tolerance."""
    from reviewagent.commands.improve import ImproveCommand
    from reviewagent.telemetry.store import get_store

    head_sha = "cafebabe" * 5
    s = get_store()
    s.record_suggestion(
        project_id=34, mr_iid=155, note_id="seed-post",
        file_path="tests/test_metrics.py", target_line=10,
        target_line_end=10,
        existing_code="x = 1",
        improved_code="x = 2",
        header="h", label="l",
        fingerprint="fp1", cohort_key="c1",
        severity_source="rule",
        head_sha=head_sha,
    )
    conn = sqlite3.connect(tmp_telemetry)
    conn.execute("UPDATE suggestions SET head_sha=?, state='open' WHERE note_id='seed-post'", (head_sha,))
    conn.commit()
    conn.close()

    cmd = ImproveCommand.__new__(ImproveCommand)
    cmd.project_id = 34
    cmd.mr_iid = 155
    cmd.gitlab = MagicMock()
    cmd.gitlab.post_mr_discussion.return_value = "x"
    cmd.HELP_TEXT_FOOTER = ""

    raw = {
        "file": "tests/test_metrics.py", "line": 11,  # ±1 from 10
        "header": "h", "label": "l",
        "rationale": "r", "severity": "medium",
        "improved_code": "x = 2",
        "existing_code": "x = 1",
        "rule_keys": [],
    }
    cmd._normalise_suggestion = lambda r: {
        "file": r["file"], "new_line": r["line"],
        "header": r["header"], "label": r["label"],
        "rationale": r["rationale"], "severity": r["severity"],
        "improved_code": r["improved_code"],
        "body": "**b**",
    }
    cmd._validate_suggestion = lambda **kw: {"action": "post", "new_line": kw["start_line"]}
    cmd._get_mr_head_sha = lambda: head_sha

    cmd._diff_line_map = lambda: {}  # type: ignore
    with patch("reviewagent.telemetry.store.get_store", return_value=s):
        result = cmd._publish({"summary_md": "", "suggestions": [raw]})

    assert result["inline_posted"] == 0
    assert result["inline_skipped"] == 1
    cmd.gitlab.post_mr_discussion.assert_not_called()
