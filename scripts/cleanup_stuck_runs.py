#!/usr/bin/env python3
"""清理 telemetry 中卡在 running 状态的 run 记录.

背景:
- RQ worker 处理 reviewagent command 时, 会先 INSERT 一条 status=running 的 run 记录,
  命令完成时 UPDATE 为 success/failed.
- 但 worker 被 kill -9 / 机器重启 / OOM 时, 没有机会更新 status,
  run 记录会永远停在 running, 前端 UI 一直显示"运行中".

用法:
    python scripts/cleanup_stuck_runs.py --dry-run     # 只列出
    python scripts/cleanup_stuck_runs.py               # 实际更新
    python scripts/cleanup_stuck_runs.py --age-minutes 30

行为:
- 找出所有 status='running' 且 started_at 超过 N 分钟的 run
- 默认 N=10 分钟 (单次 review 跑完一般 < 5 分钟, 超过 10 分钟几乎肯定死了)
- 标记为 status='interrupted', error='worker_killed_during_run'
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys
from pathlib import Path

# 让脚本可作为 `python scripts/cleanup_stuck_runs.py` 直接跑
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 在 import reviewagent.config 之前, 自动 source .env (兼容带行尾注释的 KEY=VALUE # comment)
_env_path = Path(__file__).resolve().parents[1] / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        # 去掉尾部 `# 注释` (但保留值中的 #)
        kv, _, comment = line.partition("  #")
        if not comment:
            kv, _, comment = line.partition("\t#")
        kv = kv if not comment else kv
        k, _, v = kv.partition("=")
        v = v.strip().strip('"').strip("'")
        # 处理 list/dict 形式 (用 re 简单剥离尾随注释)
        os.environ.setdefault(k.strip(), v)

from reviewagent.telemetry.store import get_store


def main() -> int:
    parser = argparse.ArgumentParser(description="清理卡在 running 的 run 记录")
    parser.add_argument(
        "--age-minutes", type=int, default=10,
        help="超过该分钟数才认为是卡住 (默认 10)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="只列出要清理的记录, 不实际更新",
    )
    args = parser.parse_args()

    store = get_store()
    runs = store.list_runs(limit=500, status="running")
    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(minutes=args.age_minutes)

    stuck: list[dict] = []
    for r in runs:
        started_at_str = r.get("started_at") or ""
        if not started_at_str:
            continue
        try:
            started_at = dt.datetime.fromisoformat(started_at_str.replace("Z", "+00:00"))
        except ValueError:
            continue
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=dt.timezone.utc)
        if started_at < cutoff and not r.get("finished_at"):
            stuck.append(r)

    print(f"扫描: status=running 共 {len(runs)} 条, 卡住 ({args.age_minutes}min+) {len(stuck)} 条")
    for r in stuck:
        print(
            f"  #{r['id']} {r['command']:8s} mr={r['project_id']}/{r['mr_iid']} "
            f"started={r['started_at']} triggered_by={r.get('triggered_by')}"
        )

    if args.dry_run:
        print("\n--dry-run, 未实际更新")
        return 0

    for r in stuck:
        store.finish_run(
            r["id"],
            status="interrupted",
            error="worker_killed_during_run",
            duration_ms=0,
        )
        print(f"  -> #{r['id']} 已标记为 interrupted")
    print(f"\n清理完成, 共更新 {len(stuck)} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
