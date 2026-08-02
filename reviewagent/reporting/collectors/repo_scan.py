"""Layer-C1 collector: 本周代码质量全量扫描 (独立于本周检视概况的全局体检; 确定性 / 规则引擎版).

设计:
- 不再拉 MR 的 unified diff 去解析行数 (GitLab 返回 unified diff, 旧的正则按
  `git diff --stat` 格式写, 永远解析出 0 文件 — 已废弃).
- 改为直接从 telemetry 的 `suggestions` 真实检视产出按文件聚合:
    * 高风险模块 = 本周窗口内 high/critical 严重度建议最多的文件 (带代表摘要)
    * 新增坏味道 = 触发最多的规则 (rule_key_counts)
- 这样数据源就是"实际被 agent 标出的问题", 比解析 diff 更准, 也更贴近人读的报告.

输出 SectionResult.data:
    target_branch    : 目标分支
    high_risk_files  : [{path, high, critical, total, summary}]
    code_smells      : [{rule_key, count}]
    top_rules        : [(rule_key, count), ...]
    severity         : {high: N, medium: M, ...}
    suggestion_total : 本周窗口 suggestion 总数
    llm_review_markdown : 渲染好的 markdown 报告 (确定性)
    truncated        : bool
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone as _tz
from pathlib import Path
from typing import Any

from reviewagent.config import config
from reviewagent.logging_setup import logger
from reviewagent.opencode.client import client as opencode
from reviewagent.telemetry.store import get_store

from .base import CollectorContext, SectionResult
from ..rule_translate import RuleNameResolver


class RepoScanCollector:
    """本周代码质量全量扫描 (确定性, 基于 suggestions 聚合; 独立于本周检视概况)."""

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

        # SQLite 比较 created_at 用字典序, 统一转 UTC 再传 (与 telemetry 一致)
        since_iso = week_start.astimezone(_tz.utc).isoformat()
        until_iso = week_end.astimezone(_tz.utc).isoformat()

        store = get_store()
        # 本周窗口内的 suggestion (真实检视产出)
        suggestions = store.list_suggestions(
            project_id=project_id, since=since_iso, until=until_iso, limit=100000,
        )

        # 按文件聚合: 计数 + high/critical 数 + 代表摘要 + 规则频次
        by_file: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"count": 0, "high": 0, "critical": 0,
                     "summaries": [], "rules": defaultdict(int)}
        )
        for s in suggestions:
            fp = s.get("file_path") or "(未知文件)"
            rec = by_file[fp]
            rec["count"] += 1
            sev = (s.get("severity") or "").lower()
            if sev == "high":
                rec["high"] += 1
            elif sev == "critical":
                rec["critical"] += 1
            summ = s.get("one_sentence_summary") or s.get("header") or ""
            if summ and summ not in rec["summaries"]:
                rec["summaries"].append(summ)
            for r in (s.get("rule_keys") or "").split(","):
                r = r.strip()
                if r:
                    rec["rules"][r] += 1

        # 高风险模块: 按 critical*2 + high 加权排序, 取前 5
        ranked = sorted(
            by_file.items(),
            key=lambda kv: -(kv[1]["critical"] * 2 + kv[1]["high"]),
        )[:5]
        high_risk: list[dict[str, Any]] = []
        for fp, rec in ranked:
            rep = rec["summaries"][0] if rec["summaries"] else ""
            high_risk.append({
                "path": fp,
                "high": rec["high"],
                "critical": rec["critical"],
                "total": rec["count"],
                "summary": rep,
            })

        # 规则层 + 严重度层
        top_rules = store.rule_key_counts(
            project_id=project_id, since=since_iso, until=until_iso, top_n=5,
        )
        sev = store.suggestion_metrics(
            project_id=project_id, since=since_iso, until=until_iso,
        ).get("severity_counts", {})
        severity = {k: v for k, v in sev.items() if k != "unspecified"}
        if "unspecified" in sev:
            severity["other"] = sev["unspecified"]

        # 规则 key 翻译为可读类别名 (动态解析 .agents/rules + 提示词模板), 与本周检视汇总一致
        resolver = RuleNameResolver.from_repo(project_id)
        top_rules_friendly = [(resolver.translate(rk), n) for rk, n in top_rules]

        deterministic_md = self._render_markdown(
            target_branch=target_branch,
            week_start=week_start, week_end=week_end,
            high_risk=high_risk, severity=severity,
            top_rules=top_rules, top_rules_friendly=top_rules_friendly,
            suggestion_total=len(suggestions),
        )

        # 调 opencode 生成"代码质量扫描"综述 (让 LLM 自由发挥, 不限制规则命中)
        # 失败则回退到确定性 deterministic_md, 保证周报不崩
        prev_repo = (ctx.prev_data.get("repo_scan") or {}) if ctx.prev_data else {}
        llm_md = ""
        try:
            llm_md = self._generate_llm_review(
                target_branch=target_branch,
                week_start=week_start, week_end=week_end,
                high_risk=high_risk, severity=severity,
                top_rules=top_rules, top_rules_friendly=top_rules_friendly,
                suggestion_total=len(suggestions),
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
                "high_risk_files": high_risk,
                "code_smells": [{"rule_key": r, "count": c} for r, c in top_rules],
                "top_rules": top_rules,
                "severity": severity,
                "suggestion_total": len(suggestions),
                "llm_review_markdown": llm_md,
                "truncated": False,
            },
            markdown=llm_md,
            meta={"suggestion_total": len(suggestions),
                  "high_risk_files": len(high_risk)},
        )

    @staticmethod
    def _build_llm_prompt(
        *,
        target_branch: str,
        week_start: datetime, week_end: datetime,
        high_risk: list[dict[str, Any]],
        severity: dict[str, int],
        top_rules: list[tuple[str, int]],
        top_rules_friendly: list[tuple[str, int]],
        suggestion_total: int,
        prev_data: dict[str, Any] | None = None,
    ) -> str:
        """构造发给 weekly_quality_scan agent 的 user prompt (聚合数据).

        定位: 独立于『本周检视概况』的全量代码体检, 从整个项目出发看本周所有代码变动。
        输出固定 4 小节: 高风险模块 / 新增坏味道 / 测试覆盖与可靠性 / 建议跟进。
        """
        ws = week_start.strftime("%Y-%m-%d")
        we = week_end.strftime("%Y-%m-%d")
        lines: list[str] = [
            "你是代码质量分析师。下面是本周代码检视的真实聚合数据，请做一次**全量代码体检**。",
            "",
            "【定位说明】这是独立于『本周检视概况』的全量代码扫描：后者是逐 MR 的 diff 级检视汇总（只看改动行），"
            "本段从整个项目出发，对本周**所有代码变动**做一次全局体检，弥补 diff 检视只看局部、看不到跨文件影响与全局趋势的劣势。",
            "",
            "请输出一段『本周代码质量全量扫描』综述，严格按以下 4 个小节组织"
            "（用 **加粗** 作小标题，顺序不变，小节之间空一行）：",
            "**高风险模块** —— 从整体项目视角，指出本周风险最集中 / 最该关注的模块与文件，说明为什么"
            "（结合 high/critical 严重度、改动面、模块间耦合与影响半径），不要逐条罗列 diff。",
            "**新增坏味道** —— 本周新出现或反复出现的代码坏味道；从全局看哪些趋势值得警惕（不要只报规则命中次数）。",
            "**测试覆盖与可靠性** —— 本周改动是否有对应测试覆盖、异常路径是否验证、可靠性风险（资源泄漏 / 并发 / 超时 / 幂等等）如何。",
            "**建议跟进** —— 具体、可执行的跟进项（补哪些测试、沉淀哪些规范、重构优先级）。",
            "",
            "要求：严格输出 JSON：{\"markdown\": \"...\"}, markdown 用中文，可含 bullet，不要用 # 顶级标题，"
            "不要写『本周代码质量全量扫描』标题（渲染层已加）。markdown 内换行用 \\n 转义。"
            "不要机械罗列 R-XXX 原始规则键，规则命中只是信号，重点是你的判断。数字准确使用我给的数据，不要编造。",
            "",
            f"数据范围：{ws} ~ {we}，目标分支 `{target_branch}`，本周共 {suggestion_total} 条检视建议"
            "（全量视角，覆盖本周所有代码变动）。",
            "",
            "高风险模块（按 high/critical 加权）：",
        ]
        if high_risk:
            for f in high_risk:
                badge: list[str] = []
                if f["critical"]:
                    badge.append(f"critical {f['critical']}")
                if f["high"]:
                    badge.append(f"high {f['high']}")
                badge_str = f"（{'、'.join(badge)}）" if badge else ""
                summ = f.get("summary", "")
                lines.append(f"- `{f['path']}`{badge_str}: {summ}")
        else:
            lines.append("- (无 high/critical)")
        lines.append("")
        sev_str = "、".join(f"{k}={v}" for k, v in severity.items()) if severity else "(无)"
        lines.append(f"严重度分布：{sev_str}")
        lines.append("")
        lines.append("高频触发的规则（已转为可读类别名，可能为中文 / 英文描述；不要回退到原始机器 key）：")
        if top_rules_friendly:
            for rk, n in top_rules_friendly[:8]:
                lines.append(f"- {rk} × {n}")
        else:
            lines.append("- (无)")
        # 上周对比数据 (给 LLM 真实趋势基准, 避免编造)
        if prev_data:
            lines.append("")
            lines.append("上周对比数据（用于趋势判断，不要编造）：")
            lines.append(f"- 上周 suggestion 总数：{prev_data.get('suggestion_total', '?')}")
            prev_sev = prev_data.get("severity") or {}
            if prev_sev:
                lines.append(f"- 上周严重度分布：{', '.join(f'{k}={v}' for k, v in prev_sev.items())}")
            prev_hr = prev_data.get("high_risk_files") or []
            if prev_hr:
                lines.append("- 上周高风险模块：")
                for f in prev_hr[:3]:
                    lines.append(f"  - `{f.get('path', '?')}` (high={f.get('high', 0)}, critical={f.get('critical', 0)})")
        return "\n".join(lines)

    def _generate_llm_review(
        self,
        *,
        target_branch: str,
        week_start: datetime, week_end: datetime,
        high_risk: list[dict[str, Any]],
        severity: dict[str, int],
        top_rules: list[tuple[str, int]],
        top_rules_friendly: list[tuple[str, int]],
        suggestion_total: int,
        prev_data: dict[str, Any] | None = None,
    ) -> str:
        """调 opencode weekly_quality_scan agent 生成综述；失败抛异常由调用方回退."""
        prompt = self._build_llm_prompt(
            target_branch=target_branch, week_start=week_start, week_end=week_end,
            high_risk=high_risk, severity=severity, top_rules=top_rules,
            top_rules_friendly=top_rules_friendly,
            suggestion_total=suggestion_total, prev_data=prev_data,
        )
        oc_result = opencode.run(
            agent="weekly_quality_scan",
            prompt=prompt,
            workdir=Path.cwd(),
            files=[],
            timeout=max(120, int(config.rq_worker_timeout * 0.4)),
        )
        return (oc_result.data or {}).get("markdown", "") or ""

    @staticmethod
    def _render_markdown(
        *,
        target_branch: str,
        week_start: datetime, week_end: datetime,
        high_risk: list[dict[str, Any]],
        severity: dict[str, int],
        top_rules: list[tuple[str, int]],
        top_rules_friendly: list[tuple[str, int]] | None = None,
        suggestion_total: int,
    ) -> str:
        ws = week_start.strftime("%Y-%m-%d")
        we = week_end.strftime("%Y-%m-%d")
        lines: list[str] = []
        lines.append(
            f"本周 (`{ws}` ~ `{we}`) 共产生 **{suggestion_total}** 条检视建议, "
            f"以下按文件聚合的高风险模块与高频触发规则:\n"
        )

        # 高风险模块 (不重复 merged_mrs 的 MR 计数, 直接切入文件级风险)
        lines.append("**高风险模块**")
        if high_risk:
            for f in high_risk:
                badge: list[str] = []
                if f["critical"]:
                    badge.append(f"critical {f['critical']}")
                if f["high"]:
                    badge.append(f"high {f['high']}")
                badge_str = f"（{'、'.join(badge)}）" if badge else ""
                lines.append(f"- `{f['path']}`{badge_str}")
                if f["summary"]:
                    lines.append(f"  - {f['summary']}")
        else:
            lines.append("- (本周无 high/critical 严重度建议)")
        lines.append("")

        # 新增坏味道 (按规则聚合, 用翻译后的可读类别名)
        lines.append("**新增坏味道**")
        friendly = top_rules_friendly or [(rk, n) for rk, n in top_rules]
        if friendly:
            for name, count in friendly[:5]:
                lines.append(f"- {name} × **{count}**")
        else:
            lines.append("- (本周无规则连续触发, 检视质量稳定)")
        lines.append("")

        # 测试覆盖与可靠性
        lines.append("**测试覆盖与可靠性**")
        test_files = [f["path"] for f in high_risk
                      if "test" in f["path"].lower() or "spec" in f["path"].lower()]
        if test_files:
            lines.append(
                f"- 本周高风险改动命中测试目录的有 {len(test_files)} 处: "
                + ", ".join(f"`{p}`" for p in test_files[:3])
            )
        else:
            lines.append("- 本周高风险模块未直接命中测试目录 (`test*/spec*`), 建议下周期补测试")
        lines.append("")

        # 建议跟进
        lines.append("**建议跟进**")
        hi = severity.get("high", 0)
        crit = severity.get("critical", 0)
        if hi + crit > 0:
            lines.append(f"- 本周新增 **{hi + crit}** 条 high/critical 严重度建议, "
                         "请优先 review 采纳或显式 dismiss")
        if top_rules and top_rules[0][1] >= 3:
            lines.append(f"- 规则 `{top_rules[0][0]}` 本周触发 {top_rules[0][1]} 次, "
                         "建议团队沉淀进 AGENTS.md 防再犯")
        lines.append("")

        return "\n".join(lines).rstrip() + "\n"


__all__ = ["RepoScanCollector"]
