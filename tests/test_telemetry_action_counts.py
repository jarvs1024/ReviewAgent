"""action_counts 统计口径: 校验失败的 /adopt 不虚增 adopted."""
from __future__ import annotations

import os
import pathlib
import tempfile

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


def _seed_suggestion(s, *, note_id, state):
    s.record_suggestion(
        project_id=7, mr_iid=9, note_id=note_id,
        file_path="a.py", target_line=3, target_line_end=3,
        existing_code="x = 1\n", improved_code="x = 2\n",
        header="h", label="l",
        fingerprint=f"fp-{note_id}", cohort_key=f"cohort-{note_id}",
        severity="medium", severity_source="rule", head_sha="sha-posted",
    )
    if state != "open":
        s.update_suggestion_state(note_id, state, actor_username="root")


def _seed_action(s, *, note_id, action, validation_status):
    s.record_suggestion_action(
        project_id=7, mr_iid=9, suggestion_note_id=note_id,
        file_path="a.py", target_line=3,
        action=action, actor_username="root", reason="r",
        validation_status=validation_status, head_sha_posted="sha-posted",
    )


def test_action_counts_exclude_rejected_adoptions(tmp_telemetry):
    from reviewagent.telemetry.store import get_store

    s = get_store()
    _seed_suggestion(s, note_id="ok-adopt", state="applied")
    _seed_suggestion(s, note_id="ui-apply", state="applied")
    _seed_suggestion(s, note_id="rejected", state="open")
    _seed_suggestion(s, note_id="on-dismissed", state="dismissed")

    _seed_action(s, note_id="ok-adopt", action="adopted", validation_status="ok")
    _seed_action(s, note_id="ui-apply", action="adopted", validation_status="gitlab-ui-apply")
    _seed_action(s, note_id="rejected", action="adopted", validation_status="target-unchanged")
    _seed_action(s, note_id="rejected", action="adopted", validation_status="same-head")
    _seed_action(s, note_id="on-dismissed", action="adopted", validation_status="already-dismissed")
    _seed_action(s, note_id="on-dismissed", action="dismissed", validation_status="ok")

    assert s.suggestion_stats(7, 9)["action_counts"] == {
        "adopted": 2, "dismissed": 1,
    }
    metrics = s.suggestion_metrics(project_id=7)
    assert metrics["action_counts"] == {"adopted": 2, "dismissed": 1}

    # 原始审计行必须保留 (用户意图 + 校验结果可追溯)
    raw = s.list_suggestion_actions(project_id=7, mr_iid=9, limit=100)
    assert len(raw) == 6
    assert sum(1 for r in raw if r["action"] == "adopted") == 5


def test_repeat_adopt_on_applied_still_counts(tmp_telemetry):
    from reviewagent.telemetry.store import get_store

    s = get_store()
    _seed_suggestion(s, note_id="applied", state="applied")
    _seed_action(s, note_id="applied", action="adopted", validation_status="ok")
    _seed_action(s, note_id="applied", action="adopted", validation_status="already-applied")

    assert s.suggestion_stats(7, 9)["action_counts"] == {"adopted": 2}
