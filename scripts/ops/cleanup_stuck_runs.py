"""清理 telemetry DB 中 stuck runs (status='running' 但 finished_at IS NULL).

产生原因:
  - worker horse 进程被 SIGKILL (如 restart_local.sh, OOM) — finish_run 没执行
  - verify_e2e 被 pkill 而 in-flight chain 还在排队

用法:
  set -a; source .env; set +a
  .venv/bin/python scripts/ops/cleanup_stuck_runs.py [--dry-run] [--since-hours 1]

默认标记 status='failed' + error 含 'worker terminated by SIGKILL'.
"""
from __future__ import annotations

import argparse
import os

# 在脚本里手动 source env (跟 verify_e2e.py 风格一致)


def _ensure_env() -> None:
    for k in ("GITLAB_URL", "REVIEWAGENT_DATA_DIR"):
        if not os.environ.get(k):
            raise RuntimeError(f"missing required env var: {k}; source .env first")


def main() -> int:
    _ensure_env()
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true",
                   help="只列出 stuck runs, 不修改")
    p.add_argument("--since-hours", type=float, default=0.5,
                   help="只清理 started_at 早于 now - N 小时的 (避免误杀正在跑的)")
    args = p.parse_args()

    from reviewagent.telemetry.store import get_store
    from reviewagent.telemetry.events import emit_run_finished
    from datetime import datetime, timezone, timedelta

    s = get_store()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=args.since_hours)).isoformat()

    with s._conn() as conn:
        rows = conn.execute(
            """
            SELECT id, project_id, mr_iid, command, started_at
            FROM review_runs
            WHERE status='running' AND finished_at IS NULL
              AND started_at < ?
            ORDER BY started_at
            """,
            (cutoff,),
        ).fetchall()

    print(f"Found {len(rows)} stuck runs older than {args.since_hours}h:")
    for r in rows:
        d = dict(r)
        print(f"  id={d['id']} project={d['project_id']} mr={d['mr_iid']} cmd={d['command']} started={d['started_at'][:19]}")

    if args.dry_run or not rows:
        return 0

    for r in rows:
        d = dict(r)
        emit_run_finished(
            d['id'],
            status='failed',
            error='worker terminated by SIGKILL during restart/abort cycle (stuck run cleanup)',
            duration_ms=0,
        )
    print(f"\nFixed {len(rows)} stuck runs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
