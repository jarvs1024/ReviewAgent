"""Layer-C1 collector: 本周代码质量全量扫描 (独立全局体检).

设计:
- 不再读取 telemetry 的 suggestions（避免和「本周检视概况」同数据源）。
- 改为拉取本周合并到目标分支的所有 MR，获取每个 MR 的 changes（结构化 diff），
  按文件聚合后形成「本周全项目变更视图」。
- 把 MR 摘要 + 文件热力图 + 关键 diff 片段喂给 opencode，要求从整个项目视角
  做一次全局代码质量扫描，输出固定 4 小节：
    高风险模块 / 新增坏味道 / 测试覆盖与可靠性 / 建议跟进。
- LLM 失败时回退到基于变更热力图的确定性 markdown。

输出 SectionResult.data:
    target_branch    : 目标分支
    total_mrs        : 本周合并 MR 数
    total_files      : 去重变更文件数
    total_additions  : 新增行数
    total_deletions  : 删除行数
    file_changes     : [{path, additions, deletions, change_count, mr_iids, is_new, is_deleted}]
    top_files        : 按变更行数排序的前 N 文件
    llm_review_markdown : 渲染好的 markdown 报告
    truncated        : bool
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone as _tz
from pathlib import Path
from typing import Any

from reviewagent.config import config
from reviewagent.gitlab.client import GitLabError, client as gl
from reviewagent.logging_setup import logger
from reviewagent.opencode.client import OpencodeError, client as opencode

from .base import CollectorContext, SectionResult


class RepoScanCollector:
    """本周代码质量全量扫描 (基于本周 MR 变更数据的全局体检)."""

    name: str = "repo_scan"

    # 控制喂给 LLM 的上下文大小
    MAX_FILES_IN_HEATMAP = 30
    MAX_DIFF_SNIPPETS = 15
    MAX_DIFF_CHARS = 15000
    MAX_DIFF_LINES_PER_FILE = 120

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
        target_branch = ctx.target_branch or "main"

        # 1. 拉取本周 merged MRs
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
            logger.warning("repo_scan list mrs failed: {}", e)
            return SectionResult(status="failed", data=None, error=str(e)[:300])

        # 按 merged_at 精确过滤
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

        if not in_window:
            return SectionResult(
                status="ok",
                data={
                    "target_branch": target_branch,
                    "total_mrs": 0,
                    "total_files": 0,
                    "total_additions": 0,
                    "total_deletions": 0,
                    "file_changes": [],
                    "top_files": [],
                    "llm_review_markdown": "本周无合并到目标分支的 MR，未触发全量扫描。",
                    "truncated": False,
                },
                markdown="本周无合并到目标分支的 MR，未触发全量扫描。",
                meta={"reason": "no_mrs"},
            )

        # 2. 拉取每个 MR 的 changes 并聚合
        file_changes: dict[str, dict[str, Any]] = defaultdict(
            lambda: {
                "additions": 0,
                "deletions": 0,
                "change_count": 0,
                "mr_iids": [],
                "is_new": False,
                "is_deleted": False,
                "is_renamed": False,
                "diff_parts": [],
            }
        )
        mr_summaries: list[dict[str, Any]] = []
        total_additions = 0
        total_deletions = 0

        for m in in_window:
            iid = m.get("iid")
            title = m.get("title", "")
            author = (m.get("author") or {}).get("username", "") or "?"
            try:
                changes = gl.get_mr_changes(project_id, iid)
            except GitLabError as e:
                logger.warning("repo_scan get_mr_changes failed project={} mr={}: {}",
                               project_id, iid, e)
                changes = []

            mr_additions = 0
            mr_deletions = 0
            for c in changes:
                new_path = c.get("new_path") or c.get("old_path") or "(unknown)"
                old_path = c.get("old_path") or new_path
                diff_body = c.get("diff", "")
                adds, dels = self._count_diff_lines(diff_body)
                # API 可能直接返回 additions/deletions，优先用
                adds = c.get("additions", adds) or adds
                dels = c.get("deletions", dels) or dels

                rec = file_changes[new_path]
                rec["path"] = new_path
                rec["old_path"] = old_path
                rec["additions"] += adds
                rec["deletions"] += dels
                rec["change_count"] += 1
                if iid not in rec["mr_iids"]:
                    rec["mr_iids"].append(iid)
                if c.get("new_file"):
                    rec["is_new"] = True
                if c.get("deleted_file"):
                    rec["is_deleted"] = True
                if c.get("renamed_file"):
                    rec["is_renamed"] = True
                if diff_body:
                    rec["diff_parts"].append(
                        f"<!-- MR !{iid} -->\n{diff_body[:self.MAX_DIFF_LINES_PER_FILE * 200]}"
                    )

                mr_additions += adds
                mr_deletions += dels

            total_additions += mr_additions
            total_deletions += mr_deletions
            mr_summaries.append({
                "iid": iid,
                "title": title,
                "author": author,
                "additions": mr_additions,
                "deletions": mr_deletions,
                "files": len(changes),
            })

        # 转成普通 dict 并补齐 diff 聚合字段
        file_changes_plain: dict[str, dict[str, Any]] = {}
        for path, rec in file_changes.items():
            full_diff = "\n".join(rec["diff_parts"])
            rec["diff"] = full_diff
            file_changes_plain[path] = dict(rec)

        # 3. 排序：按总变更行数取 TOP 文件
        sorted_files = sorted(
            file_changes_plain.values(),
            key=lambda r: -(r["additions"] + r["deletions"]),
        )
        top_files = sorted_files[: self.MAX_FILES_IN_HEATMAP]

        # 4. 构造确定性回退 + LLM prompt
        deterministic_md = self._render_markdown(
            target_branch=target_branch,
            week_start=week_start,
            week_end=week_end,
            total_mrs=len(in_window),
            total_files=len(file_changes_plain),
            total_additions=total_additions,
            total_deletions=total_deletions,
            top_files=top_files,
            mr_summaries=mr_summaries,
        )

        prev_repo = (ctx.prev_data.get("repo_scan") or {}) if ctx.prev_data else {}
        llm_md = ""
        try:
            llm_md = self._generate_llm_review(
                target_branch=target_branch,
                week_start=week_start,
                week_end=week_end,
                total_mrs=len(in_window),
                total_files=len(file_changes_plain),
                total_additions=total_additions,
                total_deletions=total_deletions,
                top_files=top_files,
                mr_summaries=mr_summaries,
                file_changes=file_changes_plain,
                prev_data=prev_repo,
            )
            if not llm_md or not llm_md.strip():
                logger.warning("repo_scan.llm_review empty -> fallback to deterministic")
                llm_md = deterministic_md
            else:
                logger.info("repo_scan.llm_review ok chars={}", len(llm_md))
        except Exception as e:
            logger.warning("repo_scan.llm_review failed (fallback to deterministic): {}", e)
            llm_md = deterministic_md

        return SectionResult(
            status="ok",
            data={
                "target_branch": target_branch,
                "total_mrs": len(in_window),
                "total_files": len(file_changes_plain),
                "total_additions": total_additions,
                "total_deletions": total_deletions,
                "file_changes": list(file_changes_plain.values()),
                "top_files": top_files,
                "llm_review_markdown": llm_md,
                "truncated": False,
            },
            markdown=llm_md,
            meta={
                "total_mrs": len(in_window),
                "total_files": len(file_changes_plain),
                "total_additions": total_additions,
                "total_deletions": total_deletions,
            },
        )

    @staticmethod
    def _count_diff_lines(diff_body: str) -> tuple[int, int]:
        """统计 unified diff 的新增/删除行数."""
        additions = deletions = 0
        for line in diff_body.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                additions += 1
            elif line.startswith("-") and not line.startswith("---"):
                deletions += 1
        return additions, deletions

    @staticmethod
    def _is_core_path(path: str) -> bool:
        """判断文件是否属于核心模块（用于 diff 片段优先级）."""
        core_prefixes = (
            "services/", "framework/", "src/", "app/", "core/", "lib/",
            "reviewagent/",  # 对本项目自身也适用
        )
        lower = path.lower()
        return any(lower.startswith(p) for p in core_prefixes)

    def _select_diff_snippets(
        self,
        file_changes: dict[str, dict[str, Any]],
        top_files: list[dict[str, Any]],
    ) -> list[tuple[str, str]]:
        """按优先级挑选 diff 片段，控制总长度不超过 MAX_DIFF_CHARS."""
        candidates: list[dict[str, Any]] = []
        for f in top_files:
            path = f["path"]
            rec = file_changes.get(path)
            if not rec or not rec.get("diff"):
                continue
            # 优先级分数：核心路径 + 新增/删除 + 总变更行数
            score = f["additions"] + f["deletions"]
            if self._is_core_path(path):
                score += 500
            if f.get("is_new") or f.get("is_deleted"):
                score += 300
            if "test" in path.lower() or "spec" in path.lower():
                score += 100
            candidates.append({
                "path": path,
                "diff": rec["diff"],
                "score": score,
                "additions": f["additions"],
                "deletions": f["deletions"],
            })

        # 按分数降序
        candidates.sort(key=lambda x: -x["score"])

        snippets: list[tuple[str, str]] = []
        used_chars = 0
        for c in candidates[: self.MAX_DIFF_SNIPPETS]:
            diff = c["diff"]
            # 单文件截断
            lines = diff.splitlines()
            if len(lines) > self.MAX_DIFF_LINES_PER_FILE:
                diff = "\n".join(lines[: self.MAX_DIFF_LINES_PER_FILE]) + "\n[... diff 截断 ...]"
            if used_chars + len(diff) > self.MAX_DIFF_CHARS:
                remaining = self.MAX_DIFF_CHARS - used_chars
                if remaining > 200:
                    diff = diff[:remaining] + "\n[... 总 diff 截断 ...]"
                    snippets.append((c["path"], diff))
                break
            snippets.append((c["path"], diff))
            used_chars += len(diff)
        return snippets

    def _build_llm_prompt(
        self,
        *,
        target_branch: str,
        week_start: datetime,
        week_end: datetime,
        total_mrs: int,
        total_files: int,
        total_additions: int,
        total_deletions: int,
        top_files: list[dict[str, Any]],
        mr_summaries: list[dict[str, Any]],
        file_changes: dict[str, dict[str, Any]],
        prev_data: dict[str, Any] | None = None,
    ) -> str:
        """构造发给 weekly_quality_scan agent 的 user prompt.

        输入是本周所有 MR 的变更数据（diff），不是 telemetry 检视建议。
        """
        ws = week_start.strftime("%Y-%m-%d")
        we = week_end.strftime("%Y-%m-%d")

        lines: list[str] = [
            "你是代码质量分析师。下面是本周合并到目标分支的所有 MR 的变更数据（含 MR 摘要、文件热力图与关键 diff 片段）。",
            "请基于这些真实变更数据，对项目做一次**全量代码质量扫描**。",
            "",
            "【定位说明】这是独立于『本周检视概况』的全局代码扫描：",
            "- 『本周检视概况』是逐 MR diff 级检视汇总的建议条数与规则命中。",
            "- 本段只看你眼前的『本周所有变更数据』，从整个项目视角判断：这些改动叠加后，全局代码质量走向如何、有哪些跨文件/跨模块风险被 diff 视角漏掉。",
            "- **不要引用或复述任何 telemetry 检视数据**（如 high/medium 建议条数、SSD-RULE-* 规则命中次数等）。",
            "",
            "【输出格式】严格输出 JSON：{\"markdown\": \"...\"}。markdown 用中文，固定 4 个小节（用 **加粗** 作小标题，顺序不变，小节之间空一行）：",
            "**高风险模块** —— 从整个项目视角，指出本周风险最集中 / 最该关注的模块与文件，说明为什么（结合改动面、模块间耦合、影响半径、新增/删除的大文件、核心接口签名变化等）。",
            "**新增坏味道** —— 本周新出现或反复出现的代码坏味道趋势；从全局看哪些模式值得警惕（不要只报文件变更次数）。",
            "**测试覆盖与可靠性** —— 本周改动是否有对应测试覆盖、异常路径是否验证、可靠性风险（资源泄漏 / 并发 / 超时 / 幂等 / 回滚等）如何。",
            "**建议跟进** —— 具体、可执行的跟进项（补哪些测试、沉淀哪些规范、重构优先级），可以引用具体文件 / 模块作为佐证。",
            "",
            "要求：",
            "- 不要输出 # 顶级标题，不要写『本周代码质量全量扫描』标题（渲染层已加）。",
            "- markdown 内换行用 \\n 转义。",
            "- 不要编造数据里没有的文件或问题；基于我给的数据发挥即可。",
            "- 行内代码用反引号包裹（文件 / 函数 / 模块名）。",
            "",
            f"数据范围：{ws} ~ {we}，目标分支 `{target_branch}`。",
            f"本周合并 MR 数：{total_mrs}，去重变更文件数：{total_files}，新增 {total_additions} 行，删除 {total_deletions} 行。",
            "",
            "MR 摘要（iid | 标题 | 作者 | 文件数 | +行数 | -行数）：",
        ]
        for m in mr_summaries[:30]:
            lines.append(
                f"- !{m['iid']} {m['title']} | {m['author']} | "
                f"文件 {m['files']} | +{m['additions']} -{m['deletions']}"
            )

        lines.extend(["", "文件热力图（按总变更行数排序）："])
        if top_files:
            for f in top_files[: self.MAX_FILES_IN_HEATMAP]:
                tag = []
                if f.get("is_new"):
                    tag.append("新增")
                if f.get("is_deleted"):
                    tag.append("删除")
                if f.get("is_renamed"):
                    tag.append("重命名")
                if f["change_count"] > 1:
                    tag.append(f"被 {f['change_count']} 个 MR 改动")
                tag_str = f" [{' / '.join(tag)}]" if tag else ""
                lines.append(
                    f"- `{f['path']}`{tag_str} | +{f['additions']} -{f['deletions']}"
                )
        else:
            lines.append("- (无)")

        # 关键 diff 片段
        snippets = self._select_diff_snippets(file_changes, top_files)
        if snippets:
            lines.extend(["", "关键 diff 片段（用于全局判断，可能已截断）："])
            for path, diff in snippets:
                lines.append(f"\n### `{path}`\n```diff\n{diff}\n```")

        # 上周对比
        if prev_data:
            lines.append("")
            lines.append("上周对比数据（仅用于趋势语感，不要编造）：")
            lines.append(f"- 上周合并 MR 数：{prev_data.get('total_mrs', '?')}")
            lines.append(f"- 上周去重变更文件数：{prev_data.get('total_files', '?')}")
            lines.append(
                f"- 上周新增/删除行数：+{prev_data.get('total_additions', '?')} "
                f"-{prev_data.get('total_deletions', '?')}"
            )

        return "\n".join(lines)

    def _generate_llm_review(
        self,
        *,
        target_branch: str,
        week_start: datetime,
        week_end: datetime,
        total_mrs: int,
        total_files: int,
        total_additions: int,
        total_deletions: int,
        top_files: list[dict[str, Any]],
        mr_summaries: list[dict[str, Any]],
        file_changes: dict[str, dict[str, Any]],
        prev_data: dict[str, Any] | None = None,
    ) -> str:
        """调 opencode weekly_quality_scan agent 生成综述；失败抛异常由调用方回退."""
        prompt = self._build_llm_prompt(
            target_branch=target_branch,
            week_start=week_start,
            week_end=week_end,
            total_mrs=total_mrs,
            total_files=total_files,
            total_additions=total_additions,
            total_deletions=total_deletions,
            top_files=top_files,
            mr_summaries=mr_summaries,
            file_changes=file_changes,
            prev_data=prev_data,
        )
        oc_result = opencode.run(
            agent="weekly_quality_scan",
            prompt=prompt,
            workdir=Path.cwd(),
            files=[],
            timeout=max(120, int(config.rq_worker_timeout * 0.4)),
            tolerant_markdown=True,
        )
        return (oc_result.data or {}).get("markdown", "") or ""

    @staticmethod
    def _render_markdown(
        *,
        target_branch: str,
        week_start: datetime,
        week_end: datetime,
        total_mrs: int,
        total_files: int,
        total_additions: int,
        total_deletions: int,
        top_files: list[dict[str, Any]],
        mr_summaries: list[dict[str, Any]],
    ) -> str:
        """确定性回退：基于本周 MR 变更热力图生成 4 小节."""
        ws = week_start.strftime("%Y-%m-%d")
        we = week_end.strftime("%Y-%m-%d")
        lines: list[str] = [
            f"本周 (`{ws}` ~ `{we}`) 合并到 `{target_branch}` 的 MR 共 **{total_mrs}** 个，"
            f"去重变更文件 **{total_files}** 个，新增 **{total_additions}** 行、删除 **{total_deletions}** 行。"
            "以下基于变更热力图做全局判断：",
            "",
            "**高风险模块**",
            "",
        ]
        core_changed = [f for f in top_files if RepoScanCollector._is_core_path(f["path"])]
        if core_changed:
            lines.append("本周核心模块变动较为集中，需重点关注以下文件：")
            for f in core_changed[:5]:
                tag = []
                if f.get("is_new"):
                    tag.append("新增")
                if f.get("is_deleted"):
                    tag.append("删除")
                if f["change_count"] > 1:
                    tag.append(f"被 {f['change_count']} 个 MR 改动")
                tag_str = f" [{' / '.join(tag)}]" if tag else ""
                lines.append(
                    f"- `{f['path']}`{tag_str} | +{f['additions']} -{f['deletions']}"
                )
        else:
            lines.append("- 本周核心模块无显著变更，风险较分散。")
        lines.append("")

        lines.append("**新增坏味道**")
        lines.append("")
        new_or_deleted = [f for f in top_files if f.get("is_new") or f.get("is_deleted")]
        if new_or_deleted:
            lines.append("- 以下文件为新增或删除，可能引入结构性变化，建议全局审视：")
            for f in new_or_deleted[:5]:
                kind = "新增" if f.get("is_new") else "删除"
                lines.append(f"  - `{f['path']}` ({kind}, +{f['additions']} -{f['deletions']})")
        else:
            lines.append("- 本周以修改现有文件为主，未观察到显著的新增/删除模块。")
        lines.append("")

        lines.append("**测试覆盖与可靠性**")
        lines.append("")
        test_files = [f for f in top_files if "test" in f["path"].lower() or "spec" in f["path"].lower()]
        if test_files:
            paths = ", ".join(f"`{f['path']}`" for f in test_files[:3])
            lines.append(
                f"- 本周测试相关文件变更 {len(test_files)} 处，"
                f"主要涉及：{paths}。"
                "建议确认核心逻辑改动是否都有对应用例覆盖。"
            )
        else:
            lines.append("- 本周高风险模块未直接命中测试目录，建议下周期检查核心逻辑是否补充测试。")
        lines.append("")

        lines.append("**建议跟进**")
        lines.append("")
        if total_additions + total_deletions > 1000:
            lines.append("- 本周代码变动量较大，建议对核心模块做专项 review，避免跨文件破坏。")
        if core_changed:
            lines.append("- 关注核心模块的接口签名与依赖变化，补充集成测试与回归用例。")
        lines.append("- 下周期统计新增坏味道的趋势，必要时沉淀到 AGENTS.md 规范。")
        lines.append("")

        return "\n".join(lines).rstrip() + "\n"


__all__ = ["RepoScanCollector"]
