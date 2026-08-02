"""Telemetry collector — pr_agent 风格的检视概况采集.

输出 SectionResult.data 含:
    mr_count          : 本周窗口内的 MR 数 (来自 mr_activity OVERVIEW)
    mr_total          : 项目累计 MR 数
    suggestion_count  : 本周窗口 suggestion 数
    suggestion_total  : 项目累计 suggestion 数
    adoption_rate     : 已采纳 / (已采纳 + 已关闭), 0~1
    severity_breakdown: {high: N, medium: M, ...}
    top_rules         : [(rule_key, count), ...] 前 5 名

也保留向后兼容字段 (success / failed / by_command / top_mrs) 用于旧实现。
"""
from __future__ import annotations

from datetime import datetime
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
            pid = ctx.target_project_id or None
            # SQLite 比较 created_at (UTC ISO 字符串) 用字典序, 传非 UTC 字符串会被
            # 时区后缀打乱顺序 (如 "2026-07-30T16:00:00+00:00" < "2026-07-27T00:00:00+08:00"
            # 因为 0<7 字典序). 统一转 UTC 再传.
            from datetime import timezone as _tz
            since_iso = week_start.astimezone(_tz.utc).isoformat()
            until_iso = week_end.astimezone(_tz.utc).isoformat()

            # ---- MR 窗口 + 累计 ----
            mr_overview_window = store.mr_overview(
                project_id=pid, since=since_iso, until=until_iso,
            )
            mr_overview_total = store.mr_overview(project_id=pid)
            # window 含 merged/closed/opened, 但 PR-Agent 模板用 merge_count 概念
            # 这里 mr_count 报"窗口内出现过的不同 MR" — 由 window_count 提供
            mr_count = mr_overview_window["window_count"]

            # ---- suggestion 窗口 + 累计 ----
            win_metrics = store.suggestion_metrics(
                project_id=pid, since=since_iso, until=until_iso,
            )
            all_metrics = store.suggestion_metrics(project_id=pid)
            suggestion_count = win_metrics["total"]
            suggestion_total = all_metrics["total"]
            # 累计采纳率 = 全量已采纳 / 全量 (不看周窗口)
            # store 返回 0~1 小数, renderer 再 *100 = %, 这里不要多除一次
            adoption_rate = all_metrics.get("adoption_rate", 0.0) or 0.0

            # severity 重整: severity_counts -> severity_breakdown (按 high/medium/low/critical)
            sev = win_metrics.get("severity_counts") or {}
            # 兼容 'unspecified' 标签
            severity_breakdown = {k: v for k, v in sev.items() if k != "unspecified"}
            if "unspecified" in sev:
                severity_breakdown["other"] = sev["unspecified"]

            # ---- 触发最多规则 (窗口期) ----
            top_rules = store.rule_key_counts(
                project_id=pid, since=since_iso, until=until_iso, top_n=5,
            )

            # ---- 兼容原实现 ----
            rows = store.list_runs(
                project_id=pid, since=since_iso, until=until_iso, limit=1000,
            )
            mr_cache: dict[tuple[int, int], dict] = {}
            enriched: list[dict] = []
            for r in rows:
                key = (r["project_id"], r["mr_iid"])
                if key not in mr_cache:
                    mr_cache[key] = store.get_mr(*key) or {}
                r2 = dict(r)
                mr_meta = mr_cache[key]
                r2["mr_title"] = mr_meta.get("title", "")
                r2["mr_author"] = mr_meta.get("author_sticky") or mr_meta.get("author_username", "")
                enriched.append(r2)

            stats = self._aggregate(enriched)
            stats["suggestions"] = {
                "total": suggestion_count,
                "state_counts": win_metrics.get("state_counts", {}),
                "severity_counts": win_metrics.get("severity_counts", {}),
                "action_counts": win_metrics.get("action_counts", {}),
                "adoption_rate": win_metrics.get("adoption_rate", 0.0),
                "adopted": win_metrics.get("adopted", 0),
                "dismissed": win_metrics.get("dismissed", 0),
            }

            dismissals = store.list_suggestion_actions(
                project_id=pid, action="dismissed",
                since=since_iso, until=until_iso, limit=10000,
            )
            from collections import defaultdict
            reasons: dict[str, int] = defaultdict(int)
            for dismissal in dismissals:
                reasons[(dismissal.get("reason") or "未填写原因").strip()] += 1
            stats["dismissal_reasons"] = dict(sorted(reasons.items(), key=lambda item: (-item[1], item[0])))

            # ---- 新增 pr_agent 风格字段 (放在 data 顶层) ----
            stats.update({
                "mr_count": mr_count,
                "mr_total": mr_overview_total["total"],
                "suggestion_count": suggestion_count,
                "suggestion_total": suggestion_total,
                "adoption_rate": adoption_rate,
                "severity_breakdown": severity_breakdown,
                "top_rules": top_rules,
            })

            # ---- 环比 delta (从上周 artifact 计算, 给 renderer 显示趋势箭头) ----
            prev_t = (ctx.prev_data.get("telemetry") or {}) if ctx.prev_data else {}
            if prev_t:
                def _delta(cur, p):
                    return cur - p if (cur is not None and p is not None) else None
                prev_sev = prev_t.get("severity_breakdown") or {}
                prev_hc = (prev_sev.get("high", 0) + prev_sev.get("critical", 0))
                cur_hc = severity_breakdown.get("high", 0) + severity_breakdown.get("critical", 0)
                # adoption_rate: prev 可能不存在该字段, 用 None 表示"无对比基准"
                prev_ar = prev_t.get("adoption_rate")
                stats["deltas"] = {
                    "mr_count": _delta(mr_count, prev_t.get("mr_count")),
                    "suggestion_count": _delta(suggestion_count, prev_t.get("suggestion_count")),
                    "adoption_rate_pct": _delta(
                        round(adoption_rate * 100, 1),
                        round(prev_ar * 100, 1) if prev_ar is not None else None,
                    ),
                    "high_critical": _delta(cur_hc, prev_hc if prev_sev else None),
                }
            else:
                stats["deltas"] = {}

            return SectionResult(
                status="ok",
                data=stats,
                meta={"rows": len(rows), "week_start": since_iso, "week_end": until_iso,
                      "mr_window": mr_overview_window, "mr_total": mr_overview_total["total"]},
            )
        except Exception as e:
            logger.exception("telemetry collector failed: {}", e)
            return SectionResult(status="failed", error=str(e), data=None, meta={})

    @staticmethod
    def _aggregate(rows: list[dict]) -> dict[str, Any]:
        from collections import defaultdict
        if not rows:
            return {
                "total": 0, "success": 0, "failed": 0, "running": 0,
                "skipped": 0, "success_rate": 0.0, "avg_duration_ms": 0,
                "by_command": {}, "by_status": {}, "by_day": {},
                "top_mrs": [], "failed_runs": [],
            }

        # by_command: {command: {"count": N, "success": N, "failed": N, "running": N, "avg_duration_ms": int, "max_duration_ms": int}}
        by_command: dict[str, dict] = {}
        by_status: dict[str, int] = defaultdict(int)
        by_day: dict[str, int] = defaultdict(int)
        by_mr: dict[tuple[int, int], dict] = {}
        failed_runs: list[dict] = []
        success_count = failed_count = running_count = skipped_count = 0
        duration_total = 0
        for r in rows:
            cmd = r.get("command") or "?"
            status = r.get("status") or "?"
            dur = r.get("duration_ms") or 0
            b = by_command.setdefault(cmd, {"count": 0, "success": 0, "failed": 0, "running": 0, "skipped": 0,
                                            "duration_total": 0, "max_duration_ms": 0, "avg_duration_ms": 0})
            b["count"] += 1
            b["duration_total"] += dur
            b["max_duration_ms"] = max(b["max_duration_ms"], dur)
            if status == "success":
                b["success"] += 1; success_count += 1
            elif status in ("failed", "timeout"):
                b["failed"] += 1; failed_count += 1
            elif status == "running":
                b["running"] += 1; running_count += 1
            elif status == "skipped":
                b["skipped"] += 1; skipped_count += 1
            by_status[status] += 1

            day = (r.get("started_at") or "")[:10]
            if day:
                by_day[day] += 1

            dur = r.get("duration_ms") or 0
            duration_total += dur

            key = (r["project_id"], r["mr_iid"])
            cur = by_mr.get(key)
            if cur is None or (r.get("started_at") or "") > (cur.get("started_at") or ""):
                by_mr[key] = r

            if status in ("failed", "timeout"):
                failed_runs.append({
                    "project_id": r["project_id"], "mr_iid": r["mr_iid"],
                    "command": cmd, "started_at": r.get("started_at"),
                    "duration_ms": dur, "error": (r.get("error") or "")[:200],
                    "mr_title": r.get("mr_title", ""),
                })

        total = len(rows)
        avg_ms = duration_total // total if total else 0
        success_rate = round(success_count / total * 100, 1) if total else 0.0
        for cmd, b in by_command.items():
            if b["count"] > 0:
                b["avg_duration_ms"] = b["duration_total"] // b["count"]
            del b["duration_total"]
        # 转换 by_status 为正常 dict (不 defaultdict)
        by_status_plain = dict(by_status)
        # ---- 在按 MR 聚合时也累计 success / failed / runs ----
        per_mr_stats: dict[tuple[int, int], dict] = {}
        for r in rows:
            key = (r["project_id"], r["mr_iid"])
            s_mr = per_mr_stats.setdefault(key, {"runs": 0, "success": 0, "failed": 0})
            s_mr["runs"] += 1
            st = r.get("status", "")
            if st == "success":
                s_mr["success"] += 1
            elif st in ("failed", "timeout"):
                s_mr["failed"] += 1
        per_mr_top = sorted(per_mr_stats.items(), key=lambda kv: -kv[1]["runs"])[:5]

        top_mrs_block = []
        for (pid, iid), st in per_mr_top:
            sample = by_mr.get((pid, iid), {})
            top_mrs_block.append({
                "project_id": pid, "mr_iid": iid,
                "title": sample.get("mr_title", ""),
                "author": sample.get("mr_author", ""),
                "runs": st["runs"], "success": st["success"], "failed": st["failed"],
            })

        return {
            "total": total,
            "success": success_count,
            "failed": failed_count,
            "running": running_count,
            "skipped": skipped_count,
            "success_rate": success_rate,
            "avg_duration_ms": avg_ms,
            "by_command": dict(sorted(by_command.items())),
            "by_status": by_status_plain,
            "by_day": dict(sorted(by_day.items())),
            "top_mrs": top_mrs_block,
            "failed_runs": failed_runs[:5],
        }
