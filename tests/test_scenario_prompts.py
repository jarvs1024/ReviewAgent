"""scenario.prompts — 按 profile 加载 prompt / block / 模板渲染."""

from __future__ import annotations

import hashlib
from pathlib import Path

import frontmatter
import pytest
import yaml

from reviewagent.prompts.loader import load, load_block
from reviewagent.scenario.profile import load_profile
from reviewagent.scenario.prompts import (
    available,
    load_template,
    render_template,
)
from reviewagent.scenario.prompts import load_block as scenario_load_block

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


def test_render_template_allows_whitespace_and_reports_profile():
    assert render_template("a{{ x }}b", {"x": "1"}, template_path="t") == "a1b"
    with pytest.raises(ValueError, match="missing") as excinfo:
        render_template("{{ y }}", {}, template_path="t", profile_name="alt")
    assert "alt" in str(excinfo.value)


# ---------- Fix round 1：_shared 回退 / 目录穿越 / available 键推导 ----------


def _make_tmp_profile(tmp_path: Path, name: str = "alt"):
    root = tmp_path / "profiles"
    profile_dir = root / name
    prompts = {"improve": "prompts/improve.md", "describe": "prompts/describe.md"}
    for rel in prompts.values():
        path = profile_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\nname: {Path(rel).stem}\n---\nbody\n", encoding="utf-8")
    data = {
        "schema_version": 1,
        "name": name,
        "language": "python",
        "prompts": prompts,
    }
    (profile_dir / "profile.yaml").write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return load_profile(name, root=root)


def test_load_block_no_shared_fallback(tmp_path):
    """_shared 目录已随 preset 层删除：block 只在 profile 根内查找."""
    profile = _make_tmp_profile(tmp_path)
    shared = profile.root.parent / "_shared" / "prompts"
    shared.mkdir(parents=True)
    (shared / "_shared_rules.md").write_text("shared block body\n", encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="not found in profile"):
        scenario_load_block("_shared_rules", profile)


def test_load_block_rejects_path_traversal(tmp_path):
    profile = _make_tmp_profile(tmp_path)
    with pytest.raises(ValueError, match="invalid prompt name"):
        scenario_load_block("_../escape", profile)
    with pytest.raises(ValueError, match="invalid prompt name"):
        scenario_load_block("_..\\escape", profile)


def test_load_rejects_path_traversal():
    with pytest.raises(ValueError, match="invalid prompt name"):
        load("../improve")
    with pytest.raises(ValueError, match="invalid prompt name"):
        load(".hidden")


def test_available_uses_profile_keys_not_disk(tmp_path):
    profile = _make_tmp_profile(tmp_path)
    (profile.root / "prompts" / "stray.md").write_text("stray\n", encoding="utf-8")
    assert available(profile) == ["describe", "improve"]
