"""周报 markdown 渲染 — pr-agent 风格 3 段布局.

参考: pr_agent/reporting/renderer.py

布局:
    # TITLE
    > 生成时间 / 数据范围

    ## 一、本周检视概况                (telemetry section)
        | 指标 | 数值 | 表格 ...

    ## 二、本周 {branch} 变更汇总      (merged_mrs section)
        head_line (总览) +
        变更摘要 (LLM 或本系统拼接) +
        涉及 MR 列表

    ## 三、本周代码质量全量扫描        (repo_scan section)
        高风险模块 / 新增坏味道 / 测试覆盖与可靠性 / 建议跟进

每段标题用 `##` 而 LLM 可能产出 `#` — 渲染前 demote.
"""
from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from .artifact import WeeklyArtifact
from .collectors.base import SectionResult
from .rule_translate import translate_rule_key


SECTION_TITLES: dict[str, str] = {
    "telemetry":   "一、本周检视概况",
    "merged_mrs":  "二、本周 {branch} 变更汇总",
    "repo_scan":   "三、本周代码质量全量扫描",
}


_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$")


def section_title(name: str, fallback: str | None = None) -> str:
    return SECTION_TITLES.get(name, fallback or name)


def _demote_llm_headings(md: str | None) -> str:
    """LLM 输出里 `#` / `##` 标题降级为 `**粗体**`, DingTalk 渲染时不会被放大."""
    if not md:
        return md or ""
    out: list[str] = []
    for line in md.splitlines():
        m = _HEADING_RE.match(line.rstrip())
        if not m:
            out.append(line)
            continue
        title = m.group(1).strip()
        out.append(f"**{title}**")
    return "\n".join(out)


def _strip_leading_section_header(md: str | None, label: str) -> str:
    """去掉 LLM 自带、与渲染层重复的节标题（如『变更摘要』『本周代码质量全量扫描』）.

    LLM 有时仍会输出标题，这里防御性剥离首行, 让渲染层统一加标题, 避免重复.
    """
    if not md:
        return md or ""
    lines = md.splitlines()
    if not lines:
        return md
    first = lines[0].strip()
    if (re.match(rf"^#+\s*{label}\s*$", first)
            or first == f"**{label}**"
            or first == label):
        return "\n".join(lines[1:]).lstrip("\n")
    return md


def _wrap(s: str, width: int = 22) -> str:
    """长字符串按 word boundary 切, 用 `<br>` 拼回去 — DingTalk 单元格不会自动 wrap.

    对 CJK (无空格) 按字符宽度强制截断.
    """
    if not s:
        return ""
    s = s.replace("|", "\\|").replace("\n", " ")
    if len(s) <= width:
        return s
    # 先按空格分词 (英文), 再对超长 token (CJK) 按字符截断
    words = s.split(" ")
    out: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for w in words:
        # 单个 token 超过 width (通常是 CJK 无空格串), 按字符硬切
        while len(w) > width:
            if cur:
                out.append(" ".join(cur))
                cur = []
                cur_len = 0
            out.append(w[:width])
            w = w[width:]
        if not w:
            continue
        if not cur:
            cur = [w]
            cur_len = len(w)
            continue
        if cur_len + 1 + len(w) > width:
            out.append(" ".join(cur))
            cur = [w]
            cur_len = len(w)
        else:
            cur.append(w)
            cur_len += 1 + len(w)
    if cur:
        out.append(" ".join(cur))
    return "<br>".join(out)


_NATURE_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("修复", ("fix", "修复", "bug", "hotfix", "缺陷")),
    ("新增", ("feat", "feature", "新增", "add", "实现", "支持", "introduce")),
    ("重构", ("refactor", "重构", "restructure", "重命名", "rename")),
    ("性能", ("perf", "性能", "optimize", "优化", "加速")),
    ("测试", ("test", "测试", "spec", "用例")),
    ("文档", ("doc", "文档", "readme", "comment", "注释")),
    ("杂项", ("chore", "ci", "build", "deps", "依赖", "bump", "配置", "config")),
    ("风格", ("style", "format", "格式", "lint")),
]


