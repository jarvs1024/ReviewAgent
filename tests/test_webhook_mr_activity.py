"""webhook → mr_activity 落库: 真实 MR 作者 + no_more_review 独立列."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def tmp_store(monkeypatch):
    tmp = TemporaryDirectory()
    path = Path(tmp.name) / "telemetry.db"
    from reviewagent.config import config as _cfg
    monkeypatch.setattr(
        type(_cfg), "sqlite_path",
        property(lambda self: path),
        raising=False,
    )
    from reviewagent.telemetry import store as st
    st._store = None
    # get_mr 默认会调 GitLab 补 web_url — 单测里跳过网络
    monkeypatch.setattr("reviewagent.telemetry.store._enrich_web_url", lambda row: row)
    yield path
    st._store = None
    tmp.cleanup()


def _mr_payload(*, action="merge", user=None) -> dict:
    return {
        "object_kind": "merge_request",
        "user": user or {"name": "Merger", "username": "merger"},
        "project": {"id": 34},
        "object_attributes": {
            "iid": 500,
            "action": action,
            "title": "feat: x",
            "state": "merged" if action == "merge" else "opened",
            "source_branch": "feature",
            "target_branch": "main",
            "created_at": "2026-01-01 00:00:00 UTC",
            "updated_at": "2026-01-02 00:00:00 UTC",
            "merged_at": "2026-01-02 00:00:00 UTC" if action == "merge" else None,
            "diff_refs": {"head_sha": "abc"},
        },
    }


def _api_mr() -> dict:
    return {
        "project_id": 34,
        "iid": 500,
        "title": "feat: x",
        "author": {"name": "Alice", "username": "alice"},
        "source_branch": "feature",
        "target_branch": "main",
        "state": "merged",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
        "merged_at": "2026-01-02T00:00:00Z",
    }


def test_upsert_mr_uses_api_author_for_merge_event(tmp_store, monkeypatch):
    from reviewagent.gitlab.client import client as gl
    from reviewagent.webhook import router as router_mod
    from reviewagent.webhook.parsers import MRHookPayload

    monkeypatch.setattr(gl, "get_mr", MagicMock(return_value=_api_mr()))
    payload = _mr_payload(action="merge")
    router_mod._upsert_mr_from_hook(payload, MRHookPayload.from_payload(payload))

    from reviewagent.telemetry.store import get_store
    row = get_store().get_mr(34, 500)
    assert row["author_username"] == "Alice@alice"
    assert row["state"] == "merged"


def test_upsert_mr_open_keeps_payload_author_without_api(tmp_store, monkeypatch):
    from reviewagent.gitlab.client import client as gl
    from reviewagent.webhook import router as router_mod
    from reviewagent.webhook.parsers import MRHookPayload

    spy = MagicMock(side_effect=AssertionError("open 事件不该查 API"))
    monkeypatch.setattr(gl, "get_mr", spy)
    payload = _mr_payload(
        action="open", user={"name": "Alice", "username": "alice"},
    )
    router_mod._upsert_mr_from_hook(payload, MRHookPayload.from_payload(payload))

    from reviewagent.telemetry.store import get_store
    row = get_store().get_mr(34, 500)
    assert row["author_username"] == "Alice@alice"
    spy.assert_not_called()


def test_upsert_mr_falls_back_to_payload_author_when_api_fails(tmp_store, monkeypatch):
    from reviewagent.gitlab.client import client as gl
    from reviewagent.webhook import router as router_mod
    from reviewagent.webhook.parsers import MRHookPayload

    monkeypatch.setattr(gl, "get_mr", MagicMock(side_effect=RuntimeError("boom")))
    payload = _mr_payload(action="merge")
    router_mod._upsert_mr_from_hook(payload, MRHookPayload.from_payload(payload))

    from reviewagent.telemetry.store import get_store
    assert get_store().get_mr(34, 500)["author_username"] == "Merger@merger"


def test_claim_no_more_review_notice_is_idempotent(tmp_store):
    from reviewagent.telemetry.models import MRRecord
    from reviewagent.telemetry.store import get_store

    store = get_store()
    store.upsert_mr(MRRecord(34, 501, "t", "alice", "f", "m", "opened"))
    assert store.claim_no_more_review_notice(34, 501) is True
    assert store.claim_no_more_review_notice(34, 501) is False
    assert store.get_mr(34, 501)["no_more_review"] == 1

    store.reset_no_more_review_notice(34, 501)
    assert store.claim_no_more_review_notice(34, 501) is True


def test_migration_moves_legacy_title_marker_to_column(tmp_store):
    conn = sqlite3.connect(tmp_store)
    conn.executescript(
        """
        CREATE TABLE mr_activity (
            project_id INTEGER NOT NULL,
            mr_iid INTEGER NOT NULL,
            title TEXT, author_username TEXT, author_sticky TEXT,
            source_branch TEXT, target_branch TEXT, state TEXT,
            created_at TIMESTAMP, updated_at TIMESTAMP, merged_at TIMESTAMP,
            description_generated INTEGER DEFAULT 0,
            last_review_at TIMESTAMP, last_activity_at TIMESTAMP,
            PRIMARY KEY (project_id, mr_iid)
        );
        INSERT INTO mr_activity (project_id, mr_iid, title, state)
        VALUES (34, 502, 'feat: x [no_more_review]', 'opened');
        """
    )
    conn.commit()
    conn.close()

    from reviewagent.telemetry.store import Store

    store = Store(tmp_store)
    row = store.get_mr(34, 502)
    assert row["title"] == "feat: x"
    assert row["no_more_review"] == 1


@pytest.mark.asyncio
async def test_max_review_notice_posts_once_and_does_not_pollute_title(
    tmp_store, monkeypatch,
):
    from reviewagent.webhook import router as router_mod

    fake_locks = MagicMock()
    fake_locks.is_bot.return_value = False
    fake_locks.check_diff_head_changed.return_value = True
    fake_locks.should_skip_max_review_calls.return_value = (True, 3)
    fake_locks.should_skip_cooldown.return_value = False
    monkeypatch.setattr(router_mod, "locks", fake_locks)
    monkeypatch.setattr(router_mod, "_mr_record_from_api", lambda mr: None)
    monkeypatch.setattr(
        "reviewagent.commands.suggestion_actions.auto_detect_applied",
        lambda **kwargs: {"scanned": 0, "applied": 0},
    )

    gl_instance = MagicMock()
    monkeypatch.setattr(
        "reviewagent.gitlab.client.GitLabClient", MagicMock(return_value=gl_instance),
    )

    payload = _mr_payload(action="update")
    first = await router_mod._handle_code_change(payload, "merge_request", MagicMock())
    second = await router_mod._handle_code_change(payload, "merge_request", MagicMock())

    assert first["status"] == "skipped"
    assert second["status"] == "skipped"
    assert gl_instance.post_mr_comment.call_count == 1

    from reviewagent.telemetry.store import get_store
    row = get_store().get_mr(34, 500)
    assert row["title"] == "feat: x"
    assert "no_more_review" not in (row["title"] or "")
    assert row["no_more_review"] == 1
