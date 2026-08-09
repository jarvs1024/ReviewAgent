#!/usr/bin/env python3
"""Reconcile late_detect: 对指定 MR 跑一次 late_detect (只扫 resolved + gitlab_resolve
那部分), 把历史误分类的「已关闭(未分类)」翻回 applied.

适用场景:
    历史数据里因为 race condition (用户先点 Resolve thread 后 push 代码) 误分类
    的一批 suggestions, 用这个脚本一次性扫.

Usage:
    python3 scripts/reconcile_late_detect.py --project-id 34 --mr-iid 247 [--head-sha HEAD]
    python3 scripts/reconcile_late_detect.py --all  # 扫所有 MR

    --head-sha 默认从 GitLab API 拉该 MR 当前 head_sha.
    --dry-run 只打印不改库.
"""
from __future__ import annotations

import argparse
import sys
import os

# 让脚本能 import reviewagent 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _resolve_sugs(store, project_id: int, mr_iid: int) -> list[dict]:
    return store.list_resolved_suggestions(project_id=project_id, mr_iid=mr_iid)


def reconcile_mr(
    store, gl, project_id: int, mr_iid: int, head_sha: str, *, dry_run: bool = False
) -> dict:
    """对单个 MR 跑 late_detect."""
    from reviewagent.commands.suggestion_actions import _late_detect_single

    late_sugs = _resolve_sugs(store, project_id=project_id, mr_iid=mr_iid)
    result = {"scanned": len(late_sugs), "applied": 0, "unchanged": 0, "errors": 0, "flipped": []}

    for sug in late_sugs:
        note_id = sug.get("note_id") or ""
        file_path = sug.get("file_path") or ""
        target_line = int(sug.get("target_line") or 0)
        target_line_end = int(sug.get("target_line_end") or target_line)

        if dry_run:
            current = store.get_suggestion_by_note_id(note_id)
            print(f"  [DRY] {note_id[:10]} file={file_path[-40:]} L{target_line}-{target_line_end} state={current['state'] if current else '?'} res_src={current.get('resolution_source') if current else '?'}")
            continue

        late_result = _late_detect_single(
            sug=sug, head_sha=head_sha,
            project_id=project_id, mr_iid=mr_iid,
            actor_username="reconcile-script",
        )
        if late_result == "applied":
            result["applied"] += 1
            result["flipped"].append(note_id)
        elif late_result == "error":
            result["errors"] += 1
        else:
            result["unchanged"] += 1

    return result


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--project-id", type=int, default=0)
    p.add_argument("--mr-iid", type=int, default=0)
    p.add_argument("--head-sha", default="", help="MR 当前 head_sha (空则从 GitLab API 拉)")
    p.add_argument("--all", action="store_true", help="扫所有有 resolved+gitlab_resolve suggestion 的 MR")
    p.add_argument("--dry-run", action="store_true", help="只打印不改")
    args = p.parse_args()

    if not args.all and (not args.project_id or not args.mr_iid):
        p.error("需要 --project-id + --mr-iid 或 --all")

    from reviewagent.gitlab.client import GitLabClient
    from reviewagent.telemetry.store import get_store

    gl = GitLabClient()
    store = get_store()

    if args.all:
        # 扫所有有 resolved+gitlab_resolve 的 MR
        with store._conn() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT project_id, mr_iid FROM suggestions
                WHERE state='resolved' AND resolution_source='gitlab_resolve'
                ORDER BY project_id, mr_iid
                """
            ).fetchall()
        if not rows:
            print("无 resolved+gitlab_resolve suggestions, 无需 reconcile")
            return 0
        print(f"待 reconcile 的 MR: {len(rows)}")
        total_applied = 0
        for r in rows:
            pid, miid = r["project_id"], r["mr_iid"]
            # 拉 head_sha
            try:
                mr = gl._gl.projects.get(pid).mergerequests.get(miid)
                head_sha = mr.sha
            except Exception as e:
                print(f"  skip mr={pid}/{miid}: 拉 head_sha 失败 {e}")
                continue
            print(f"\n=== project={pid} mr={miid} head_sha={head_sha[:8]} ===")
            res = reconcile_mr(store, gl, pid, miid, head_sha, dry_run=args.dry_run)
            print(f"  scanned={res['scanned']} applied={res['applied']} unchanged={res['unchanged']} errors={res['errors']}")
            total_applied += res["applied"]
        print(f"\n总计 applied: {total_applied}")
        return 0

    # 单 MR 模式
    head_sha = args.head_sha
    if not head_sha:
        try:
            mr = gl._gl.projects.get(args.project_id).mergerequests.get(args.mr_iid)
            head_sha = mr.sha
        except Exception as e:
            print(f"ERROR: 拉 head_sha 失败: {e}")
            return 1

    print(f"=== project={args.project_id} mr={args.mr_iid} head_sha={head_sha[:8]} ===")
    res = reconcile_mr(
        store, gl, args.project_id, args.mr_iid, head_sha, dry_run=args.dry_run,
    )
    print(f"scanned={res['scanned']} applied={res['applied']} unchanged={res['unchanged']} errors={res['errors']}")
    if res["flipped"]:
        print(f"flipped note_ids: {res['flipped']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
