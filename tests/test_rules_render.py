"""scenario.rules.render — verbatim 逐字节返回 / 预算护栏 / structured 表格."""
from __future__ import annotations

from pathlib import Path

import frontmatter
import pytest
import yaml

from reviewagent.scenario.profile import load_profile
from reviewagent.scenario.rules.render import render_rule_block
from reviewagent.scenario.rules.resolver import assemble_base

REPO = Path(__file__).resolve().parents[1]


def _write_pack(root: Path, body: str) -> None:
    path = root / "rules" / "p.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "schema_version: 1\nid: t/render\npriority: 800\nrender: structured\n"
        "applies_to:\n  languages: [python]\n"
        "---\n"
        f"{body}",
        encoding="utf-8",
    )


def _load_structured_profile(tmp_path: Path):
    root = tmp_path / "profiles"
    profile_dir = root / "alt"
    prompts = {"improve": "prompts/improve.md", "describe": "prompts/describe.md"}
    for rel in prompts.values():
        path = profile_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {rel}\n", encoding="utf-8")
    (profile_dir / "profile.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "name": "alt",
                "language": "python",
                "prompts": prompts,
                "rules": ["rules/p.md"],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    _write_pack(
        profile_dir,
        "| 规则键 | 严重度 | 摘要 |\n"
        "|---|---|---|\n"
        "| R-LOG | high | 日志可观测 |\n"
        "| R-RES | low | 资源托管 |\n",
    )
    return load_profile("alt", root=root)


def test_verbatim_returns_exact_body():
    profile = load_profile(None)
    ruleset = assemble_base(profile)
    rendered = render_rule_block(ruleset, profile=profile)
    assert rendered == ruleset.verbatim_body
    legacy = frontmatter.load(REPO / "reviewagent/prompts/_general_rules_block.md").content
    assert rendered == legacy


def test_verbatim_over_budget_raises_with_profile_and_sizes():
    profile = load_profile(None)
    ruleset = assemble_base(profile)
    size = len(ruleset.verbatim_body.encode())
    with pytest.raises(ValueError, match=profile.name) as excinfo:
        render_rule_block(ruleset, profile=profile, budget=size - 1)
    assert str(size) in str(excinfo.value)
    assert str(size - 1) in str(excinfo.value)
    # 恰好等于预算不算超限
    assert render_rule_block(ruleset, profile=profile, budget=size) == ruleset.verbatim_body


def test_structured_renders_rows_in_rules_order(tmp_path):
    profile = _load_structured_profile(tmp_path)
    ruleset = assemble_base(profile)
    assert ruleset.render == "structured"
    rendered = render_rule_block(ruleset, profile=profile)
    lines = rendered.splitlines()
    assert lines[0] == "| 规则键 | 严重度 | 摘要 |"
    assert lines[1] == "|---|---|---|"
    assert [rule.key for rule in ruleset.rules] == ["R-LOG", "R-RES"]
    assert lines[2:] == [
        "| R-LOG | high | 日志可观测 |",
        "| R-RES | low | 资源托管 |",
    ]
