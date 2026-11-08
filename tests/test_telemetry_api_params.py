"""读接口参数语义 — /runs total, /metrics/rules, /metrics/authors, /dismissals, /mrs."""
from __future__ import annotations

import os
import pathlib
import sqlite3
import tempfile
from datetime import datetime, timezone

import pytest


@pytest.fixture
def client(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    from reviewagent.config import config as _cfg
    monkeypatch.setattr(
        type(_cfg), "sqlite_path",
        property(lambda self: pathlib.Path(path)),
        raising=False,
    )
    import reviewagent.telemetry.store as st_mod
    st_mod._store = None
    monkeypatch.setattr(st_mod, "_enrich_web_url", lambda row: row)

    _seed(path)

    from fastapi.testclient import TestClient
    from reviewagent.main import app
    yield TestClient(app)

    st_mod._store = None
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _seed(path: str) -> None:
    from reviewagent.telemetry.models import MRRecord, ReviewRun
    from reviewagent.telemetry.store import get_store

    s = get_store()
    # MR1: 2026-01 创建, 2026-06 更新 (updated_at 很新)
    s.upsert_mr(MRRecord(
        34, 1, "one", "Alice@alice", "f", "m", "opened",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    ))
    s.upsert_mr(MRRecord(
        34, 2, "two", "Bob@bob", "g", "m", "opened",
        created_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 3, 2, tzinfo=timezone.utc),
    ))
    for actor, mr_iid in (("Alice@alice", 1), ("Alice@alice", 1), ("Bob@bob", 2)):
        run_id = s.insert_run(ReviewRun(
            34, mr_iid, "improve", "webhook", actor_username=actor,
            started_at=datetime(2026, 3, 10, tzinfo=timezone.utc),
        ))
        s.finish_run(run_id, status="success")

    for note_id, line, rule_key in (
        ("n-old", 1, "R-ERR"),
        ("n-new", 2, "R-CI"),
        ("n-open", 3, "R-ERR"),
    ):
        s.record_suggestion(
            project_id=34, mr_iid=1, note_id=note_id,
            file_path="a.py", target_line=line, header="h",
            severity="high", head_sha="sha1", rule_keys=[rule_key],
        )
    s.update_suggestion_state(
        "n-old", "dismissed", actor_username="alice", dismissed_reason="误报",
    )
    s.update_suggestion_state(
        "n-new", "dismissed", actor_username="bob", dismissed_reason="不需要",
    )

    conn = sqlite3.connect(path)
    conn.execute(
        "UPDATE suggestions SET dismissed_at=? WHERE note_id=?",
        ("2026-03-05 00:00:00", "n-old"),
    )
    conn.execute(
        "UPDATE suggestions SET dismissed_at=? WHERE note_id=?",
        ("2026-03-20 00:00:00", "n-new"),
    )
    conn.commit()
    conn.close()


def test_runs_total_is_not_page_size(client):
    res = client.get("/api/v1/telemetry/runs", params={"limit": 1})
    assert res.status_code == 200
    body = res.json()
    assert len(body["runs"]) == 1
    assert body["total"] == 3


def test_metrics_rules_returns_real_rule_keys(client):
    res = client.get("/api/v1/telemetry/metrics/rules")
    assert res.status_code == 200
    rules = {r["rule_key"]: r["count"] for r in res.json()["rules"]}
    assert rules.get("R-ERR") == 2
    assert rules.get("R-CI") == 1


def test_metrics_authors_reports_mrs_and_runs(client):
    res = client.get("/api/v1/telemetry/metrics/authors")
    assert res.status_code == 200
    by_author = {a["author"]: a for a in res.json()["authors"]}
    assert by_author["Alice@alice"]["mrs"] == 1
    assert by_author["Alice@alice"]["runs"] == 2
    assert by_author["Bob@bob"]["mrs"] == 1
    assert by_author["Bob@bob"]["runs"] == 1


def test_dismissals_rule_key_filters_by_suggestion_rule_keys(client):
    res = client.get("/api/v1/telemetry/dismissals", params={"rule_key": "R-ERR"})
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 1
    assert [d["note_id"] for d in body["dismissals"]] == ["n-old"]


def test_dismissals_supports_until_offset_and_total(client):
    res = client.get(
        "/api/v1/telemetry/dismissals",
        params={"until": "2026-03-10", "limit": 10},
    )
    body = res.json()
    assert body["total"] == 1
    assert [d["note_id"] for d in body["dismissals"]] == ["n-old"]

    page1 = client.get(
        "/api/v1/telemetry/dismissals", params={"limit": 1, "offset": 0},
    ).json()
    page2 = client.get(
        "/api/v1/telemetry/dismissals", params={"limit": 1, "offset": 1},
    ).json()
    assert page1["total"] == 2 == page2["total"]
    assert page1["dismissals"][0]["note_id"] != page2["dismissals"][0]["note_id"]


def test_mrs_window_uses_created_at(client):
    res = client.get(
        "/api/v1/telemetry/mrs",
        params={"since": "2026-02-01", "until": "2026-04-01"},
    )
    assert res.status_code == 200
    body = res.json()
    assert [mr["mr_iid"] for mr in body["mrs"]] == [2]
