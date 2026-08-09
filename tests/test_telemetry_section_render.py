"""renderer._render_telemetry 输出契约 — bullet 「问题类型」 + 3 列加宽指标表."""
from __future__ import annotations

import os
os.environ.setdefault("GITLAB_URL", "http://x")
os.environ.setdefault("REVIEWAGENT_WEBHOOK_PORT", "3000")
os.environ.setdefault("TELEMETRY_DB", "/tmp/x.db")
os.environ.setdefault("LLM_PROVIDER", "stub")
os.environ.setdefault("LOG_LEVEL", "WARNING")

from reviewagent.reporting.renderer import _render_telemetry, _build_inspection_summary


def _sample_data() -> dict:
    return {
        "mr_count": 42, "mr_total": 200,
        "suggestion_count": 367, "suggestion_total": 1234,
        "adoption_rate": 0.147,
        "severity_breakdown": {"high": 172, "medium": 163, "low": 32},
        "top_rules": [
            ("SSD-RULE-TYPEHINTS", 113),
            ("SSD-RULE-DOCSTRING-REQUIRED", 97),
            ("R-RES", 33),
        ],
        "top_rules_friendly": [("类型注解缺失", 113), ("docstring 缺失", 97), ("资源句柄未释放", 33)],
        "deltas": {
            "mr_count": 8, "mr_total": 8,
            "suggestion_count": 50, "suggestion_total": 50,
            "adoption_rate_pct": 9.9,
        },
        "llm_summary_markdown": "",  # 走确定性兜底
    }


def test_render_telemetry_uses_three_column_metric_table():
    out = _render_telemetry(_sample_data())
    # 表头 3 列 (|:-:|:-:|:-:| 整串作为单一表分隔)
    assert "|:-:|:-:|:-:|" in out, "header separator should define 3 center-aligned columns"
    # 5 行指标 (每行 3 cell)
    # 表头 3 列含 emoji (钉钉桌面端 macOS 能正常渲染)
    header = next(l for l in out.splitlines() if l.startswith("|"))
    assert header == "| 📋 指标 | 📈 当前数值 | 📉 较上周 |", header
    # 每行 emoji 前缀锚定维度
    rows = [l for l in out.splitlines() if l.startswith("| 📥") or l.startswith("| 📂")
            or l.startswith("| 💡") or l.startswith("| 📊") or l.startswith("| ✅")]
    assert len(rows) == 5, f"expected 5 metric rows, got {len(rows)}: {rows}"
    # 累计采纳率行 pp 后缀
    pp_row = [r for r in rows if r.startswith("| ✅")][0]
    assert "pp" in pp_row, "adoption_rate_pct delta should carry 'pp' suffix"
    # 有 delta 时显示箭头
    assert any("↑" in l for l in out.splitlines()), "have deltas → should show ↑/↓ arrow"


def test_render_telemetry_shows_single_dash_when_no_deltas():
    """无 deltas 时 (首周跑批), trend 列显示单字 "—", 累计两行尤其要简洁."""
    data = _sample_data()
    data["deltas"] = {}   # 清空 deltas
    out = _render_telemetry(data)
    rows = [l for l in out.splitlines() if l.startswith("| 📥") or l.startswith("| 📂")
            or l.startswith("| 💡") or l.startswith("| 📊") or l.startswith("| ✅")]
    assert len(rows) == 5
    # 5 行 trend 列应该都是 "—"
    dash_rows = [l for l in rows if l.endswith("— |") or "| — |" in l]
    assert len(dash_rows) == 5, f"全部 5 行 metrics 在 deltas 为空时 trend 列都应该是 '—', got: {rows}"
    # 不能出现 "首周" 这种旧文案
    assert "首周" not in out, "「首周」长文案应已被替换为单字 —"
    # 也不能出现 "(新项目无对比)" 这种会被钉钉截断的长文案
    assert "(新项目无对比)" not in out


def test_deterministic_summary_problem_types_is_bullet():
    md = _build_inspection_summary(_sample_data())
    # 「问题类型」段下面应该是一组 `- **...** ×N` bullet
    pt_segment = md.split("**问题类型**")[1].split("**跟进建议**")[0]
    bullets = [line for line in pt_segment.splitlines() if line.startswith("- **")]
    assert len(bullets) >= 1, "problem_types 段必须是 bullet 列表"
    # 至少含一条 `×N`，N 是数字
    assert any("×" in b and any(ch.isdigit() for ch in b) for b in bullets)


def test_render_telemetry_appends_dashboard_link_when_url_given():
    """dashboard_url 非空时, 在指标表后追加 `📈 检视看板: [url](url)` 一行."""
    out = _render_telemetry(_sample_data(), dashboard_url="http://127.0.0.1:8080/code-review")
    lines = out.splitlines()
    # 在最末行 (表格之后, 空行 + 链接行)
    link_line = next((l for l in lines if l.startswith("📈")), None)
    assert link_line is not None, f"应有「📈 检视看板」行, got:\n{out}"
    # markdown 链接格式: [url](url)
    assert "[http://127.0.0.1:8080/code-review](http://127.0.0.1:8080/code-review)" in link_line
    # 链接行必须出现在指标表最后一行 (✅ 行) 之后
    last_metric_idx = max(
        i for i, l in enumerate(lines)
        if l.startswith(("| 📥", "| 📂", "| 💡", "| 📊", "| ✅"))
    )
    link_idx = lines.index(link_line)
    assert link_idx > last_metric_idx, (
        f"检视看板链接必须在指标表之后. last_metric_idx={last_metric_idx}, link_idx={link_idx}"
    )


def test_render_telemetry_omits_dashboard_link_when_url_empty():
    """dashboard_url 为空时, 不渲染「检视看板」行, 行为向前兼容."""
    out_default = _render_telemetry(_sample_data())
    out_empty = _render_telemetry(_sample_data(), dashboard_url="")
    # 「📈」emoji 也会出现在表头「📈 当前数值」里, 不能用纯 emoji 检测.
    # 改用「检视看板」锚文字精确判定: 仅 dashboard 链接行有它.
    assert "检视看板" not in out_default
    assert "检视看板" not in out_empty
