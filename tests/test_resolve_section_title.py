"""renderer._resolve_section_title 占位符兜底 — 防未启用分支残留 {branch} 字面量."""
from __future__ import annotations

import os
os.environ.setdefault("GITLAB_URL", "http://x")
os.environ.setdefault("REVIEWAGENT_WEBHOOK_PORT", "3000")
os.environ.setdefault("TELEMETRY_DB", "/tmp/x.db")
os.environ.setdefault("LLM_PROVIDER", "stub")
os.environ.setdefault("LOG_LEVEL", "WARNING")

from reviewagent.reporting.collectors.base import SectionResult
from reviewagent.reporting.renderer import _resolve_section_title, SECTION_TITLES


def test_merged_mrs_unenabled_branch_placeholder_resolved():
    """未启用分支 (section=None) 也要把 {branch} 替成 main, 避免字面残留."""
    title = _resolve_section_title("merged_mrs", None)
    assert "{branch}" not in title
    assert "main" in title
    assert title == "二、本周 main 变更汇总"


def test_merged_mrs_with_target_branch_in_data():
    """已采集数据但 status=failed 时, 也能从 data.target_branch 拿."""
    sr = SectionResult(
        status="failed", data={"target_branch": "release/2026Q3"}, error="x"
    )
    title = _resolve_section_title("merged_mrs", sr)
    assert title == "二、本周 release/2026Q3 变更汇总"


def test_merged_mrs_missing_target_branch_fallback_main():
    sr = SectionResult(status="ok", data={}, markdown=None)
    title = _resolve_section_title("merged_mrs", sr)
    assert title == "二、本周 main 变更汇总"


def test_other_sections_untouched():
    """telemetry / repo_scan 不含占位符, 行为不变."""
    assert _resolve_section_title("telemetry", None) == SECTION_TITLES["telemetry"]
    assert _resolve_section_title("repo_scan", None) == SECTION_TITLES["repo_scan"]


def test_demote_llm_headings_still_called_for_repo_scan():
    """段三 dead-code 去除后, _demote_llm_headings 仍正常."""
    from reviewagent.reporting.renderer import _demote_llm_headings
    assert _demote_llm_headings("# \u9ad8\u98ce\u9669\u6a21\u5757") == "**\u9ad8\u98ce\u9669\u6a21\u5757**"
    assert _demote_llm_headings("## \u9ad8\u98ce\u9669") == "**\u9ad8\u98ce\u9669**"
    assert _demote_llm_headings(None) == ""
