"""scenario.prompts — 按 profile 加载 prompt / block / 模板渲染."""

from __future__ import annotations

import hashlib
from pathlib import Path

import frontmatter
import pytest

from reviewagent.prompts.loader import load, load_block
from reviewagent.scenario.profile import load_profile
from reviewagent.scenario.prompts import (
    available,
    load_template,
    render_template,
)

REPO = Path(__file__).resolve().parents[1]
LEGACY_PROMPTS = REPO / "reviewagent" / "prompts"

PUBLIC_PROMPTS = (
    "improve",
    "describe",
    "weekly_inspection_summary",
    "weekly_change_summary",
    "weekly_quality_scan",
)
ALL_PROMPTS = PUBLIC_PROMPTS + ("_general_rules_block",)


@pytest.fixture(scope="module")
def profile():
    return load_profile(None)


def test_prompt_bytes_match_legacy_source():
    legacy = (LEGACY_PROMPTS / "improve.md").read_text(encoding="utf-8")
    assert load("improve")["prompt"] in legacy
    assert hashlib.sha256(load_block("_general_rules_block").encode()).hexdigest() == (
        "84ed4563f513be9589e8ab4aa97c49d2699bc226348377455dd65a8b2fa9712c"
    )


def test_profile_prompt_files_are_byte_identical(profile):
    for name in ALL_PROMPTS:
        legacy = (LEGACY_PROMPTS / f"{name}.md").read_bytes()
        migrated = (profile.root / "prompts" / f"{name}.md").read_bytes()
        assert legacy == migrated, name


@pytest.mark.parametrize("name", PUBLIC_PROMPTS)
def test_load_fields_match_legacy_frontmatter(name):
    legacy_md = frontmatter.load(LEGACY_PROMPTS / f"{name}.md")
    meta = load(name)
    assert meta["name"] == (legacy_md.metadata.get("name") or name)
    assert meta["description"] == legacy_md.metadata.get("description", "")
    assert meta["output_schema"] == legacy_md.metadata.get("output_schema")
    assert meta["tools"] == legacy_md.metadata.get("tools", {})
    assert meta["tools_disabled"] == [
        k for k, v in legacy_md.metadata.get("tools", {}).items() if v is False
    ]
    assert meta["prompt"] == legacy_md.content


def test_available_lists_public_prompts_only(profile):
    assert set(available(profile)) == set(PUBLIC_PROMPTS)


def test_load_block_requires_underscore_prefix():
    with pytest.raises(ValueError, match="下划线"):
        load_block("improve")


def test_load_template_reads_profile_file(profile):
    text = load_template("prompts/improve.md", profile)
    assert text == (profile.root / "prompts" / "improve.md").read_text(encoding="utf-8")
    trimmed = load_template("prompts/improve.md", profile, trim_trailing_newline=True)
    assert trimmed == text.rstrip("\n")
    assert not trimmed.endswith("\n")


def test_load_template_rejects_path_escape(profile):
    with pytest.raises(ValueError, match="escapes profile root"):
        load_template("../profile.yaml", profile)
    with pytest.raises(ValueError, match="escapes profile root"):
        load_template("/etc/hostname", profile)


def test_template_placeholder_is_strict():
    assert render_template("a{{x}}b", {"x": "1"}, template_path="t") == "a1b"

    with pytest.raises(ValueError, match="missing") as missing:
        render_template("a{{x}}", {}, template_path="t")
    assert "t" in str(missing.value)

    with pytest.raises(ValueError, match="extra") as extra:
        render_template("a", {"x": "1"}, template_path="t")
    assert "t" in str(extra.value)


def test_render_template_rejects_unknown_identifier():
    # 非法占位符名（数字开头）不属于 {{name}} 语法，保持原样
    assert render_template("a{{1x}}", {}, template_path="t") == "a{{1x}}"
