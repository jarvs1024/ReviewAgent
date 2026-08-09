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


def _resolve_section_title(name: str, section: "SectionResult | None") -> str:
    """Resolve SECTION_TITLES.format(...) 占位符.

    段二 `merged_mrs` 标题含 `{branch}`, data 缺失或 section 为 None 时
    用 "main" 兜底, 避免渲染出字面 `## 二、本周 {branch} 变更汇总`.
    """
    title = SECTION_TITLES.get(name, name)
    if "{branch}" in title:
        target_branch = None
        if section is not None:
            target_branch = (section.data or {}).get("target_branch")
        if not target_branch:
            target_branch = "main"
        try:
            title = title.format(branch=target_branch)
        except (KeyError, IndexError):
            title = title.replace("{branch}", target_branch)
    return title
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
        "**概述**",
        "",
        f"本周共合并 **{len(mr_list)}** 个 MR，涉及 **{author_count}** 位作者。",
    ]
    if nature_counter:
        desc = "、".join(f"{k} {v} 个" for k, v in sorted(nature_counter.items(), key=lambda x: -x[1]))
        parts.append(f"按性质看：{desc}。")
    for label in ("新增", "修改", "删除"):
        items = groups[label]
        parts.append("")
        parts.append(f"**{label}**")
        parts.append("")
        if items:
            for t in items[:8]:
                parts.append(f"- {t}")
        else:
            parts.append(f"- 本周无{label}类变更")
        parts.append("")
    return "\n".join(parts).strip()


def render_section(name: str, sr: SectionResult, *, dashboard_url: str = "") -> str:
    """单个 section -> markdown 块 (不含顶层 ## 标题).

    Args:
        name: section 名 (telemetry / merged_mrs / repo_scan)
        sr: 该 section 的采集结果
        dashboard_url: 检视看板 URL, 当前仅 telemetry 段 (「本周检视概况」) 末尾拼接.
                        空字符串时不渲染.
    """
    if sr.status == "failed":
        err = sr.error or "未知错误"
        return f"> ⚠️ 数据采集失败: `{err}`\n"

    if name == "telemetry":
        return _render_telemetry(sr.data or {}, dashboard_url=dashboard_url)
    if name == "merged_mrs":
        return _render_merged_mrs(sr.data or {})
    if name == "repo_scan":
        return _render_repo_scan(sr.data or {}, sr.markdown)
    # 兜底
    return "```json\n" + str(sr.data)[:1000] + "\n```"


def _fmt_delta(delta, suffix: str = "", digits: int = 1) -> str:
    """格式化环比 delta 为带箭头的短串; None 返回空串.

    默认 `digits=1` 把浮点数截断到 1 位小数, 防止 14.7% (↑9.899999999999999pp)
    这种 IEEE 浮点尾巴泄漏到钉钉表里. `digits=0` 用于整数.
    """
    if delta is None:
        return ""
    arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "→")
    rounded = round(float(delta), digits)
    # 兼容 -0.0 → 0
    if rounded == 0:
        rounded = 0
        arrow = "→"
    val = f"{arrow}{abs(rounded)}{suffix}"
    return val


def _maybe_unwrap_llm_markdown(text: str | None) -> str:
    """LLM markdown 字符串嗅探兜底.

    部分 agent (weekly_change_summary / weekly_quality_scan) 即使走
    tolerant_markdown 模式, 偶尔会把 `{"markdown": "..."}` 字面字符串
    透传到 `data["markdown"]`. 这里再剥一次, 防止钉钉群里出现一坨裸 JSON.

    非字符串或解析失败时原样返回.
    """
    if not text or not isinstance(text, str):
        return text or ""
    head = text.lstrip()
    if not head.startswith("{"):
        return text
    import json as _json
    try:
        obj = _json.loads(head, strict=False)
    except _json.JSONDecodeError:
        return text
    if isinstance(obj, dict):
        md = obj.get("markdown")
        if isinstance(md, str):
            return md
    return text


