"""Layer-C1 collector: 本周代码质量扫描 (确定性 / 规则引擎版).

参考 pr_agent ``pr_agent/reporting/collectors/repo_scan.py`` 的设计 (clone+diff+LLM).
本实现因为当前 opencode 客户端要求 workdir, 走另一条路:

1. 从 GitLab `mr.changes()` 拉本周合并 MR 的 diff (文本),
2. 解析 diff 拿到文件级 +/-, 选变更最密集的 N 个文件,
3. 结合 telemetry 的 suggestion_metrics / rule_key_counts,
4. 给一段 deterministic 报告, 含 SSD 维度相关 bullet.

输出 SectionResult.data:
    target_branch    : 目标分支
    diff_stats       : {commits, files_changed, additions, deletions, mr_count}
    high_risk_files  : [{path, additions, deletions, reason}]
    code_smells      : [bullet dict {rule_key, count, severity}]
    top_rules        : [(rule_key, count), ...]
    llm_review_markdown : 渲染好的 markdown 报告 (确定性)
    truncated        : bool
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from reviewagent.gitlab.client import GitLabError, client as gl
from reviewagent.logging_setup import logger
from reviewagent.telemetry.store import get_store

from .base import CollectorContext, SectionResult


# diff 一行匹配 (+x / -x / " 文件 changed, X insertions, Y deletions")
_INSERTION_RE = re.compile(r"(\d+) insertion")
_DELETION_RE = re.compile(r"(\d+) deletion")
_FILE_CHANGED_RE = re.compile(r"^\s*(.+?)\s+\|\s+(\d+)\s*([+-]*)")
_SUMMARY_RE = re.compile(r"(\d+)\s+files?\s+changed")


def _parse_diff_stats(diff_text: str | None) -> tuple[int, int, int]:
    """从 ``git diff --stat`` 输出解析 (files_changed, additions, deletions)."""
    if not diff_text:
        return 0, 0, 0
    adds = dels = 0
    files: set[str] = set()
    for line in diff_text.splitlines():
        s = _SUMMARY_RE.search(line)
        if s:
            files_cnt = int(s.group(1))
            for ln in diff_text.splitlines():
                m = _FILE_CHANGED_RE.match(ln)
                if m and m.group(1) and m.group(1) != "-":
                    files.add(m.group(1).strip())
            return max(files_cnt, len(files)), adds, dels
        m = _FILE_CHANGED_RE.match(line)
        if m and m.group(1) and m.group(1) != "-":
            files.add(m.group(1).strip())
            adds += len(m.group(3).replace("-", ""))
            dels += len(m.group(3).replace("+", ""))
    return len(files), adds, dels


def _high_risk_files_from_diff(diff_text: str | None) -> list[dict[str, Any]]:
    """从 diff 提取高变更文件 (按 +/- 总和排序)."""
    if not diff_text:
        return []
    rows: list[dict[str, Any]] = []
    for line in diff_text.splitlines():
        m = _FILE_CHANGED_RE.match(line)
        if not m or m.group(1) == "-":
            continue
        path = m.group(1).strip()
        # m.group(3) 是 '+' / '-' 字符序列 (git diff --stat 风格)
        delta_marker = m.group(3)
        a = delta_marker.count("+")
        d = delta_marker.count("-")
        if a + d == 0:
            continue
        rows.append({"path": path, "additions": a, "deletions": d})
    rows.sort(key=lambda r: -(r["additions"] + r["deletions"]))
    return rows[:5]


class RepoScanCollector:
    """本周代码质量扫描 (确定版, 不调 LLM)."""

    name: str = "repo_scan"

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

        # 1. 拉本周合并 MR 列表 (用于拿 diff / 选高风险文件)
        try:
            mrs = gl.list_project_mrs(
                project_id,
                state="merged",
                updated_after=week_start.isoformat(),
                updated_before=week_end.isoformat(),
                per_page=100,
            )
        except GitLabError as e:
            logger.warning("repo_scan: list_project_mrs failed: {}", e)
            mrs = []

        # 时间窗内 merged_at 过滤
        ws_ts = week_start.timestamp()
        we_ts = week_end.timestamp()
        in_window = []
        for m in mrs:
            merged_at = m.get("merged_at")
            if not merged_at:
                continue
            try:
                ts = datetime.fromisoformat(merged_at.replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
            if ws_ts <= ts < we_ts:
                in_window.append(m)

        # 2. 逐 MR 拉 diff (限速: 只取前 10 个 MR)
        combined_diff = ""
        added = 0
        deleted = 0
        files_count = 0
        for mr in in_window[:10]:
            iid = mr.get("iid")
            try:
                diff = gl.get_mr_diff(project_id, iid)
                # 解析 stat
                fc, a, d = _parse_diff_stats(diff)
                added += a
                deleted += d
                files_count += fc
                combined_diff += diff + "\n"
            except GitLabError as e:
                logger.debug("repo_scan: get_mr_diff !%s failed: {}", iid, e)

        # 3. 高风险文件 (按 +/- 总和)
        high_risk = _high_risk_files_from_diff(combined_diff)

        # 4. 规则层 — 从 telemetry 取本窗口 suggestion 触发最多的规则
        store = get_store()
        sev = store.suggestion_metrics(
            project_id=project_id, since=week_start.isoformat(), until=week_end.isoformat(),
        )
        top_rules = store.rule_key_counts(
            project_id=project_id, since=week_start.isoformat(), until=week_end.isoformat(),
            top_n=5,
        )
        severity = sev.get("severity_counts", {})

        # 5. 渲染报告 (deterministic markdown)
        md = self._render_markdown(
            target_branch=target_branch,
            week_start=week_start, week_end=week_end,
            mr_count=len(in_window),
            files_changed=files_count, additions=added, deletions=deleted,
            high_risk=high_risk,
            severity=severity, top_rules=top_rules,
        )

        return SectionResult(
            status="ok",
            data={
                "target_branch": target_branch,
                "diff_stats": {
                    "commits": len(in_window),
                    "files_changed": files_changed if False else files_count,
                    "additions": added, "deletions": deleted,
                    "mr_count": len(in_window),
                },
                "high_risk_files": high_risk,
                "code_smells": [{"rule_key": r, "count": c} for r, c in top_rules],
                "top_rules": top_rules,
                "severity": severity,
                "llm_review_markdown": md,
                "truncated": False,
            },
            markdown=md,
            meta={"mr_in_window": len(in_window)},
        )

    @staticmethod
    def _render_markdown(
        *,
        target_branch: str,
        week_start: datetime, week_end: datetime,
        mr_count: int, files_changed: int, additions: int, deletions: int,
        high_risk: list[dict[str, Any]],
        severity: dict[str, int],
        top_rules: list[tuple[str, int]],
    ) -> str:
        ws = week_start.strftime("%Y-%m-%d")
        we = week_end.strftime("%Y-%m-%d")
        lines: list[str] = []
        lines.append(f"本周 (`{ws}` ~ `{we}`) 合并到 `{target_branch}` 的 MR 共 **{mr_count}** 个, "
                     f"覆盖 **{files_changed}** 个文件, 新增代码 **{additions}** 行, "
                     f"删除 **{deletions}** 行。\n")

        # 高风险模块
        lines.append("**高风险模块**")
        if high_risk:
            for f in high_risk[:5]:
                lines.append(f"- `{f['path']}` (变更 +{f['additions']}/-{f['deletions']})")
        else:
            lines.append("- (本周 diff 不涉及明显高风险模块)")
        lines.append("")

        # 新增坏味道 (按规则聚合)
        lines.append("**新增坏味道**")
        if top_rules:
            for rule_key, count in top_rules[:5]:
                lines.append(f"- `{rule_key}` × **{count}** (按 suggestion 聚合)")
        else:
            lines.append("- (本周无规则连续触发, 检视质量稳定)")
        lines.append("")

        # 测试覆盖与可靠性
        lines.append("**测试覆盖与可靠性**")
        if mr_count > 0:
            test_files = [f for f in high_risk if "test" in f["path"].lower()]
            if test_files:
                lines.append(f"- 本周改动覆盖测试目录的有 {len(test_files)} 处: "
                             + ", ".join(f"`{f['path']}`" for f in test_files[:3]))
            else:
                lines.append("- 本周无直接命中测试目录 (`test*/spec*`) 的改动, 建议下周期补")
        else:
            lines.append("- 本周无合并 MR, 跳过可靠性评估")
        lines.append("")

        # 建议跟进
        lines.append("**建议跟进**")
        if severity:
            hi = severity.get("high", 0)
            crit = severity.get("critical", 0)
            if hi + crit > 0:
                lines.append(f"- 本周新增 **{hi + crit}** 条 high/critical 严重度建议, "
                             "请优先 review 采纳或显式 dismiss")
        if top_rules and top_rules[0][1] >= 3:
            lines.append(f"- 规则 `{top_rules[0][0]}` 本周触发 {top_rules[0][1]} 次, "
                         "建议团队沉淀进 AGENTS.md 防再犯")
        if not high_risk and mr_count == 0:
            lines.append("- 本周交付较静默, 仍可考虑下周期规划少量专项检视")
        lines.append("")

        return "\n".join(lines).rstrip() + "\n"


__all__ = ["RepoScanCollector"]
