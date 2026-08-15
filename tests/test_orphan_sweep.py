"""Test orphaned running-run sweep — 根治 work-horse 被强杀后残留的假 running.

Why:
  - 命令内 `_common.py` 的 finally 安全网在 work-horse 被 SIGKILL/OOM 或 RQ 硬
    SIGTERM 时不执行, 导致 review_runs 永久停在 running (MR 304/305 实测卡死).
  - 根治必须靠进程外 sweep: 由下一个存活进程 (新 job / worker 启动) 清理.
  - 两层: per-MR 精确 (拿链锁后, 无阈值) + 全局时间阈值兜底.
"""
from __future__ import annotations

import datetime
import os
import pathlib
import tempfile
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


def _seed_run(s, *, project_id=34, mr_iid=305, command="improve",
              started_at=None, status="running"):
    from reviewagent.telemetry.models import ReviewRun

    run = ReviewRun(
        project_id=project_id, mr_iid=mr_iid, command=command,
        triggered_by="webhook", actor_username="tester",
        started_at=started_at or datetime.datetime.now(datetime.timezone.utc),
        status=status,
    )
    return s.insert_run(run)


def test_sweep_orphaned_runs_respects_threshold(tmp_telemetry):
    """超阈值的 running 被标 failed; 未超阈值的保留."""
    from reviewagent.telemetry.store import get_store

    s = get_store()
    now = datetime.datetime.now(datetime.timezone.utc)
    old = now - datetime.timedelta(hours=1)
    recent = now - datetime.timedelta(seconds=10)
    id_old = _seed_run(s, mr_iid=305, started_at=old)
    id_recent = _seed_run(s, mr_iid=306, started_at=recent)

    n = s.sweep_orphaned_runs(threshold_seconds=300)
    assert n == 1, n

    with s._conn() as c:
        st_old = c.execute("SELECT status FROM review_runs WHERE id=?", (id_old,)).fetchone()["status"]
        st_recent = c.execute("SELECT status FROM review_runs WHERE id=?", (id_recent,)).fetchone()["status"]
    assert st_old == "failed"
    assert st_recent == "running"


def test_sweep_orphaned_runs_for_mr_is_precise(tmp_telemetry):
    """per-MR 精确清理: 清掉该 mr 全部 running, 不影响其他 mr (无阈值误判)."""
    from reviewagent.telemetry.store import get_store

    s = get_store()
    now = datetime.datetime.now(datetime.timezone.utc)
    a = _seed_run(s, project_id=34, mr_iid=305, started_at=now - datetime.timedelta(minutes=5))
    b = _seed_run(s, project_id=34, mr_iid=305, started_at=now - datetime.timedelta(minutes=4))
    c = _seed_run(s, project_id=34, mr_iid=304, started_at=now - datetime.timedelta(minutes=5))

    n = s.sweep_orphaned_runs_for_mr(project_id=34, mr_iid=305)
    assert n == 2, n

    with s._conn() as conn:
        st_a = conn.execute("SELECT status FROM review_runs WHERE id=?", (a,)).fetchone()["status"]
        st_b = conn.execute("SELECT status FROM review_runs WHERE id=?", (b,)).fetchone()["status"]
        st_c = conn.execute("SELECT status FROM review_runs WHERE id=?", (c,)).fetchone()["status"]
    assert st_a == "failed"
    assert st_b == "failed"
    assert st_c == "running"  # 其他 mr 不受影响


def test_sweep_does_not_touch_finished(tmp_telemetry):
    """已终态 (success/failed) 的记录不应被 sweep 改动."""
    from reviewagent.telemetry.store import get_store

    s = get_store()
    now = datetime.datetime.now(datetime.timezone.utc)
    rid = _seed_run(
        s, mr_iid=305, started_at=now - datetime.timedelta(hours=2), status="success",
    )
    n = s.sweep_orphaned_runs(threshold_seconds=300)
    assert n == 0
    with s._conn() as c:
        st = c.execute("SELECT status FROM review_runs WHERE id=?", (rid,)).fetchone()["status"]
    assert st == "success"


def test_per_mr_sweep_marks_duration_and_error(tmp_telemetry):
    """per-MR sweep 应回填 finished_at/duration_ms/error, 消除 running 假象."""
    from reviewagent.telemetry.store import get_store

    s = get_store()
    now = datetime.datetime.now(datetime.timezone.utc)
    rid = _seed_run(s, mr_iid=305, started_at=now - datetime.timedelta(minutes=7))
    s.sweep_orphaned_runs_for_mr(project_id=34, mr_iid=305)

    with s._conn() as c:
        row = c.execute(
            "SELECT status, finished_at, duration_ms, error FROM review_runs WHERE id=?",
            (rid,),
        ).fetchone()
    assert row["status"] == "failed"
    assert row["finished_at"] is not None
    assert row["duration_ms"] >= 0
    assert "orphan" in (row["error"] or "").lower()