def _render_telemetry(d: dict[str, Any], *, dashboard_url: str = "") -> str:
    """检视概况: 叙事性检视汇总(LLM 润色) 在上, 核心指标表在下.

    - 汇总段优先用 opencode 生成的 llm_summary_markdown; 为空(LLM 失败/未配置)
      回退到确定性 _build_inspection_summary.
    - 指标表仅保留 5 行核心指标, 不再堆 severity 分布 / 触发最多规则 两行裸数据.
    - `dashboard_url` 非空时, 在指标表之后追加「📈 检视看板: [url](url)」一行,
      指向 cfg.REVIEWAGENT_WEEKLY_DASHBOARD_URL 配置的检视看板地址.
    """
    mr_count = d.get("mr_count", 0)
    mr_total = d.get("mr_total", 0)
    suggestion_count = d.get("suggestion_count", 0)
    suggestion_total = d.get("suggestion_total", suggestion_count)
    adoption_rate = round(float(d.get("adoption_rate", 0)) * 100, 1)
    deltas = d.get("deltas") or {}

    # 汇总段: LLM 润色版优先, 否则确定性兜底
    llm_summary = _maybe_unwrap_llm_markdown(d.get("llm_summary_markdown")).strip()
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

    # ---- 渲染指标表 (3 列 + emoji, 上周无对比显示 "—") ----
    # 钉钉桌面端在 macOS / Windows 都能正常渲染标准 emoji,
    # 给每个 cell 配 emoji 不仅撑宽视觉, 也快速锚定"周/累计/采纳率"维度.
    # 累计两行 (累计 MR 数 / 累计建议数) 在首次推送时无 prev_data,
    # 较上周列显示单字 "—", 简短不截断, 跟"↑42"风格对比统一.
    def _fmt_trend(delta) -> str:
        if delta is None:
            return "—"          # 新项目无对比 — 单字 dash, 不写"首周"
        if delta == 0:
            return "→ 持平"
        arrow = "↑" if delta > 0 else "↓"
        return f"{arrow}{abs(delta)}"

    rows = [
        ("📥 本周 MR 数",        str(mr_count),          _fmt_trend(deltas.get("mr_count"))),
        ("📂 累计 MR 数",        str(mr_total),          _fmt_trend(deltas.get("mr_total"))),
        ("💡 本周建议数",        str(suggestion_count),  _fmt_trend(deltas.get("suggestion_count"))),
        ("📊 累计建议数",        str(suggestion_total),  _fmt_trend(deltas.get("suggestion_total"))),
        ("✅ 累计采纳率",        f"{adoption_rate}%",    _fmt_trend(deltas.get("adoption_rate_pct"))),
    ]
    # adoption_rate_pct 的 delta 是 pp 单位; 箭头后补 pp 让语义清楚
    if deltas.get("adoption_rate_pct") not in (None, 0):
        d = deltas["adoption_rate_pct"]
        rows[-1] = (
            "✅ 累计采纳率",
            f"{adoption_rate}%",
            f"{'↑' if d > 0 else '↓'}{abs(round(float(d), 1))} pp",
        )

    lines.append("| 📋 指标 | 📈 当前数值 | 📉 较上周 |")
    lines.append("|:-:|:-:|:-:|")
    for label, value, trend in rows:
        lines.append(f"| {label} | {value} | {trend} |")

    if dashboard_url:
        # 末尾独立一行: emoji + 锚文字 + markdown 链接, 钉钉桌面端会渲染成可点链接.
        lines.append("")
        lines.append(f"📈 **检视看板**: [{dashboard_url}]({dashboard_url})")

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
        return "**概述**\n\n本周窗口内无检视建议产生，暂无问题分布与严重度数据。"

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
    dom = (
        "代码规范类" if style_total >= logic_total else "正确性/稳定性类"
    ) if (style_items or logic_items) else ""

    # 跟「跟进建议」/ LLM 输出格式对齐: 按序列 bullet, 每个高频类别一行
    problem_lines: list[str] = []
    for cat_name, items in (("代码规范类", style_items), ("正确性/稳定性类", logic_items)):
        for name, n in sorted(items, key=lambda x: -x[1]):
            problem_lines.append(
                f"- **{name}** ×{n}：归为「{cat_name}」，直接影响 review 流程或运行期行为"
            )
    if not problem_lines:
        problem_lines.append("- 本周未捕捉到具体规则命中的分布。")
    else:
        problem_lines.append(f"- {dom}出现次数最多。")
    problem_types = "\n".join(problem_lines)

    # 跟进建议不再用"规范 CI / 正确性人工"这种万能套话, 改为基于
    # 本周 top_rules 给出针对性 1~3 条具体动作. 规则前缀 -> 具体建议的
    # 映射可以持续扩展, 缺的类别退化到通用提示 (但不退化成套话).
    _RULE_ACTIONS = {
        "SSD-RULE-TYPEHINTS":              "启用 ruff `ANN`/mypy strict 加入 CI, 一次性消存量",
        "SSD-RULE-DOCSTRING-REQUIRED":     "在 pre-commit 跑 ruff `D` 系列, docstring 缺失直接 fail",
        "SSD-RULE-NO-LOG-EXC":             "用 ruff `BLE` + `S` (logging 规范) 加 CI, 对裸 `except Exception` 报错",
        "SSD-RULE-NO-BARE-PRINT":          "用 ruff `T201` (禁止 print) 加入 CI 阻断列表",
        "SSD-RULE-NO-MUTABLE-DEFAULT":     "用 ruff `B006` 拦截可变默认参数",
        "SSD-RULE-RESOURCE-CONTEXT-MANAGER": "用 ruff `SIM`/`PTH123` 拦截裸 open/close, 强制 with 语句",
        "SSD-RULE-FORBIDDEN-COMMENT":      "把无效注释扫一遍存 issue, 用 ruff `ERA` 抑制注释式代码",
        "SSD-RULE-FORBIDDEN-WILDCARD-IMPORT": "用 ruff `F401`/`F403` 拦截 `from foo import *`",
        "R-LOOP":                          "把『循环边界/无限循环』作为 review checklist 红线条款",
        "R-RES":                           "扫一遍 `open/requests/socket` 调用, 强制 `with` + 超时",
        "R-ERR":                           "把裸 `except:` 与静默 `pass` 列为硬错误, 加入 review 范本",
        "R-SHELL":                         "对所有 shell 调用加超时 + 异常类型细化 + sandbox 跑",
        "R-CI":                            "对并行用例加临时目录 PID 隔离 / 串行锁, 修 flaky",
        "R-OTHER-IMPACT":                  "对跨文件函数签名变化在 MR 描述里点出 caller, 拉对应 owner review",
    }

    follow_lines: list[str] = []
    seen_categories: set[str] = set()
    for raw_key, n in raw_rules:
        if n <= 0:
            continue
        # 命中精确规则 → 命中前缀 (R-OTHER-IMPACT:caller_param 这种 long-form)
        action = _RULE_ACTIONS.get(raw_key)
        if not action and raw_key.startswith("R-OTHER-IMPACT:"):
            action = _RULE_ACTIONS["R-OTHER-IMPACT"]
        if not action and raw_key.startswith("R-OTHER:"):
            action = "针对新增『其他』类违规, 沉淀一条新规则到 AGENTS.md 规范清单"
        if not action:
            continue
        # 同类别去重 (避免重复推荐同一类规则)
        cat = raw_key.split(":")[0]
        if cat in seen_categories:
            continue
        seen_categories.add(cat)
        follow_lines.append(f"- **{translate_rule_key(raw_key)}** ×{n}: {action}")
        if len(follow_lines) >= 3:
            break

    if not follow_lines:
        # 没匹配到已知规则, 给一句具体观察而不是套话
        if hc_p >= 50:
            follow_lines.append(
                f"- 本周 high 占比 {hc_p}%, 建议本周 review 时优先关注 high 严重度的具体 MR 列表"
            )
        else:
            follow_lines.append(
                f"- 本周无 dominant 规则, 建议按 top_rules 的人工抽样 5 个 suggestion 看根因"
            )

    follow = "\n".join(follow_lines)

    return "\n\n".join([
        f"**概述**\n\n{overview}",
        f"**问题类型**\n\n{problem_types}",
        f"**跟进建议**\n\n{follow}",
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
    llm_summary = _maybe_unwrap_llm_markdown(d.get("llm_description_markdown")).strip()
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


def _is_core_path_for_scan(path: str) -> bool:
    """判断文件是否属于核心模块（repo_scan 兜底渲染用）."""
    core_prefixes = (
        "services/", "framework/", "src/", "app/", "core/", "lib/",
        "reviewagent/",
    )
    lower = path.lower()
    return any(lower.startswith(p) for p in core_prefixes)


def _render_repo_scan(d: dict[str, Any], pre_rendered: str | None) -> str:
    """本周代码质量全量扫描: 优先用 collector 的 markdown 字段, 否则从 data 拼."""
    pre_rendered = _maybe_unwrap_llm_markdown(pre_rendered)
    if pre_rendered:
        # 把 LLM 误用的 # 降级为粗体 (因为钉钉 markdown 渲染时不放大 #)
        return _demote_llm_headings(pre_rendered)
    # 兜底: 由 data 合成 markdown (与新 collector 返回字段保持一致)
    target_branch = d.get("target_branch", "?")
    total_mrs = d.get("total_mrs", 0)
    total_files = d.get("total_files", 0)
    total_additions = d.get("total_additions", 0)
    total_deletions = d.get("total_deletions", 0)
    top_files = d.get("top_files", []) or d.get("file_changes", [])[:5]
    lines = [
        f"本周合并到 `{target_branch}` 的 MR 共 **{total_mrs}** 个，"
        f"去重变更文件 **{total_files}** 个，新增 **{total_additions}** 行、删除 **{total_deletions}** 行。"
        "以下基于变更热力图做全局判断：",
        "",
        "**高风险模块**",
        "",
    ]
    core_files = [f for f in top_files if f.get("path") and _is_core_path_for_scan(f["path"])]
    display_files = core_files[:5] if core_files else top_files[:5]
    if display_files:
        for f in display_files:
            lines.append(
                f"- `{f.get('path')}` | +{f.get('additions', 0)} -{f.get('deletions', 0)}"
            )
    else:
        lines.append("- (无显著变更)")
    lines.append("")
    lines.append("**新增坏味道**")
    lines.append("")
    new_files = [f for f in top_files if f.get("is_new")]
    if new_files:
        lines.append("- 新增文件 / 模块需关注：")
        for f in new_files[:5]:
            lines.append(f"  - `{f.get('path')}`")
    else:
        lines.append("- 本周以现有文件修改为主，未观察到新增模块。")
    lines.append("")
    lines.append("**测试覆盖与可靠性**")
    lines.append("")
    test_files = [f for f in top_files if "test" in (f.get("path") or "").lower()]
    if test_files:
        lines.append(f"- 测试文件有 {len(test_files)} 处变更，建议确认核心逻辑是否配套覆盖。")
    else:
        lines.append("- 本周变更未明显命中测试目录，建议检查核心逻辑是否补充测试。")
    lines.append("")
    lines.append("**建议跟进**")
    lines.append("")
    lines.append("- 关注核心模块变更的接口兼容性与回归测试覆盖。")
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
            # section 未启用: 仍然要 resolve 标题 (merged_mrs 含 {branch} 占位)
            parts.append(f"\n## {_resolve_section_title(name, None)}\n\n> 本节未启用\n")
            continue

        title = _resolve_section_title(name, section)

        parts.append(f"\n## {title}\n")

        if section.status != "ok":
            failures.append(name)
            parts.append(f"\n⚠️ 数据缺失: {section.error or '未知原因'}\n")
            continue

        body = render_section(name, section, dashboard_url=artifact.dashboard_url or "")
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
