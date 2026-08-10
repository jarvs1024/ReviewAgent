"""Periodic reconcile loop — 扫描所有 bot 跟踪的 open MR, sync GitLab click ✓ 状态.

Why:
    GitLab 17.5 偶尔不发 "marked this discussion as resolved" webhook 给
    note_events hook. publish_overview 顶部 pre-reconcile 已经覆盖了
    "click 后还有 push /adopt /dismiss 等其他事件" 的情况. 但 "纯 click-only
    无任何后续事件" 场景下, 没有事件触发 publish_overview, DB 会永远停在 open.

入口:
    - reconcile_open_mrs() — 一次完整扫描 (用于 launchd StartInterval 调起)
    - reconcile_single_mr() — 单个 MR 扫描 (用于测试 / 一次性手动跑)
"""
from __future__ import annotations

from typing import Any

from reviewagent.commands._common import publish_overview
from reviewagent.commands.suggestion_actions import _scan_and_mark_resolved_silent
from reviewagent.gitlab.client import GitLabClient
from reviewagent.logging_setup import logger
from reviewagent.telemetry.store import get_store


def reconcile_single_mr(
    *,
    project_id: int,
    mr_iid: int,
    actor_username: str = "periodic_reconciler",
) -> dict[str, Any]:
    """单个 MR 同步: scan GitLab click ✓ → DB update → publish_overview.

    Returns:
        {
            "scanned": int,
            "updated": int,
            "note_ids": list[str],
            "overview_refreshed": bool,
        }
    """
    result = _scan_and_mark_resolved_silent(
        project_id=project_id, mr_iid=mr_iid,
        actor_username=actor_username,
        adoption_source="periodic_reconcile",
        reason="periodic_reconciler: 用户在 UI 点 ✓ 但无 webhook 触发",
        validation_status="periodic_reconcile",
    )

    overview_refreshed = False
    if result["updated"]:
        try:
            publish_overview(
                project_id=project_id, mr_iid=mr_iid,
                inline_posted_count=0,
                run_late_detect=False,
            )
            overview_refreshed = True
        except Exception as _e:  # noqa: BLE001
            logger.warning(
                "reconcile_single_mr.publish_overview failed project={} mr={} err={}",
                project_id, mr_iid, _e,
            )

    return {
        **result,
        "overview_refreshed": overview_refreshed,
    }


def reconcile_open_mrs(
    *,
    project_id: int | None = None,
) -> dict[str, Any]:
    """扫描 bot 跟踪的所有 open MR, 对每个调 reconcile_single_mr.

    Args:
        project_id: 限定单个 project (None = 所有 bot 跟踪的 project).
            默认 None; 周报 / 测试可以传具体 project.

    Returns:
        {
            "total_mrs": int,           # 扫的 MR 数
            "total_updated": int,       # 所有 MR 一共更新了多少 suggestion
            "mrs_updated": list[dict],  # 实际有更新的 MR 列表
                                     # 每项: {"project_id", "mr_iid", "scanned", "updated", "note_ids"}
            "duration_s": float,
        }
    """
    import time
    start = time.monotonic()
    store = get_store()

    # 找 bot 跟踪过的 open MR. mr_activity 表存的是 bot 已经处理过的 MR.
    # 新 MR 没有记录会被 GitLab webhook 自然 cover (push 时会入队 describe+improve).
    try:
        rows = store.list_mrs(project_id=project_id, state="opened", limit=500)
    except Exception as e:  # noqa: BLE001
        logger.warning("reconcile_open_mrs.list_mrs failed (non-fatal): {}", e)
        rows = []

    total_updated = 0
    mrs_updated: list[dict[str, Any]] = []
    for r in rows:
        pid = r.get("project_id")
        iid = r.get("mr_iid")
        if not pid or not iid:
            continue
        try:
            single = reconcile_single_mr(project_id=pid, mr_iid=iid)
        except Exception as e:  # noqa: BLE001
            # 单个 MR 失败不影响其他 — 每个 MR 独立 try.
            logger.warning(
                "reconcile_open_mrs.single_mr failed project={} mr={} err={}",
                pid, iid, e,
            )
            continue
        if single.get("updated"):
            total_updated += single["updated"]
            mrs_updated.append({
                "project_id": pid,
                "mr_iid": iid,
                "scanned": single["scanned"],
                "updated": single["updated"],
                "note_ids": single["note_ids"],
            })

    duration_s = round(time.monotonic() - start, 3)
    logger.info(
        "reconcile_open_mrs summary scanned_mrs={} total_updated={} duration_s={}",
        len(rows), total_updated, duration_s,
    )
    return {
        "total_mrs": len(rows),
        "total_updated": total_updated,
        "mrs_updated": mrs_updated,
        "duration_s": duration_s,
    }


if __name__ == "__main__":
    """CLI: 直接跑一次, 用于手动触发 / 测试.

    用法: python -m reviewagent.reconciler.loop [--project-id N]
    """
    import argparse
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from reviewagent.logging_setup import setup_logging

    setup_logging()
    p = argparse.ArgumentParser(description="一次性 reconcile open MRs")
    p.add_argument("--project-id", type=int, default=None)
    args = p.parse_args()
    result = reconcile_open_mrs(project_id=args.project_id)
    print(f"[ok] scanned {result['total_mrs']} MRs, updated {result['total_updated']} suggestions in {result['duration_s']}s")
    if result["mrs_updated"]:
        for m in result["mrs_updated"]:
            print(f"  - project={m['project_id']} mr={m['mr_iid']} updated={m['updated']}")
