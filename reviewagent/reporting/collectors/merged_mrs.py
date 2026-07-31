"""Merged MRs collector — 从 GitLab API 拉本周合并到目标分支的 MR 列表.

参考 pr-agent `pr_agent/reporting/collectors/master_merges.py`:
- python-gitlab 直连 (不依赖 PR 上下文)
- target_branch 默认取项目 default_branch, ctx.target_branch 可覆盖
- 时间窗通过 updated_after / updated_before 过滤 (API 行为: 只对 merged_at)

输出 SectionResult.data:
    total: int
    items: list[{iid, title, author, merged_at, merged_by, source_branch, web_url, additions, deletions, changed_files}]
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from reviewagent.gitlab.client import GitLabError, client as gl
from reviewagent.logging_setup import logger

from .base import CollectorContext, SectionResult


class MergedMrsCollector:
    """列出本周合并到目标分支的 MR — Layer B 数据源."""

    name: str = "merged_mrs"

    def collect(
        self,
        *,
        week_start: datetime,
        week_end: datetime,
        ctx: CollectorContext,
    ) -> SectionResult:
        project_id = ctx.target_project_id or 0
        if not project_id:
            return SectionResult(status="skipped", data=None,
                                 meta={"reason": "no_target_project_id"})

        # 默认用 main; env 也可改 (target_branch 字段)
        target_branch = ctx.target_branch or "main"

        try:
            mrs = gl.list_project_mrs(
                project_id,
                state="merged",
                updated_after=week_start.isoformat(),
                updated_before=week_end.isoformat(),
                per_page=100,
            )
        except GitLabError as e:
            logger.warning("merged_mrs collector failed: {}", e)
            return SectionResult(status="failed", data=None, error=str(e)[:300])

        # 按 merged_at 重排 (updated_after 是个粗筛, 真正合并时间可能落在边界外)
        ws = week_start.timestamp()
        we = week_end.timestamp()
        in_window = []
        for m in mrs:
            merged_at = m.get("merged_at") or m.get("updated_at")
            if not merged_at:
                continue
            try:
                ts = datetime.fromisoformat(merged_at.replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
            if ws <= ts < we:
                in_window.append(m)
            else:
                logger.debug("merged_mrs out_of_window iid={} merged_at={}", m.get("iid"), merged_at)

        items: list[dict[str, Any]] = []
        for m in in_window:
            items.append({
                "iid": m.get("iid"),
                "title": m.get("title", ""),
                "author": (m.get("author") or {}).get("username", ""),
                "merged_by": (m.get("merged_by") or {}).get("username", ""),
                "merged_at": m.get("merged_at"),
                "source_branch": m.get("source_branch"),
                "target_branch": m.get("target_branch"),
                "web_url": m.get("web_url"),
                "additions": m.get("additions_count") or 0,
                "deletions": m.get("deletions_count") or 0,
                "changed_files": m.get("changed_files_count") or 0,
                "squash": bool(m.get("squash")),
            })

        items.sort(key=lambda m: m.get("merged_at") or "", reverse=True)

        return SectionResult(
            status="ok",
            data={"total": len(items), "target_branch": target_branch, "items": items},
            meta={"queried_total": len(mrs), "week_start": week_start.isoformat(),
                  "week_end": week_end.isoformat()},
        )
