#!/usr/bin/env python3
"""从 GitLab 拉取 MR 实际状态, 同步更新到 mr_activity 表.

用法:
    python scripts/sync_mr_states.py              # dry-run, 只看差异
    python scripts/sync_mr_states.py --apply      # 实际写入 DB
"""
import argparse
import sys
import os

# 让 import找到项目根
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reviewagent.config import config
from reviewagent.gitlab.client import GitLabClient
from reviewagent.telemetry.store import get_store
from reviewagent.telemetry.models import MRRecord, _parse_dt


def sync_mr_states(dry_run: bool = True) -> dict:
    gl = GitLabClient()
    store = get_store()

    # 从 DB 拿所有已记录的 MR
    all_mrs = store.list_mrs(limit=10000)
    project_ids = {row["project_id"] for row in all_mrs}

    stats = {"checked": 0, "updated": 0, "already_ok": 0, "errors": 0}

    for pid in sorted(project_ids):
        db_mrs = {row["mr_iid"]: row for row in all_mrs if row["project_id"] == pid}
        print(f"\nProject {pid}: {len(db_mrs)} MRs in DB")

        # 从 GitLab 拉所有状态的 MR
        for state_filter in ("opened", "merged", "closed"):
            try:
                gitlab_mrs = gl.list_project_mrs(pid, state=state_filter, per_page=100)
            except Exception as e:
                print(f"  ERROR fetching state={state_filter}: {e}")
                stats["errors"] += 1
                continue

            for gm in gitlab_mrs:
                iid = gm.get("iid")
                if iid not in db_mrs:
                    continue  # DB 里没记录的不管

                stats["checked"] += 1
                db_state = db_mrs[iid].get("state", "")
                gl_state = gm.get("state", "")

                if db_state == gl_state:
                    stats["already_ok"] += 1
                    continue

                # 有差异
                merged_at = _parse_dt(gm.get("merged_at"))
                updated_at = _parse_dt(gm.get("updated_at"))
                print(f"  MR !{iid}: {db_state} -> {gl_state} (merged_at={merged_at})")

                if not dry_run:
                    record = MRRecord(
                        project_id=pid,
                        mr_iid=iid,
                        title=gm.get("title", ""),
                        author_username=db_mrs[iid].get("author_username", ""),
                        source_branch=gm.get("source_branch", ""),
                        target_branch=gm.get("target_branch", ""),
                        state=gl_state,
                        created_at=_parse_dt(gm.get("created_at")),
                        updated_at=updated_at,
                        merged_at=merged_at,
                    )
                    store.upsert_mr(record)

                stats["updated"] += 1

    print(f"\n{'=' * 50}")
    print(f"Checked: {stats['checked']}, Updated: {stats['updated']}, "
          f"Already OK: {stats['already_ok']}, Errors: {stats['errors']}")
    if dry_run and stats["updated"] > 0:
        print("(dry-run mode, no changes written. Use --apply to update)")
    return stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Actually write changes to DB")
    args = parser.parse_args()
    sync_mr_states(dry_run=not args.apply)
