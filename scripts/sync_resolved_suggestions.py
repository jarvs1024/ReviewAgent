#!/usr/bin/env python3
"""Sync GitLab resolved → DB resolved (一次性 / 周期性 reconciliation).

适用场景:
    用户在 GitLab UI 直接点 ✓ 解决主题 (没改代码) 时, 没有 push webhook 触发
    auto_detect_applied, DB 里 state 永远停在 open (MR 247 e50f4c0d4d4e 实测).
    一次性 / 周期性跑这个脚本扫所有 / 指定 MR, 把"GitLab resolved 但 DB open"
    的 suggestion 标 resolved + resolution_source='gitlab_resolve'.

Usage:
    python3 scripts/sync_resolved_suggestions.py --project-id 34 --mr-iid 247
    python3 scripts/sync_resolved_suggestions.py --all
    python3 scripts/sync_resolved_suggestions.py --all --dry-run
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--project-id", type=int, default=0)
    p.add_argument("--mr-iid", type=int, default=0)
    p.add_argument("--all", action="store_true",
                   help="扫所有有 open suggestion 的 MR")
    p.add_argument("--dry-run", action="store_true",
                   help="只打印, 不改 DB")
    args = p.parse_args()

    if not args.all and (not args.project_id or not args.mr_iid):
        p.error("需要 --project-id + --mr-iid, 或 --all")

    from reviewagent.commands.suggestion_actions import sync_resolved_from_gitlab
    from reviewagent.telemetry.store import get_store

    store = get_store()

    if args.all:
        # 扫所有有 state=open suggestion 的 MR
        with store._conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT project_id, mr_iid FROM suggestions "
                "WHERE state='open' ORDER BY project_id, mr_iid"
            ).fetchall()
        if not rows:
            print("无 open suggestions, 无需 sync")
            return 0
        print(f"待 sync 的 MR: {len(rows)}")
        total_updated = 0
        for r in rows:
            pid, miid = r["project_id"], r["mr_iid"]
            if args.dry_run:
                with store._conn() as conn:
                    n_open = conn.execute(
                        "SELECT COUNT(*) AS n FROM suggestions "
                        "WHERE project_id=? AND mr_iid=? AND state='open'",
                        (pid, miid),
                    ).fetchone()["n"]
                print(f"  [DRY] project={pid} mr={miid} open_count={n_open}")
                continue
            res = sync_resolved_from_gitlab(
                project_id=pid, mr_iid=miid,
                actor_username="sync-script",
            )
            print(
                f"  project={pid} mr={miid} scanned={res['scanned']} "
                f"updated={res['updated']}"
            )
            total_updated += res["updated"]
        print(f"\n总计 updated: {total_updated}")
        return 0

    if args.dry_run:
        with store._conn() as conn:
            n_open = conn.execute(
                "SELECT COUNT(*) AS n FROM suggestions "
                "WHERE project_id=? AND mr_iid=? AND state='open'",
                (args.project_id, args.mr_iid),
            ).fetchone()["n"]
        print(f"[DRY] project={args.project_id} mr={args.mr_iid} open_count={n_open}")
        return 0

    res = sync_resolved_from_gitlab(
        project_id=args.project_id, mr_iid=args.mr_iid,
        actor_username="sync-script",
    )
    print(
        f"project={args.project_id} mr={args.mr_iid} "
        f"scanned={res['scanned']} updated={res['updated']}"
    )
    if res["note_ids"]:
        print(f"updated note_ids: {[n[:12] for n in res['note_ids']]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
