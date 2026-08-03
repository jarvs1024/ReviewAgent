"""sync_qoder_agents — frontmatter materialisation into .qoder/agents."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from scripts.sync_qoder_agents import sync_qoder_agents


def _write_prompt(prompts_dir: Path, name: str, body: str, **front) -> None:
    p = prompts_dir / f"{name}.md"
    fm_lines = ["---"]
    fm_lines.append(f"name: {front.get('name', name)}")
    if "description" in front:
        fm_lines.append(f"description: {front['description']}")
    if "tools" in front:
        fm_lines.append("tools:")
        for k, v in front["tools"].items():
            fm_lines.append(f"  {k}: {str(v).lower()}")
    fm_lines.append("---")
    p.write_text("\n".join(fm_lines) + "\n\n" + textwrap.dedent(body))


def test_writes_one_md_per_prompt(tmp_path: Path) -> None:
    prompts = tmp_path / "prompts"
    agents = tmp_path / "agents"
    prompts.mkdir()
    _write_prompt(prompts, "improve", "You are a code improver.", description="Reviewer")
    _write_prompt(prompts, "describe", "You write MR descriptions.", description="Describer")
    written = sync_qoder_agents(prompts, agents)
    assert sorted(p.name for p in written) == ["describe.md", "improve.md"]


def test_disallowed_tools_mapping(tmp_path: Path) -> None:
    prompts = tmp_path / "prompts"
    agents = tmp_path / "agents"
    prompts.mkdir()
    _write_prompt(
        prompts, "improve", "body",
        description="d",
        tools={"write": False, "edit": False, "bash": False, "webfetch": False},
    )
    sync_qoder_agents(prompts, agents)
    text = (agents / "improve.md").read_text()
    assert "disallowedTools: [Write, Edit, Bash, WebFetch, WebSearch]" in text


def test_hardened_safety_fields_present(tmp_path: Path) -> None:
    prompts = tmp_path / "prompts"
    agents = tmp_path / "agents"
    prompts.mkdir()
    _write_prompt(prompts, "improve", "body", description="d")
    sync_qoder_agents(prompts, agents)
    text = (agents / "improve.md").read_text()
    assert "tools: [Read, Grep, Glob, Agent]" in text
    assert "permissionMode: default" in text
    assert "maxTurns: 3" in text
    assert "model: inherit" in text


def test_skips_underscore_files(tmp_path: Path) -> None:
    prompts = tmp_path / "prompts"
    agents = tmp_path / "agents"
    prompts.mkdir()
    _write_prompt(prompts, "_general_rules_block", "private block", description="d")
    _write_prompt(prompts, "improve", "public", description="d")
    written = sync_qoder_agents(prompts, agents)
    assert [p.name for p in written] == ["improve.md"]


def test_idempotent_no_rewrite_when_unchanged(tmp_path: Path) -> None:
    prompts = tmp_path / "prompts"
    agents = tmp_path / "agents"
    prompts.mkdir()
    _write_prompt(prompts, "improve", "body", description="d")
    sync_qoder_agents(prompts, agents)
    first_mtime = (agents / "improve.md").stat().st_mtime_ns
    sync_qoder_agents(prompts, agents)
    second_mtime = (agents / "improve.md").stat().st_mtime_ns
    assert first_mtime == second_mtime
