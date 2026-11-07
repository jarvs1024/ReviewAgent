"""scenario.rules — verbatim/structured 规则包解析、schema 校验与路径包含性."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from reviewagent.scenario.rules.parser import parse_pack

ROOT = Path(__file__).resolve().parents[1] / "profiles/ssd-python-autotest"


def test_verbatim_body_is_byte_identical():
    pack = parse_pack(ROOT / "rules/general_rules.md", language="python", root=ROOT)
    assert pack.render == "verbatim"
    assert len(pack.body) == 3944
    assert hashlib.sha256(pack.body.encode()).hexdigest() == (
        "84ed4563f513be9589e8ab4aa97c49d2699bc226348377455dd65a8b2fa9712c"
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