def _infer_nature(title: str) -> str:
    """从 MR 标题推断变更性质 (确定性, 不调 LLM)."""
    t = (title or "").lower()
    for label, keys in _NATURE_KEYWORDS:
        if any(k in t for k in keys):
            return label
    return "其他"


def _build_change_summary(mr_list: list[dict], author_count: int) -> str:
    """确定性变更概要（与 LLM 同款格式）: 概述 + 新增/修改/删除 归类，不 @ 作者."""
    if not mr_list:
        return ""
    # 按 新增 / 修改 / 删除 三段归类
    groups: dict[str, list[str]] = {"新增": [], "修改": [], "删除": []}
    nature_counter: dict[str, int] = {}
    for m in mr_list:
        title = (m.get("title") or "").replace("|", "\\|").replace("\n", " ")
        nature = _infer_nature(m.get("title", ""))
        nature_counter[nature] = nature_counter.get(nature, 0) + 1
        if nature == "新增":
            groups["新增"].append(title)
        elif nature == "删除":
            groups["删除"].append(title)
        else:
            groups["修改"].append(title)

    parts = [
        "概述",
        f"本周共合并 **{len(mr_list)}** 个 MR，涉及 **{author_count}** 位作者。",
    ]
    if nature_counter:
        desc = "、".join(f"{k} {v} 个" for k, v in sorted(nature_counter.items(), key=lambda x: -x[1]))
        parts.append(f"按性质看：{desc}。")
    parts.append("")
    for label in ("新增", "修改", "删除"):
        items = groups[label]
        if not items:
            continue
        parts.append(label)
        for t in items[:8]:
            parts.append(f"- {t}")
        parts.append("")
    return "\n".join(parts).strip()


def render_section(name: str, sr: SectionResult) -> str:
    """单个 section -> markdown 块 (不含顶层 ## 标题)."""
    if sr.status == "failed":
        err = sr.error or "未知错误"
        return f"> ⚠️ 数据采集失败: `{err}`\n"

    if name == "telemetry":
        return _render_telemetry(sr.data or {})
    if name == "merged_mrs":
        return _render_merged_mrs(sr.data or {})
    if name == "repo_scan":
        return _render_repo_scan(sr.data or {}, sr.markdown)
    # 兜底
    return "```json\n" + str(sr.data)[:1000] + "\n```"


def _fmt_delta(delta, suffix: str = "", invert_good: bool = False) -> str:
    """格式化环比 delta 为带箭头的短串; None 返回空串."""
    if delta is None:
        return ""
    arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "→")
    val = f"{arrow}{abs(delta)}{suffix}"
    return val


def _render_telemetry(d: dict[str, Any]) -> str:
    """检视概况: 叙事性检视汇总(LLM 润色) 在上, 核心指标表在下.

    - 汇总段优先用 opencode 生成的 llm_summary_markdown; 为空(LLM 失败/未配置)
      回退到确定性 _build_inspection_summary.
    - 指标表仅保留 5 行核心指标, 不再堆 severity 分布 / 触发最多规则 两行裸数据.
    """
    mr_count = d.get("mr_count", 0)
    mr_total = d.get("mr_total", 0)
    suggestion_count = d.get("suggestion_count", 0)
    suggestion_total = d.get("suggestion_total", suggestion_count)
    adoption_rate = round(float(d.get("adoption_rate", 0)) * 100, 1)
    deltas = d.get("deltas") or {}

    # 汇总段: LLM 润色版优先, 否则确定性兜底
    llm_summary = (d.get("llm_summary_markdown") or "").strip()
    if llm_summary:
        summary = _strip_leading_section_header(
            _demote_llm_headings(llm_summary), "本周检视汇总"
        )
    else:
        summary = _build_inspection_summary(d)

    lines: list[str] = []
    if summary:
        lines.append("**本周检视汇总**")
        lines.append("")
        lines.append(summary.strip())
        lines.append("")

    def _row(label: str, value: str, delta=None, suffix: str = "") -> tuple[str, str]:
        if delta is not None and delta != 0:
            value = f"{value} ({_fmt_delta(delta, suffix)})"
        elif delta == 0:
            value = f"{value} (→)"
        return (label, value)

    rows: list[tuple[str, str]] = [
        _row("本周窗口 MR 数", str(mr_count), deltas.get("mr_count")),
        ("项目累计 MR 数", str(mr_total)),
        _row("本周 suggestion 数", str(suggestion_count), deltas.get("suggestion_count")),
        ("累计 suggestion 数", str(suggestion_total)),
        _row("累计采纳率", f"{adoption_rate}%", deltas.get("adoption_rate_pct"), "pp"),
    ]
    lines.append("| 指标 | 数值 |")
    lines.append("|---|---|")
    for label, value in rows:
        lines.append(f"| {label} | {value} |")

    return "\n".join(lines) + "\n"


