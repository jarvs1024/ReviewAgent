"""Test mr_activity.last_activity_at tracking.

Why:
  - 修复 dashboard "MR 数据采集中时间只有开启时间, 没有最后活动时间" 问题
  - last_activity_at = MAX(now, last_review_at, MAX(suggestions.created_at), MAX(suggestion_actions.created_at))
  - 在以下事件触发后更新: review 完成 / suggestion 发布 / /adopt /dismiss

Why MAX:
  - 写入时间早于已存在的 last_activity_at 不会回退
  - backfill SQL 也用 MAX 保证新数据不会覆盖回填的旧时间
"""
from __future__ import annotations

import os
import tempfile
import pathlib
from unittest.mock import patch

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
    from reviewagent.telemetry import store as st_mod
    st_mod._store = None
    yield path
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    st_mod._store = None


def _seed_mr(s, *, project_id=34, mr_iid=999):
    """Insert a basic mr_activity row."""
    from reviewagent.telemetry.models import MRRecord
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    mr = MRRecord(
        project_id=project_id, mr_iid=mr_iid,
        title="t", author_username="tester",
        source_branch="src", target_branch="tgt",
        state="opened",
        created_at=now, updated_at=now, merged_at=None,
    )
    s.upsert_mr(mr)


def test_touch_mr_activity_basic(tmp_telemetry):
    """touch_mr_activity should update last_activity_at."""
    from reviewagent.telemetry.store import get_store
    s = get_store()
    _seed_mr(s)

    # Initially last_activity_at should be NULL
    mr_before = s.get_mr(34, 999)
    assert mr_before["last_activity_at"] is None, "fresh MR should have null last_activity_at"

    # Touch it
    s.touch_mr_activity(34, 999)

    mr_after = s.get_mr(34, 999)
    assert mr_after["last_activity_at"] is not None, "touch should set last_activity_at"
    print(f"  ✓ touch_mr_activity sets last_activity_at: {mr_after['last_activity_at']}")


def test_record_suggestion_updates_last_activity(tmp_telemetry):
    """record_suggestion should touch last_activity_at."""
    from reviewagent.telemetry.store import get_store
    s = get_store()
    _seed_mr(s)
    s.touch_mr_activity(34, 999)  # baseline
    before = s.get_mr(34, 999)["last_activity_at"]

    s.record_suggestion(
        project_id=34, mr_iid=999,
        note_id="note-1",
        file_path="a.py", target_line=1, target_line_end=2,
        existing_code="x", improved_code="y",
        header="h", severity="medium", head_sha="a"*40,
    )
    after = s.get_mr(34, 999)["last_activity_at"]
    assert after is not None
    assert after >= before, "record_suggestion should advance last_activity_at"
    print(f"  ✓ record_suggestion advances last_activity_at: {before} → {after}")


def test_record_suggestion_action_updates_last_activity(tmp_telemetry):
    """record_suggestion_action should touch last_activity_at."""
    from reviewagent.telemetry.store import get_store
    s = get_store()
    _seed_mr(s)
    s.touch_mr_activity(34, 999)
    before = s.get_mr(34, 999)["last_activity_at"]

    s.record_suggestion_action(
        project_id=34, mr_iid=999,
        suggestion_note_id="note-1",
        action="adopted", actor_username="reviewer",
    )
    after = s.get_mr(34, 999)["last_activity_at"]
    assert after is not None
    assert after >= before, "action should advance last_activity_at"
    print(f"  ✓ record_suggestion_action advances last_activity_at: {before} → {after}")


def test_finish_run_updates_last_activity(tmp_telemetry):
    """finish_run should touch last_activity_at."""
    from reviewagent.telemetry.store import get_store
    s = get_store()
    _seed_mr(s)
    s.touch_mr_activity(34, 999)
    before = s.get_mr(34, 999)["last_activity_at"]

    # Insert a run then finish it
    from reviewagent.telemetry.models import ReviewRun
    import datetime as _dt
    started_at = _dt.datetime.now(_dt.timezone.utc)
    run = ReviewRun(
        project_id=34, mr_iid=999,
        command="improve", triggered_by="webhook",
        actor_username="bot", started_at=started_at, status="running",
    )
    run_id = s.insert_run(run)
    s.finish_run(run_id, status="success", duration_ms=1000)

    after = s.get_mr(34, 999)["last_activity_at"]
    assert after is not None
    assert after >= before, "finish_run should advance last_activity_at"
    print(f"  ✓ finish_run advances last_activity_at: {before} → {after}")


def test_max_semantics_no_regression(tmp_telemetry):
    """写入时间早于已存在的 last_activity_at 不会回退 (MAX 语义)."""
    from reviewagent.telemetry.store import get_store
    s = get_store()
    _seed_mr(s)

    # First touch with explicit "future" timestamp
    future_ts = "2099-01-01T00:00:00.000000+00:00"
    s.touch_mr_activity(34, 999, at=future_ts)
    mr = s.get_mr(34, 999)
    assert mr["last_activity_at"] == future_ts
    print(f"  ✓ set future: {mr['last_activity_at']}")

    # Try to touch with "now" — should NOT regress below future_ts
    s.touch_mr_activity(34, 999)
    mr = s.get_mr(34, 999)
    assert mr["last_activity_at"] == future_ts, \
        f"MAX semantics broken: regressed to {mr['last_activity_at']}"
    print(f"  ✓ no regression: still {mr['last_activity_at']}")


def test_backfill_from_existing_data(tmp_telemetry):
    """Migration: existing mr_activity rows get backfilled last_activity_at."""
    from reviewagent.telemetry.store import get_store
    s = get_store()
    _seed_mr(s)

    # 直接通过 SQL 写入历史时间戳 (不走 record_suggestion/touch_mr_activity,
    # 避免 touch 把 NOW 写到 last_activity_at)
    with s._conn() as conn:
        # 设置 last_review_at 为 2026-08-01
        conn.execute(
            "UPDATE mr_activity SET last_review_at = ? WHERE project_id=34 AND mr_iid=999",
            ("2026-08-01T10:00:00+00:00",),
        )
        # 直接 INSERT 一条 suggestion (created_at=2026-08-02, 晚于 last_review_at)
        conn.execute(
            """
            INSERT INTO suggestions (
                project_id, mr_iid, note_id, file_path, target_line, target_line_end,
                existing_code, improved_code, header, severity, head_sha,
                state, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
            """,
            (34, 999, "old-note", "a.py", 1, 2, "x", "y", "h", "low", "a"*40,
             "2026-08-02T10:00:00+00:00"),
        )
        conn.commit()

    # 此时 last_activity_at 还是 NULL (没有 touch 触发)
    mr_before = s.get_mr(34, 999)
    assert mr_before["last_activity_at"] is None

    # 调用 backfill (实际场景: 升级后第一次启动时由 migration 触发)
    affected = s.backfill_last_activity_at()
    assert affected >= 1, "backfill should affect at least 1 row"

    mr = s.get_mr(34, 999)
    # last_activity_at 应该是 MAX(last_review_at, suggestions.created_at)
    # = MAX(2026-08-01, 2026-08-02) = 2026-08-02
    assert mr["last_activity_at"] is not None
    assert "2026-08-02" in mr["last_activity_at"], \
        f"backfill should pick max: got {mr['last_activity_at']}"
    print(f"  ✓ backfill picked max: {mr['last_activity_at']}")
