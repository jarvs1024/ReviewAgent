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
from pathlib import Path
from typing import Any

from reviewagent.config import config
from reviewagent.gitlab.client import GitLabError, client as gl
from reviewagent.logging_setup import logger
from reviewagent.opencode.client import OpencodeError, client as opencode

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
                target_branch=target_branch,
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

        # mr_list (PR-Agent 命名) + items (向后兼容) 同时返回
        author_set: set[str] = set()
        additions_total = 0
        deletions_total = 0
        files_changed_total = 0
        items: list[dict[str, Any]] = []
        mr_list: list[dict[str, Any]] = []
        for m in in_window:
            author = (m.get("author") or {}).get("username", "") or ""
            merged_by = (m.get("merged_by") or {}).get("username", "") or ""
            author_set.add(author)
            adds = m.get("additions_count") or 0
            dels = m.get("deletions_count") or 0
            additions_total += adds
            deletions_total += dels
            # changes_count 是 GitLab list API 确实返回的字段 (字符串), 比 additions/deletions 可靠
            cc = m.get("changes_count")
            files_changed = int(cc) if cc and str(cc).isdigit() else 0
            files_changed_total += files_changed
            item = {
                "iid": m.get("iid"),
                "title": m.get("title", ""),
                "author": author,
                "merged_by": merged_by,
                "merged_at": m.get("merged_at"),
                "source_branch": m.get("source_branch"),
                "target_branch": m.get("target_branch"),
                "web_url": m.get("web_url"),
                "url": m.get("web_url"),  # PR-Agent 风格
                "additions": adds,
                "deletions": dels,
                "changed_files": files_changed,
                "squash": bool(m.get("squash")),
                "description": (m.get("description") or "").strip(),
            }
            items.append(item)
            mr_list.append(item)

        items.sort(key=lambda m: m.get("merged_at") or "", reverse=True)
        mr_list.sort(key=lambda m: m.get("merged_at") or "", reverse=True)

        # 构造 head_line 由 renderer 用, 这里给齐 pr_agent 字段
        data = {
            "total": len(items),
            "merge_count": len(items),
            "author_count": len(author_set),
            "authors": sorted(author_set),
            "additions": additions_total,
            "deletions": deletions_total,
            "files_changed": files_changed_total,
            "items": items,
            "mr_list": mr_list,
            "target_branch": target_branch,
            "window": {"since": week_start.isoformat(), "until": week_end.isoformat()},
            # LLM 生成的变更摘要 (失败则回退到确定性 _build_change_summary)
            "llm_description_markdown": "",
        }

        # 调 opencode 生成"变更摘要" (像 improve 的 _call_chunk: 数据塞进 prompt, files=[])
        prev_merged = (ctx.prev_data.get("merged_mrs") or {}) if ctx.prev_data else {}
        try:
            llm_md = self._generate_llm_summary(data, target_branch, prev_merged)
            if llm_md and llm_md.strip():
                data["llm_description_markdown"] = llm_md
                logger.info("merged_mrs.llm_summary ok chars={}", len(llm_md))
            else:
                logger.warning("merged_mrs.llm_summary empty -> fallback to deterministic")
        except Exception as e:
            logger.warning("merged_mrs.llm_summary failed (fallback to deterministic): {}", e)

        return SectionResult(
            status="ok",
            data=data,
            meta={"queried_total": len(mrs), "week_start": week_start.isoformat(),
                  "week_end": week_end.isoformat()},
        )

    @staticmethod
    def _build_llm_prompt(data: dict[str, Any], target_branch: str,
                          prev_data: dict[str, Any] | None = None) -> str:
        """构造发给 weekly_change_summary agent 的 user prompt (MR 清单)."""
        items = data.get("items") or []
        lines: list[str] = [
            "你是技术周报编辑。下面是本周合并到目标分支的 MR 清单。",
            "请据此写一段『变更摘要』：按主题/模块归纳团队本周的主要交付，用自然语言讲清楚做了什么、",
            "有什么值得注意的趋势。不要只罗列 MR，要有洞察。",
            "输出严格 JSON：{\"markdown\": \"...\"}, markdown 用中文，可含 bullet，不要用 # 顶级标题。",
            "",
            f"目标分支：`{target_branch}`",
            f"本周合并 MR 数：{len(items)}，涉及作者：{data.get('author_count', 0)} 位。",
        ]
        if prev_data:
            prev_count = prev_data.get("merge_count", prev_data.get("total", 0))
            lines.append(f"上周合并 MR 数：{prev_count}（用于趋势对比，不要编造）。")
        lines.append("")
        lines.append("MR 清单（iid | 标题 | 作者 | +增/-删 | 描述摘要）：")
        for m in items[:40]:
            desc = (m.get("description") or "").strip().replace("\n", " ")[:300]
            lines.append(
                f"- [{m.get('iid')}] {m.get('title')} | @{m.get('author')} "
                f"| +{m.get('additions')}/-{m.get('deletions')} | {desc}"
            )
        return "\n".join(lines)

    def _generate_llm_summary(self, data: dict[str, Any], target_branch: str,
                              prev_data: dict[str, Any] | None = None) -> str:
        """调 opencode weekly_change_summary agent 生成变更摘要；失败抛异常由调用方回退."""
        prompt = self._build_llm_prompt(data, target_branch, prev_data)
        oc_result = opencode.run(
            agent="weekly_change_summary",
            prompt=prompt,
            workdir=Path.cwd(),
            files=[],
            timeout=max(120, int(config.rq_worker_timeout * 0.4)),
        )
        return (oc_result.data or {}).get("markdown", "") or ""