def _categorize(friendly: str, rule_key: str) -> str:
    """把一条规则归类: 正确性/稳定性类(优先) 或 代码规范类. 仅用于确定性兜底."""
    nl = (friendly or "").lower()
    key = rule_key or ""
    # 通用/跨文件规则前缀基本都是正确性类
    if key.startswith(("R-OTHER-IMPACT", "R-LOOP", "R-RES", "R-SHELL", "R-ERR")):
        return "正确性/稳定性类"
    correctness_kw = ("接口", "参数", "循环", "资源", "内存", "泄漏", "注入",
                      "异常", "未释放", "并发", "竞态", "运行", "行为",
                      "caller", "loop", "res", "shell")
    if any(k in nl for k in correctness_kw):
        return "正确性/稳定性类"
    return "代码规范类"


def _build_inspection_summary(d: dict[str, Any]) -> str:
    """本周检视汇总(确定性兜底): 固定三小节 **概述** / **问题类型** / **跟进建议**.

    与 opencode 生成的 llm_summary_markdown 使用同一套固定排版, 保证周报布局稳定.
    正常链路由 LLM 产出; 仅当 LLM 不可用/失败才走此处.
    """
    sev = d.get("severity_breakdown") or {}
    # 优先用已翻译的中文类别名; 旧数据缺字段时现场翻译兜底
    friendly = d.get("top_rules_friendly") or [
        (translate_rule_key(rk), n) for rk, n in (d.get("top_rules") or [])
    ]
    raw_rules = d.get("top_rules") or []
    suggestion_count = int(d.get("suggestion_count", 0) or 0)
    mr_count = int(d.get("mr_count", 0) or 0)

    if suggestion_count == 0:
        return "**概述**\n本周窗口内无检视建议产生，暂无问题分布与严重度数据。"

    total = sum(sev.values()) or suggestion_count

    def _pct(n: int) -> int:
        return round(n / total * 100) if total else 0

    hc = sev.get("critical", 0) + sev.get("high", 0)
    med = sev.get("medium", 0)
    low = sev.get("low", 0) + sev.get("warning", 0) + sev.get("other", 0)
    hc_p, med_p, low_p = _pct(hc), _pct(med), _pct(low)

    if hc_p >= 50:
        judge = "超过一半的问题被标为 high，整体偏重，不能只当风格问题看待"
    elif med_p >= 50:
        judge = "以中等严重度为主，多为需人工判断的逻辑/规范问题"
    else:
        judge = "整体偏轻，多为风格与规范类问题"

    overview = (
        f"严重度上 high {hc} 条（{hc_p}%）、medium {med} 条（{med_p}%）、"
        f"low 及以下 {low} 条（{low_p}%），{judge}。"
    )

    # 规则归类: 规范类 vs 正确性/稳定性类
    style_items: list[tuple[str, int]] = []
    logic_items: list[tuple[str, int]] = []
    for i, (name, n) in enumerate(friendly):
        key = raw_rules[i][0] if i < len(raw_rules) else ""
        cat = _categorize(name, key)
        (logic_items if cat == "正确性/稳定性类" else style_items).append((name, n))

    def _fmt(items: list[tuple[str, int]]) -> str:
        items = sorted(items, key=lambda x: -x[1])
        return "、".join(f"{nm} {n} 条" for nm, n in items[:3])

    style_total = sum(n for _, n in style_items)
    logic_total = sum(n for _, n in logic_items)
    if style_items and logic_items:
        dom = "代码规范类" if style_total >= logic_total else "正确性/稳定性类"
        problem_types = (
            f"主要归为两类：代码规范类（{_fmt(style_items)}）"
            f"和正确性/稳定性类（{_fmt(logic_items)}），后者会直接影响运行行为。"
            f"{dom}出现次数最多。"
        )
    elif style_items:
        problem_types = (
            f"以代码规范类为主（{_fmt(style_items)}），可下沉到 CI 机械拦截，"
            f"减少人肉 review 噪音。"
        )
    elif logic_items:
        problem_types = (
            f"以正确性/稳定性类为主（{_fmt(logic_items)}），应优先人工跟进排查运行期风险。"
        )
    else:
        problem_types = "本周未捕捉到具体规则命中的分布。"

    follow = (
        "类型标注、docstring 等规范问题可下沉到 CI 机械拦截；"
        "接口参数、循环、资源等正确性问题应优先人工确认修复。"
    )

    return "\n\n".join([
        f"**概述**\n{overview}",
        f"**问题类型**\n{problem_types}",
        f"**跟进建议**\n{follow}",
    ])


