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
    # 5 个指标行, 表头三列无 emoji
    header = next(l for l in out.splitlines() if l.startswith("| 指标"))
    assert header == "| 指标 | 当前值 | 较上周 |", header
    # 任一 cell 不含 emoji 字符 (避免钉钉桌面端渲染成方块)
    for ch in ("📋", "📈", "📉", "📥", "📂", "💡", "📊", "✅"):
        assert ch not in out, f"emoji {ch!r} should be removed from metric table"
    # 累计采纳率行 pp 后缀
    pp_row = [l for l in out.splitlines() if "累计采纳率" in l][0]
    assert "pp" in pp_row, "adoption_rate_pct delta should carry 'pp' suffix"
    # 有 delta 时显示箭头
    assert any("↑" in l for l in out.splitlines()), "have deltas → should show ↑/↓ arrow"


def test_render_telemetry_shows_first_week_when_no_deltas():
    """没有 deltas 时 (首周跑批), trend 列显示 "首周", 短文不会被钉钉截断."""
    data = _sample_data()
    data["deltas"] = {}   # 清空 deltas
    out = _render_telemetry(data)
    rows = [l for l in out.splitlines() if l.startswith("| ✓") or l.startswith("| ▸") or l.startswith("| ▣")]
    assert len(rows) == 5
    first_week_rows = [l for l in rows if "首周" in l]
    assert len(first_week_rows) == 5, "全部 5 行 metrics 在 deltas 为空时都应该是 '首周'"
    # 采纳率行也要是 "首周", 而不是 14.7% (↑None pp)
    assert any("首周" in l and "14.7%" in l for l in rows)


def test_deterministic_summary_problem_types_is_bullet():
    md = _build_inspection_summary(_sample_data())
    # 「问题类型」段下面应该是一组 `- **...** ×N` bullet
    pt_segment = md.split("**问题类型**")[1].split("**跟进建议**")[0]
    bullets = [line for line in pt_segment.splitlines() if line.startswith("- **")]
    assert len(bullets) >= 1, "problem_types 段必须是 bullet 列表"
    # 至少含一条 `×N`，N 是数字
    assert any("×" in b and any(ch.isdigit() for ch in b) for b in bullets)
