"""T20: 运维演练——只验证现有机制，不新增运行时行为.

覆盖 `docs/BACKUP_RESTORE.md` 里两条可自动化的步骤：

* `VACUUM INTO` 在线备份 → 复制到新 data_dir → 重新打开（迁移幂等）→ 行数一致；
* 孤儿 `running` run（`started_at` 早于 TTL）被现有 `sweep_orphaned_runs` 标终态。
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from reviewagent.telemetry.models import ReviewRun
from reviewagent.telemetry.store import Store


def test_sqlite_online_backup_restore_roundtrip(tmp_path):
    src = tmp_path / "data" / "telemetry.db"
    store = Store(src)
    store.insert_run(
        ReviewRun(project_id=34, mr_iid=1, command="improve", triggered_by="webhook")
    )
    backup = tmp_path / "backup.db"
    with sqlite3.connect(src) as conn:
        conn.execute("VACUUM INTO ?", (str(backup),))
    restored = tmp_path / "restored" / "telemetry.db"
    restored.parent.mkdir()
    restored.write_bytes(backup.read_bytes())
    Store(restored)  # 迁移幂等：重复打开不报错
    with sqlite3.connect(restored) as conn:
        assert conn.execute("SELECT COUNT(*) FROM review_runs").fetchone()[0] == 1


def test_vacuum_into_snapshot_excludes_rows_written_later(tmp_path):
    """备份点语义：VACUUM INTO 只包含快照时刻已提交的数据（演练按备份时刻对账）."""
    src = tmp_path / "data" / "telemetry.db"
    store = Store(src)
    first = store.insert_run(
        ReviewRun(project_id=34, mr_iid=1, command="improve", triggered_by="webhook")
    )
    backup = tmp_path / "backup.db"
    with sqlite3.connect(src) as conn:
        conn.execute("VACUUM INTO ?", (str(backup),))
    store.insert_run(
        ReviewRun(project_id=34, mr_iid=2, command="describe", triggered_by="webhook")
    )
    with sqlite3.connect(backup) as conn:
        rows = conn.execute("SELECT id FROM review_runs").fetchall()
    assert rows == [(first,)]


def test_orphaned_running_run_is_swept_to_terminal_state(tmp_path):
    """演练「恢复后确认无 running run」：TTL 之外的孤儿由现有 sweep 收尾."""
    store = Store(tmp_path / "data" / "telemetry.db")
    stale = datetime.now(UTC) - timedelta(hours=2)
    stale_id = store.insert_run(
        ReviewRun(
            project_id=34,
            mr_iid=305,
            command="improve",
            triggered_by="webhook",
            started_at=stale,
            status="running",
        )
    )
    fresh_id = store.insert_run(
        ReviewRun(
            project_id=34,
            mr_iid=306,
            command="improve",
            triggered_by="webhook",
            status="running",
        )
    )

    # TTL = rq_worker_timeout + 300s 的现有约定；这里用远小于 2h 的阈值演练。
    assert store.sweep_orphaned_runs(threshold_seconds=300) == 1

    with store._conn() as conn:
        statuses = {
            row["id"]: row["status"]
            for row in conn.execute(
                "SELECT id, status FROM review_runs WHERE id IN (?, ?)",
                (stale_id, fresh_id),
            )
        }
    assert statuses[stale_id] == "failed"
    assert statuses[fresh_id] == "running"