def _render_merged_mrs(d: dict[str, Any]) -> str:
    """变更汇总: head_line + 变更摘要 + MR 列表 (PR-Agent 同款)."""
    merge_count = int(d.get("merge_count", 0))
    target_branch = d.get("target_branch", "?")
    if merge_count == 0:
        return f"目标分支 `{target_branch}` 本周窗口内无 MR 合并。\n"

    additions = d.get("additions", 0)
    deletions = d.get("deletions", 0)
    files_changed = d.get("files_changed", 0)
    author_count = d.get("author_count", 0)
    mr_list = d.get("mr_list") or []

    # 优先用 opencode 生成的 LLM 变更摘要; 为空(LLM 失败/未配置)才回退确定性拼装
    llm_summary = (d.get("llm_description_markdown") or "").strip()
    if llm_summary:
        summary = _strip_leading_section_header(_demote_llm_headings(llm_summary), "变更摘要")
    else:
        summary = _build_change_summary(mr_list, author_count)

    head_line = (
        f"本周合并到 `{target_branch}` 的 MR 共 **{merge_count}** 个, "
        f"涉及作者 **{author_count}** 位"
    )
    # GitLab list API 不返回 additions/deletions (恒 0), 有 files_changed 就用它
    if additions or deletions:
        head_line += f", 新增代码 **{additions}** 行, 删除 **{deletions}** 行。"
    elif files_changed:
        head_line += f", 涉及文件变更 **{files_changed}** 个。"
    else:
        head_line += "。"

    lines: list[str] = [head_line, ""]
    if summary:
        lines.append("**变更摘要**")
        lines.append("")
        lines.append(summary.strip())
        lines.append("")

    lines.append("**涉及 MR 列表**")
    lines.append("")
    lines.append("| MR | 标题 | 作者 | 合并时间 |")
    lines.append("|---|---|---|---|")
    for mr in mr_list[:50]:
        iid = mr.get("iid", "?")
        title = (mr.get("title") or "").replace("|", "\\|").replace("\n", " ")
        author = mr.get("author") or "?"
        merged_at = (mr.get("merged_at") or "")[:10].replace("-", "/") or "?"
        url = mr.get("url") or mr.get("web_url") or ""
        cell = f"[!{iid}]({url})" if url else f"!{iid}"
        lines.append(f"| {cell} | {_wrap(title, width=22)} | {author} | {merged_at} |")
    return "\n".join(lines) + "\n"


