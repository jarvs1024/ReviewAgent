"""Render WeeklyArtifact -> Markdown + split into chunks (按 ## 标题切).

参考 pr-agent: pr_agent/reporting/renderer.py
    - 每节渲染为 ## <title>
    - status='failed' 的 section 渲染为警告行
    - markdown 总长超 chunk_limit 时按 ## 切分 (避免单条 IM 消息超限)
"""
from __future__ import annotations

import re
from typing import Any

from .artifact import WeeklyArtifact
from .collectors.base import SectionResult


# 节名 -> 中文标题
SECTION_TITLES: dict[str, str] = {
    "telemetry": "本周检视概况",
    "merged_mrs": "本周合并到目标分支的 MR",
}


def section_title(name: str, fallback: str | None = None) -> str:
    return SECTION_TITLES.get(name, fallback or name)


def render_section(name: str, sr: SectionResult) -> str:
    """单个 section -> markdown 块 (不含顶层 ## 标题)."""
    if sr.status == "failed":
        err = sr.error or "未知错误"
        return f"> ⚠️ 数据采集失败: `{err}`\n"

    if name == "telemetry":
        return _render_telemetry(sr.data or {})
    if name == "merged_mrs":
        return _render_merged_mrs(sr.data or {})
    # 兜底
    return "```json\n" + str(sr.data)[:1000] + "\n```"


def _render_merged_mrs(d: dict[str, Any]) -> str:
    """merged_mrs section markdown 渲染."""
    items = d.get("items") or []
    total = d.get("total", 0)
    target_branch = d.get("target_branch", "main")
    if not items:
        return f"本周无合并到 `{target_branch}` 的 MR。\n"
    lines: list[str] = []
    lines.append(f"共合并 **{total}** 个 MR 到 `{target_branch}`:\n")
    lines.append("| MR | 标题 | 作者 | 合并人 | 源分支 | 变更行数 | 合并时间 |")
    lines.append("|---|---|---|---|---|---|---|")
    for m in items:
        iid = m.get("iid")
        title = (m.get("title") or "").replace("|", "\\|")[:60]
        author = m.get("author", "")
        merged_by = m.get("merged_by", "") or "—"
        src = (m.get("source_branch") or "").replace("|", "\\|")
        changes = f"+{m.get('additions', 0)}/-{m.get('deletions', 0)}"
        merged_at = (m.get("merged_at") or "")[:16].replace("T", " ")
        mr_link = f"[!{iid}]({m.get('web_url', '#')})" if m.get("web_url") else f"!{iid}"
        lines.append(f"| {mr_link} | {title} | `{author}` | `{merged_by}` | `{src}` | {changes} | {merged_at} |")
    return "\n".join(lines) + "\n"


