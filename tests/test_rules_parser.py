"""scenario.rules — verbatim/structured 规则包解析、schema 校验与路径包含性."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from reviewagent.scenario.rules.parser import parse_pack

ROOT = Path(__file__).resolve().parents[1] / "profiles/ssd-python-autotest"

_STRUCTURED_FM = (
    "schema_version: 1\n"
    "id: test/pack\n"
    "priority: 800\n"
    "render: structured\n"
    "applies_to:\n"
    "  languages: [python]\n"
)


def _make_pack(tmp_path: Path, frontmatter: str, body: str, name: str = "pack.md") -> Path:
    path = tmp_path / name
    path.write_text(f"---\n{frontmatter}---\n{body}", encoding="utf-8")
    return path


def test_verbatim_body_is_byte_identical():
    pack = parse_pack(ROOT / "rules/general_rules.md", language="python", root=ROOT)
    assert pack.render == "verbatim"
    assert len(pack.body) == 6904
    assert len(pack.body.encode()) == 12690
    assert hashlib.sha256(pack.body.encode()).hexdigest() == (
        "963ce3843726312aae2647371f98518e716add4bf96f349b58ad62b2cb97ba96"
    )
    assert {"R-RES", "R-LOG", "R-LOOP"} <= set(pack.index_by_key)


def test_unknown_frontmatter_field_fails(tmp_path):
    pack = tmp_path / "bad.md"
    pack.write_text("---\nschema_version: 1\nid: x\npriority: 800\nrender: verbatim\nnope: 1\n---\n")
    with pytest.raises(ValueError, match="unknown frontmatter"):
        parse_pack(pack, language="python", root=tmp_path)


def test_unsupported_schema_version_fails(tmp_path):
    pack = tmp_path / "bad.md"
    pack.write_text("---\nschema_version: 2\nid: x\npriority: 800\nrender: verbatim\n---\n")
    with pytest.raises(ValueError, match="schema_version"):
        parse_pack(pack, language="python", root=tmp_path)


def test_structured_heading_with_title_parses_examples(tmp_path):
    pack = tmp_path / "structured.md"
    pack.write_text(
        "---\n"
        "schema_version: 1\n"
        "id: test/structured\n"
        "priority: 800\n"
        "render: structured\n"
        "applies_to:\n  languages: [python]\n"
        "---\n"
        "### R-LOG 日志规范\n\n"
        "日志必须可观测。\n\n"
        "```bad\n"
        'log.info("failed")\n'
        "```\n\n"
        "```good\n"
        'log.error("nvme read failed: dev=%s, lba=%d", dev, lba)\n'
        "```\n",
        encoding="utf-8",
    )
    pack = parse_pack(pack, language="python", root=tmp_path)
    assert pack.rules[0].key == "R-LOG"
    assert [ex["kind"] for ex in pack.rules[0].examples] == ["bad", "good"]


def test_pack_outside_root_is_rejected(tmp_path):
    root = tmp_path / "profile"
    root.mkdir()
    pack = tmp_path / "escape.md"
    pack.write_text("---\nschema_version: 1\nid: x\npriority: 800\nrender: verbatim\n---\n")
    with pytest.raises(ValueError, match="escapes profile root"):
        parse_pack(pack, language="python", root=root)


# --- 表头列映射 ---------------------------------------------------------------


def test_modern_table_maps_severity_title_applies_to(tmp_path):
    body = (
        "| 规则键 | 严重度 | 摘要 |\n"
        "|---|---|---|\n"
        "| R-LOG | high | 日志必须包含硬件状态与复现步骤 |\n"
    )
    frontmatter = _STRUCTURED_FM + "  paths: ['tests/**', '**/test_*.py']\n"
    pack = parse_pack(_make_pack(tmp_path, frontmatter, body), language="python", root=tmp_path)
    assert len(pack.rules) == 1
    rule = pack.rules[0]
    assert rule.key == "R-LOG"
    assert rule.severity == "high"
    assert rule.title == "日志必须包含硬件状态与复现步骤"
    assert rule.body == ""
    assert rule.applies_to == {
        "languages": ["python"],
        "paths": ["tests/**", "**/test_*.py"],
    }


def test_legacy_table_maps_category_title_and_body(tmp_path):
    body = (
        "| 键 | 类别 | 反模式（命中条件）| 修复方向 |\n"
        "|---|---|---|---|\n"
        "| `R-RES` | 资源句柄 | 句柄泄漏 | 用 with 托管 |\n"
    )
    pack = parse_pack(_make_pack(tmp_path, _STRUCTURED_FM, body), language="python", root=tmp_path)
    rule = pack.index_by_key["R-RES"]
    assert rule.title == "资源句柄"
    assert rule.body == "句柄泄漏\n\n用 with 托管"


def test_table_and_section_merge_into_single_rule(tmp_path):
    body = (
        "| 规则键 | 严重度 | 摘要 |\n"
        "|---|---|---|\n"
        "| R-LOG | high | 摘要文字 |\n\n"
        "### R-LOG 日志规范\n\n"
        "日志必须可观测。\n\n"
        "```bad\n"
        'log.info("failed")\n'
        "```\n"
    )
    pack = parse_pack(_make_pack(tmp_path, _STRUCTURED_FM, body), language="python", root=tmp_path)
    assert len(pack.rules) == 1
    rule = pack.rules[0]
    assert rule.key == "R-LOG"
    assert rule.severity == "high"          # 表格供严重度
    assert rule.title == "日志规范"          # 段落标题优先于摘要
    assert "日志必须可观测" in rule.body
    assert [ex["kind"] for ex in rule.examples] == ["bad"]


def test_optional_columns_are_parsed(tmp_path):
    body = (
        "| 规则键 | 严重度 | 摘要 | aliases | actions | detect_hints |\n"
        "|---|---|---|---|---|---|\n"
        "| R-LOG | medium | 日志 | log, logging | refactor; test | print， log |\n"
    )
    pack = parse_pack(_make_pack(tmp_path, _STRUCTURED_FM, body), language="python", root=tmp_path)
    rule = pack.rules[0]
    assert rule.aliases == ("log", "logging")
    assert rule.actions == ("refactor", "test")
    assert rule.detect_hints == ("print", "log")


# --- 段落切分 -----------------------------------------------------------------


def test_section_heading_title_captured(tmp_path):
    body = "### R-LOG 日志规范\n\n正文。\n"
    pack = parse_pack(_make_pack(tmp_path, _STRUCTURED_FM, body), language="python", root=tmp_path)
    assert pack.rules[0].key == "R-LOG"
    assert pack.rules[0].title == "日志规范"


def test_heading_inside_fence_does_not_split(tmp_path):
    body = (
        "### R-EXAMPLE 示例\n\n"
        "```bad\n"
        "### R-OTHER 内嵌标题\n"
        "bad()\n"
        "```\n\n"
        "```good\n"
        "good()\n"
        "```\n"
    )
    pack = parse_pack(_make_pack(tmp_path, _STRUCTURED_FM, body), language="python", root=tmp_path)
    assert [rule.key for rule in pack.rules] == ["R-EXAMPLE"]
    assert "### R-OTHER 内嵌标题" in pack.rules[0].body
    assert [ex["kind"] for ex in pack.rules[0].examples] == ["bad", "good"]


def test_non_rule_heading_is_package_prose(tmp_path):
    body = "### 🟡 优先 3\n\n包级散文。\n\n### R-LOG 日志规范\n\n正文。\n"
    pack = parse_pack(_make_pack(tmp_path, _STRUCTURED_FM, body), language="python", root=tmp_path)
    assert [rule.key for rule in pack.rules] == ["R-LOG"]


# --- 校验失败路径 -------------------------------------------------------------


@pytest.mark.parametrize("pattern", ["/abs/x.py", "../up.py", "a\\b.py"])
def test_applies_to_invalid_paths_raise(tmp_path, pattern):
    frontmatter = _STRUCTURED_FM + f"  paths: ['{pattern}']\n"
    with pytest.raises(ValueError, match="applies_to.paths"):
        parse_pack(
            _make_pack(tmp_path, frontmatter, "### R-X 标题\n正文。\n"),
            language="python",
            root=tmp_path,
        )


def test_priority_out_of_range_raises(tmp_path):
    frontmatter = _STRUCTURED_FM.replace("priority: 800", "priority: 2000")
    with pytest.raises(ValueError, match="priority out of range"):
        parse_pack(_make_pack(tmp_path, frontmatter, ""), language="python", root=tmp_path)


def test_non_numeric_priority_raises(tmp_path):
    frontmatter = _STRUCTURED_FM.replace("priority: 800", "priority: eventually")
    with pytest.raises(ValueError, match="invalid priority"):
        parse_pack(_make_pack(tmp_path, frontmatter, ""), language="python", root=tmp_path)


def test_unknown_render_raises(tmp_path):
    frontmatter = _STRUCTURED_FM.replace("render: structured", "render: fancy")
    with pytest.raises(ValueError, match="unknown render"):
        parse_pack(_make_pack(tmp_path, frontmatter, ""), language="python", root=tmp_path)


def test_duplicate_table_key_raises(tmp_path):
    body = (
        "| 规则键 | 严重度 | 摘要 |\n"
        "|---|---|---|\n"
        "| R-LOG | high | 一 |\n"
        "| R-LOG | low | 二 |\n"
    )
    with pytest.raises(ValueError, match="duplicate rule key"):
        parse_pack(_make_pack(tmp_path, _STRUCTURED_FM, body), language="python", root=tmp_path)


def test_duplicate_section_key_raises(tmp_path):
    body = "### R-LOG 一\n\n正文。\n\n### R-LOG 二\n\n正文。\n"
    with pytest.raises(ValueError, match="duplicate rule section"):
        parse_pack(_make_pack(tmp_path, _STRUCTURED_FM, body), language="python", root=tmp_path)


# --- 语言 / 标题边界 ----------------------------------------------------------


def test_language_mismatch_empties_rules_and_body():
    pack = parse_pack(ROOT / "rules/general_rules.md", language="cpp", root=ROOT)
    assert pack.rules == ()
    assert pack.body == ""
    assert pack.id == "ssd-python-autotest/core"
    assert pack.priority == 800


def test_empty_title_falls_back_to_pack_id(tmp_path):
    frontmatter = (
        "schema_version: 1\n"
        "id: test/empty-title\n"
        'title: ""\n'
        "priority: 800\n"
        "render: verbatim\n"
    )
    pack = parse_pack(_make_pack(tmp_path, frontmatter, ""), language="python", root=tmp_path)
    assert pack.title == "test/empty-title"
