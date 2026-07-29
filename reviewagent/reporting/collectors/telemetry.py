"""Telemetry collector — 从 reviewagent telemetry.db 拉本周 run 统计.

输出 SectionResult.data 含:
    total / success / failed / success_rate
    by_command / by_status / by_day
    top_mrs
    failed_runs
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from reviewagent.logging_setup import logger
from reviewagent.telemetry.store import get_store

from .base import CollectorContext, SectionResult


class TelemetryCollector:
    name: str = "telemetry"

    def collect(
        self,
        *,
        week_start: datetime,
        week_end: datetime,
        ctx: CollectorContext,
    ) -> SectionResult:
        try:
            store = get_store()
            rows = store.list_runs(
                project_id=ctx.target_project_id or None,
                since=week_start.isoformat(),
                until=week_end.isoformat(),
                limit=1000,
            )
            # 关联 MR title
            mr_cache: dict[tuple[int, int], dict] = {}
            enriched: list[dict] = []
            for r in rows:
                key = (r["project_id"], r["mr_iid"])
                if key not in mr_cache:
                    mr = store.get_mr(*key) or {}
                    mr_cache[key] = mr
                r2 = dict(r)
                mr_meta = mr_cache[key]
                r2["mr_title"] = mr_meta.get("title", "")
                r2["mr_author"] = mr_meta.get("author_sticky") or mr_meta.get("author_username", "")
                enriched.append(r2)

            stats = self._aggregate(enriched)
            return SectionResult(
                status="ok",
                data=stats,
                meta={"rows": len(rows), "week_start": week_start.isoformat(),
                      "week_end": week_end.isoformat()},
            )
        except Exception as e:
            logger.exception("telemetry collector failed: {}", e)
            return SectionResult(
                status="failed", error=str(e), data=None, meta={}
            )

    @staticmethod
    def _aggregate(rows: list[dict]) -> dict[str, Any]:
        total = len(rows)
        by_command: dict[str, dict] = {}
        by_status: dict[str, int] = defaultdict(int)
        by_day: dict[str, int] = defaultdict(int)
        by_mr: dict[tuple[int, int], dict] = {}
        failed_runs: list[dict] = []

        for r in rows:
            cmd = r["command"]
            st = r["status"]
            dur = r.get("duration_ms") or 0
            day = (r.get("started_at") or "")[:10]
            bc = by_command.setdefault(cmd, {
                "count": 0, "success": 0, "failed": 0, "timeout": 0, "running": 0,
                "total_duration_ms": 0, "max_duration_ms": 0,
            })
            bc["count"] += 1
            if st in bc:
                bc[st] += 1
            bc["total_duration_ms"] += dur
            bc["max_duration_ms"] = max(bc["max_duration_ms"], dur)
            by_status[st] += 1
            by_day[day] += 1
            key = (r["project_id"], r["mr_iid"])
            bm = by_mr.setdefault(key, {
                "title": r.get("mr_title") or "?",
                "author": r.get("mr_author") or "?",
                "runs": 0, "success": 0, "failed": 0,
            })
            bm["runs"] += 1
            if st == "success":
                bm["success"] += 1
            elif st in ("failed", "timeout"):
                bm["failed"] += 1
            if st in ("failed", "timeout"):
                failed_runs.append(r)

        for bc in by_command.values():
            bc["avg_duration_ms"] = int(bc["total_duration_ms"] / bc["count"]) if bc["count"] else 0
        success = by_status.get("success", 0)
        fail = by_status.get("failed", 0) + by_status.get("timeout", 0)
        success_rate = (success / total * 100) if total else 0.0
        avg_duration = (
            int(sum(r.get("duration_ms") or 0 for r in rows) / total) if total else 0
        )
        top_mrs = [
            {"project_id": k[0], "mr_iid": k[1], **v}
            for k, v in sorted(by_mr.items(), key=lambda kv: kv[1]["runs"], reverse=True)
        ][:10]

        return {
            "total": total,
            "success": success,
            "failed": fail,
            "running": by_status.get("running", 0),
            "success_rate": round(success_rate, 1),
            "avg_duration_ms": avg_duration,
            "by_command": by_command,
            "by_status": dict(by_status),
            "by_day": dict(sorted(by_day.items())),
            "top_mrs": top_mrs,
            "failed_runs": failed_runs[:20],
        }


__all__ = ["TelemetryCollector"]