def _render_repo_scan(d: dict[str, Any], pre_rendered: str | None) -> str:
    """本周代码质量全量扫描: 优先用 collector 的 markdown 字段, 否则从 data 拼."""
    if pre_rendered:
        # 防御性: 去掉 LLM 自带的节标题(渲染层已加), 并把 # 降级为粗体
        return _strip_leading_section_header(_demote_llm_headings(pre_rendered), "本周代码质量全量扫描")
    # 兜底: 由 data 合成 markdown
    target_branch = d.get("target_branch", "?")
    stats = d.get("diff_stats", {})
    high_risk = d.get("high_risk_files", [])
    top_rules = d.get("top_rules", [])
    severity = d.get("severity", {})
    lines = [
        f"目标分支 `{target_branch}` 本周 {stats.get('mr_count', 0)} 个合并 MR, "
        f"覆盖 {stats.get('files_changed', 0)} 个文件, +{stats.get('additions', 0)}/-"
        f"{stats.get('deletions', 0)} 行。\n",
        "**高风险模块**",
    ]
    if high_risk:
        for f in high_risk[:5]:
            lines.append(f"- `{f.get('path')}` (变更 +{f.get('additions', 0)}/-{f.get('deletions', 0)})")
    else:
        lines.append("- (无)")
    lines.append("\n**新增坏味道**")
    if top_rules:
        for rk, n in top_rules[:5]:
            lines.append(f"- `{rk}` × **{n}**")
    else:
        lines.append("- (无)")
    return "\n".join(lines) + "\n"


def render_markdown(artifact: WeeklyArtifact) -> str:
    """组装完整周报 markdown."""
    parts: list[str] = []
    emoji = artifact.report_emoji or "📊"
    title = f"# {emoji} {artifact.report_title} — {artifact.week_label}\n"
    parts.append(title)
    parts.append(
        f"> 生成时间: {(artifact.generated_at or '').isoformat()[:16].replace('T', ' ')}"
        f"<br>数据范围: {artifact.week_start.isoformat()[:10].replace('-', '/')} "
        f"~ {(artifact.week_end - timedelta(days=1)).isoformat()[:10].replace('-', '/')}\n"
    )

    # 按 pr_agent 顺序拼装
    ordered = ("telemetry", "merged_mrs", "repo_scan")
    failures: list[str] = []
    for name in ordered:
        section = artifact.sections.get(name)
        if section is None:
            parts.append(f"\n## {SECTION_TITLES.get(name, name)}\n\n> 本节未启用\n")
            continue

        title = SECTION_TITLES.get(name, name)
        # merged_mrs 标题含 {branch} 占位符, 即使数据缺失也要用默认值替换, 避免残留字面量
        if name == "merged_mrs":
            branch = (section.data or {}).get("target_branch") or "main"
            title = title.format(branch=branch)

        parts.append(f"\n## {title}\n")

        if section.status != "ok":
            failures.append(name)
            parts.append(f"\n⚠️ 数据缺失: {section.error or '未知原因'}\n")
            continue

        body = render_section(name, section)
        parts.append("\n" + body + "\n")

    if failures:
        parts.append("\n---\n⚠️ 本次报告部分数据缺失: " + ", ".join(failures) + "\n")

    return "\n".join(parts).rstrip() + "\n"


def split_markdown(md: str, chunk_limit: int = 18000) -> list[str]:
    """超长 markdown 按 ## 切块, 防单条 IM 消息超限."""
    if len(md) <= chunk_limit:
        return [md]
    chunks: list[str] = []
    current: list[str] = []
    current_bytes = 0
    for line in md.splitlines(keepends=True):
        if line.startswith("## ") and current_bytes + len(line) > chunk_limit and current:
            chunks.append("".join(current))
            current = [line]
            current_bytes = len(line)
        else:
            current.append(line)
            current_bytes += len(line)
    if current:
        chunks.append("".join(current))
    return chunks


__all__ = [
    "render_section", "render_markdown", "split_markdown",
    "section_title", "SECTION_TITLES",
]
