"""E13: 周报 live 字段断言（标题 / emoji / dashboard 链接 / 分片数）的单元测试.

Why 需要:
`run_weekly_report` 这个 E2E flow 此前只断言「json + md 文件存在」，
产物**内容**完全没校验 —— 标题写错、emoji 丢、dashboard 链接没渲染成
markdown 链接、分片数对不上，都不会有任何测试变红。

这些断言逻辑放在 `flows._audit_weekly_live_fields` 里（纯函数，不触网），
所以可以在这里用构造产物做确定性测试 —— 不必真跑一次周报。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.e2e import flows  # noqa: E402

DASHBOARD = "http://127.0.0.1:8080/code-review"
TITLE = "SSD自动化代码检视周报"
EMOJI = "📊"
WEEK = "2026-W45"


def _write_artifact(
    tmp_path: Path,
    *,
    title: str = TITLE,
    emoji: str = EMOJI,
    dashboard: str = DASHBOARD,
    week_label: str = WEEK,
    md_body: str | None = None,
) -> tuple[Path, Path]:
    artifact = {
        "schema_version": 2,
        "project_id": 31,
        "week_label": week_label,
        "week_start": "2026-11-02T00:00:00+08:00",
        "week_end": "2026-11-09T00:00:00+08:00",
        "generated_at": "2026-11-09T10:30:00+08:00",
        "timezone": "Asia/Shanghai",
        "report_title": title,
        "report_emoji": emoji,
        "dashboard_url": dashboard,
        "sections": {"telemetry": {"status": "ok", "data": {}}},
    }
    json_path = tmp_path / f"weekly-{week_label}.json"
    json_path.write_text(json.dumps(artifact, ensure_ascii=False), encoding="utf-8")
    if md_body is None:
        md_body = (
            f"# {emoji} {title} — {week_label}\n\n"
            "> 生成时间: 2026-11-09 10:30\n\n"
            "## 本周检视概况\n\n"
            f"📈 **检视看板**: [{dashboard}]({dashboard})\n"
        )
    md_path = tmp_path / f"weekly-{week_label}.md"
    md_path.write_text(md_body, encoding="utf-8")
    return json_path, md_path


STDOUT = f"[ok] delivery     : dry_run, chunks=1\n"


def test_live_fields_pass_on_wellformed_artifact(tmp_path: Path) -> None:
    """标题 / emoji / dashboard 链接 / 分片数齐全 → 通过."""
    _write_artifact(tmp_path)
    problems, detail = flows._audit_weekly_live_fields(tmp_path, STDOUT)
    assert not problems, f"{problems} | {detail}"
    assert "dashboard" in detail


def test_missing_title_is_reported(tmp_path: Path) -> None:
    """artifact.report_title 为空 → 报红."""
    _write_artifact(tmp_path, title="")
    problems, _ = flows._audit_weekly_live_fields(tmp_path, STDOUT)
    assert any("report_title" in p for p in problems), problems


def test_missing_emoji_is_reported(tmp_path: Path) -> None:
    """artifact.report_emoji 为空 → 报红."""
    _write_artifact(tmp_path, emoji="")
    problems, _ = flows._audit_weekly_live_fields(tmp_path, STDOUT)
    assert any("report_emoji" in p for p in problems), problems


def test_missing_dashboard_url_is_reported(tmp_path: Path) -> None:
    """artifact.dashboard_url 为空 → 报红."""
    _write_artifact(tmp_path, dashboard="")
    problems, _ = flows._audit_weekly_live_fields(tmp_path, STDOUT)
    assert any("dashboard_url" in p for p in problems), problems


def test_markdown_missing_dashboard_link_is_reported(tmp_path: Path) -> None:
    """markdown 没渲染 dashboard 链接 → 报红."""
    _write_artifact(
        tmp_path,
        md_body=f"# {EMOJI} {TITLE} — {WEEK}\n\n## 本周检视概况\n\n无链接\n",
    )
    problems, _ = flows._audit_weekly_live_fields(tmp_path, STDOUT)
    assert any("dashboard url" in p for p in problems), problems


def test_markdown_dashboard_url_must_be_markdown_link(tmp_path: Path) -> None:
    """dashboard 链接必须是 markdown 链接形态, 裸 URL 不算数."""
    _write_artifact(
        tmp_path,
        md_body=f"# {EMOJI} {TITLE} — {WEEK}\n\n{DASHBOARD}\n",
    )
    problems, _ = flows._audit_weekly_live_fields(tmp_path, STDOUT)
    assert any("markdown link" in p for p in problems), problems


def test_markdown_heading_missing_emoji_is_reported(tmp_path: Path) -> None:
    """markdown 首行标题缺 emoji → 报红."""
    _write_artifact(
        tmp_path,
        md_body=f"# {TITLE} — {WEEK}\n\n📈 **检视看板**: [{DASHBOARD}]({DASHBOARD})\n",
    )
    problems, _ = flows._audit_weekly_live_fields(tmp_path, STDOUT)
    assert any("emoji" in p for p in problems), problems


def test_markdown_heading_missing_week_label_is_reported(tmp_path: Path) -> None:
    """markdown 首行标题缺 week_label → 报红."""
    _write_artifact(
        tmp_path,
        md_body=f"# {EMOJI} {TITLE}\n\n📈 **检视看板**: [{DASHBOARD}]({DASHBOARD})\n",
    )
    problems, _ = flows._audit_weekly_live_fields(tmp_path, STDOUT)
    assert any("week_label" in p for p in problems), problems


def test_zero_chunks_is_reported(tmp_path: Path) -> None:
    """分片数为 0 → 报红（周报根本没生成可外发内容）."""
    _write_artifact(tmp_path)
    problems, _ = flows._audit_weekly_live_fields(
        tmp_path, "[ok] delivery     : dry_run, chunks=0\n"
    )
    assert any("chunks" in p for p in problems), problems


def test_missing_chunks_report_is_reported(tmp_path: Path) -> None:
    """CLI 没打印 chunks → 报红（分片口径失联）."""
    _write_artifact(tmp_path)
    problems, _ = flows._audit_weekly_live_fields(tmp_path, "[ok] done\n")
    assert any("chunks" in p for p in problems), problems


def test_no_artifact_files_is_reported(tmp_path: Path) -> None:
    """没有产物文件 → 报红（而不是静默跳过）."""
    problems, detail = flows._audit_weekly_live_fields(tmp_path, STDOUT)
    assert problems, detail


def test_broken_json_is_reported_not_raised(tmp_path: Path) -> None:
    """artifact JSON 损坏 → 报红且不抛异常."""
    (tmp_path / "weekly-bad.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "weekly-bad.md").write_text("# x\n", encoding="utf-8")
    problems, _ = flows._audit_weekly_live_fields(tmp_path, STDOUT)
    assert problems
    assert any("unreadable" in p for p in problems), problems


def test_multiple_weeks_use_latest_artifact(tmp_path: Path) -> None:
    """目录里有多周残留时, 审计最新一份（按文件名排序取末位）."""
    _write_artifact(tmp_path, week_label="2026-W44", title="旧标题")
    _write_artifact(tmp_path, week_label="2026-W45")
    problems, detail = flows._audit_weekly_live_fields(tmp_path, STDOUT)
    assert not problems, f"{problems} | {detail}"
    assert "2026-W45" in detail or "W45" in detail, detail