def _render_telemetry(d: dict[str, Any]) -> str:
    """telemetry section markdown 渲染."""
    total = d.get("total", 0)
    success = d.get("success", 0)
    failed = d.get("failed", 0)
    running = d.get("running", 0)
    rate = d.get("success_rate", 0.0)
    avg_ms = d.get("avg_duration_ms", 0)
    if total == 0:
        return "本周无任何检视 run 记录。\n"

    lines: list[str] = []
    lines.append(f"- 总 run 数: **{total}**")
    lines.append(f"- 成功: **{success}**  •  失败/超时: **{failed}**  •  运行中: {running}")
    lines.append(f"- 成功率: **{rate}%**")
    lines.append(f"- 平均耗时: **{avg_ms/1000:.1f}s**")
    lines.append("")

    suggestions = d.get("suggestions") or {}
    if suggestions.get("total", 0):
        lines.append("**建议采纳情况:**")
        lines.append("")
        lines.append(
            f"- 建议数: **{suggestions['total']}**  •  已采纳: **{suggestions.get('adopted', 0)}**  •  "
            f"已 dismiss: **{suggestions.get('dismissed', 0)}**  •  采纳率: **{suggestions.get('adoption_rate', 0)}%**"
        )
        severity_counts = suggestions.get("severity_counts") or {}
        if severity_counts:
            lines.append("- 严重级别: " + "、".join(f"{key} {value}" for key, value in severity_counts.items()))
        reasons = d.get("dismissal_reasons") or {}
        if reasons:
            lines.append("- Dismiss 原因: " + "、".join(f"{key} {value}" for key, value in reasons.items()))
        lines.append("")

    # by_command
    by_command = d.get("by_command") or {}
    if by_command:
        lines.append("**按命令维度:**")
        lines.append("")
        lines.append("| 命令 | 次数 | 成功 | 失败 | 平均耗时 | 最长耗时 |")
        lines.append("|---|---|---|---|---|---|")
        for cmd, bc in by_command.items():
            lines.append(
                f"| `{cmd}` | {bc['count']} | {bc['success']} | {bc['failed']} | "
                f"{bc['avg_duration_ms']/1000:.1f}s | {bc['max_duration_ms']/1000:.1f}s |"
            )
        lines.append("")

    # by_day
    by_day = d.get("by_day") or {}
    if by_day:
        lines.append("**按日趋势:**")
        lines.append("")
        for day, n in by_day.items():
            bar = "█" * min(n, 30)
            lines.append(f"- `{day}`: {n} {bar}")
        lines.append("")

    # top_mrs
    top_mrs = d.get("top_mrs") or []
    if top_mrs:
        lines.append("**活跃 MR Top 5:**")
        lines.append("")
        lines.append("| Project | MR | 标题 | 作者 | run | 成功 | 失败 |")
        lines.append("|---|---|---|---|---|---|---|")
        for mr in top_mrs[:5]:
            title = (mr.get("title") or "").replace("|", "/")
            if len(title) > 40:
                title = title[:38] + "…"
            lines.append(
                f"| {mr['project_id']} | !{mr['mr_iid']} | {title} | "
                f"`{mr.get('author','')}` | {mr['runs']} | {mr['success']} | {mr['failed']} |"
            )
        lines.append("")

    # failed runs
    failed_runs = d.get("failed_runs") or []
    if failed_runs:
        lines.append(f"**失败 run 详情 (最近 {min(10, len(failed_runs))} 条):**")
        lines.append("")
        for r in failed_runs[:10]:
            t = (r.get("started_at") or "")[:19]
            err = (r.get("error") or "")[:80].replace("|", "/").replace("\n", " ")
            lines.append(f"- `{t}` {r.get('command','')} proj={r.get('project_id')} mr={r.get('mr_iid')} actor=`{r.get('actor_username','')}` — {err}")
        lines.append("")

    return "\n".join(lines)


def render_markdown(artifact: WeeklyArtifact) -> str:
    """整个 artifact -> markdown 字符串."""
    lines: list[str] = []
    title_line = f"# {artifact.report_emoji} {artifact.report_title} ({artifact.week_label})"
    lines.append(title_line)
    lines.append("")
    s = artifact.week_start
    e = artifact.week_end
    # 减 1 秒表示 inclusive
    e_disp = e
    lines.append(
        f"> 周期: **{s.strftime('%Y-%m-%d %H:%M')}** ~ **{e_disp.strftime('%Y-%m-%d %H:%M')}** "
        f"({artifact.timezone})"
    )
    lines.append(f"> 生成时间: {artifact.generated_at.isoformat() if artifact.generated_at else '?'}")
    lines.append(f"> Project ID: `{artifact.project_id}`")
    lines.append("")
    lines.append("---")
    lines.append("")

    for name, sr in artifact.sections.items():
        title = section_title(name)
        lines.append(f"## {title}")
        lines.append("")
        body = sr.markdown or render_section(name, sr)
        lines.append(body)
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("_Generated by ReviewAgent reporting · "
                 "source: `telemetry.db` · "
                 "sections: " + ", ".join(artifact.sections.keys()) + "_")
    return "\n".join(lines)


def split_markdown(md: str, chunk_limit: int = 18000) -> list[str]:
    """按 ## 切 markdown, 每片 <= chunk_limit.

    策略:
      1. 找所有 ## 位置
      2. 在最后一个 ## 处切, 使前片 <= chunk_limit
      3. 反复直到全部切完
      4. 单片也超 limit 时, 强制按 chunk_limit 字节切
    """
    if len(md.encode("utf-8")) <= chunk_limit:
        return [md]

    parts: list[str] = []
    rest = md
    while len(rest.encode("utf-8")) > chunk_limit:
        # 找所有 ## 位置
        positions = [m.start() for m in re.finditer(r"^## ", rest, re.MULTILINE)]
        if not positions:
            # 没有 ##, 硬切
            cut = chunk_limit
            parts.append(rest[:cut])
            rest = rest[cut:]
            continue
        # 找最后一个 < chunk_limit 的 ##
        cut = 0
        for p in positions[1:]:  # skip 第一个 ## (顶层标题)
            if p < chunk_limit:
                cut = p
            else:
                break
        if cut == 0:
            cut = chunk_limit
        parts.append(rest[:cut])
        rest = rest[cut:]
    if rest.strip():
        parts.append(rest)
    return parts


__all__ = ["render_markdown", "render_section", "split_markdown", "SECTION_TITLES"]
