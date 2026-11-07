"""T20: `docs/RULES.md` 的示例必须是可被 CI 真实解析的规则包.

文档里整包示例用四个反引号围栏（内部正反例仍是三个），否则内层围栏会提前
截断文档解析。这里把每个 ````markdown` 块落盘后交给 `parse_pack`，确保
「文档即模板」不会随代码漂移。
"""
from __future__ import annotations

import re
from pathlib import Path

from reviewagent.scenario.rules.parser import parse_pack

REPO_ROOT = Path(__file__).resolve().parents[1]
RULES_DOC = REPO_ROOT / "docs" / "RULES.md"
FENCE4 = "`" * 4


def _pack_blocks() -> list[str]:
    text = RULES_DOC.read_text(encoding="utf-8")
    return re.findall(FENCE4 + r"markdown\n(.*?)\n" + FENCE4, text, re.S)


def _parse_blocks(tmp_path: Path):
    packs = []
    for i, block in enumerate(_pack_blocks()):
        path = tmp_path / f"example_{i}.md"
        path.write_text(block, encoding="utf-8")
        packs.append(parse_pack(path, language="python", root=tmp_path))
    return packs


def test_rules_md_examples_parse(tmp_path):
    packs = _parse_blocks(tmp_path)
    assert len(packs) >= 2  # verbatim + structured 各一个
    for index, pack in enumerate(packs):
        assert pack.rules, f"example_{index} parsed to zero rules"


def test_rules_md_covers_both_render_modes(tmp_path):
    packs = _parse_blocks(tmp_path)
    assert [pack.render for pack in packs[:2]] == ["verbatim", "structured"]


def test_rules_md_examples_use_profile_namespace(tmp_path):
    """示例键必须落在当前 profile 的仓库命名空间（`SSD-RULE-*`）。"""
    for pack in _parse_blocks(tmp_path):
        for rule in pack.rules:
            assert rule.key.startswith("SSD-RULE-"), rule.key


def test_rules_md_structured_example_has_bad_and_good_examples(tmp_path):
    structured = _parse_blocks(tmp_path)[1]
    kinds = {example["kind"] for rule in structured.rules for example in rule.examples}
    assert {"bad", "good"} <= kinds
