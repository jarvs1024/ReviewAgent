"""scenario.rules.resolver — 规则装配：优先级/去重/覆盖/禁用/别名/缓存."""
from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import frontmatter
import pytest
import yaml

from reviewagent.scenario.profile import load_profile
from reviewagent.scenario.rules import resolver
from reviewagent.scenario.rules.resolver import (
    active_rule_set,
    assemble_base,
    set_active_rule_set,
)

REPO = Path(__file__).resolve().parents[1]
LEGACY_BLOCKS = (
    REPO / "reviewagent" / "prompts" / "_general_rules_block.md",
    REPO / "profiles" / "ssd-python-autotest" / "prompts" / "_general_rules_block.md",
)


def _write_pack(root: Path, rel: str, *, pack_id: str, priority: int, body: str,
                render: str = "verbatim") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        f"schema_version: 1\nid: {pack_id}\npriority: {priority}\n"
        f"render: {render}\napplies_to:\n  languages: [python]\n"
        "---\n"
        f"{body}",
        encoding="utf-8",
    )


def _write_profile(
    tmp_path: Path,
    name: str = "alt",
    *,
    packs: list[tuple[str, str, int, str]],
    rule_overrides: dict | None = None,
    disable: list[str] | None = None,
    rule_keys: dict | None = None,
):
    """packs: [(rel, pack_id, priority, body), ...]（写入 profile 根目录）."""
    root = tmp_path / "profiles"
    profile_dir = root / name
    prompts = {"improve": "prompts/improve.md", "describe": "prompts/describe.md"}
    for rel in prompts.values():
        path = profile_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {rel}\n", encoding="utf-8")
    for rel, pack_id, priority, body in packs:
        _write_pack(profile_dir, rel, pack_id=pack_id, priority=priority, body=body)
    data = {
        "schema_version": 1,
        "name": name,
        "language": "python",
        "prompts": prompts,
        "rules": [rel for rel, _, _, _ in packs],
    }
    if rule_overrides is not None:
        data["rule_overrides"] = rule_overrides
    if disable is not None:
        data["disable"] = disable
    if rule_keys is not None:
        data["rule_keys"] = rule_keys
    (profile_dir / "profile.yaml").write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return load_profile(name, root=root)


# ---------------- 基础装配 ----------------


def test_base_ruleset_orders_and_indexes():
    ruleset = assemble_base(load_profile(None))
    assert ruleset.verbatim_body
    assert "R-LOG" in ruleset.index_by_key
    assert ruleset.sources[0]["kind"] == "profile"
    assert ruleset.sources[0]["pack_id"] == "ssd-python-autotest/core"
    assert ruleset.render == "verbatim"
    assert ruleset.alias_map["R-LOG"] == "R-LOG"
    assert len(ruleset.fingerprint) == 32
    keys = [rule.key for rule in ruleset.rules]
    assert [ruleset.index_by_key[key] for key in keys] == list(ruleset.rules)


def test_verbatim_body_matches_legacy_block_files():
    """临时双份文件中，RuleSet 正文必须与旧 block 文件逐字节一致（防漂移）."""
    body = assemble_base(load_profile(None)).verbatim_body.encode()
    assert hashlib.sha256(body).hexdigest() == (
        "84ed4563f513be9589e8ab4aa97c49d2699bc226348377455dd65a8b2fa9712c"
    )
    for legacy in LEGACY_BLOCKS:
        assert frontmatter.load(legacy).content.encode() == body, legacy


def test_rules_are_deduplicated_in_first_encounter_order():
    ruleset = assemble_base(load_profile(None))
    keys = [rule.key for rule in ruleset.rules]
    assert len(keys) == len(set(keys))
    assert "R-RES" in keys and "R-LOG" in keys


# ---------------- 覆盖 / 禁用 ----------------


def test_unknown_override_key_fails():
    profile = load_profile(None)
    bad = replace(profile, rule_overrides={"R-NOPE": {"severity": "high"}})
    with pytest.raises(ValueError, match="override"):
        assemble_base(bad)


def test_override_applies_only_severity_and_weight():
    profile = load_profile(None)
    base = assemble_base(profile)
    changed = assemble_base(
        replace(
            profile,
            rule_overrides={"R-LOG": {"severity": "high", "weight": 2.5, "title": "ignored"}},
        )
    )
    rule = changed.index_by_key["R-LOG"]
    assert (rule.severity, rule.weight) == ("high", 2.5)
    assert rule.title == base.index_by_key["R-LOG"].title
    assert changed.verbatim_body == base.verbatim_body
    assert changed.fingerprint != base.fingerprint
    assert changed.index_by_key["R-LOG"] in changed.rules


def test_disable_keeps_verbatim_body_but_drops_index():
    profile = load_profile(None)
    bad = replace(profile, disable=("R-LOG",))
    ruleset = assemble_base(bad)
    assert "R-LOG" not in ruleset.index_by_key
    assert "R-LOG" in ruleset.verbatim_body
    assert "R-LOG" not in [rule.key for rule in ruleset.rules]
    assert "R-LOG" not in ruleset.alias_map
    assert ruleset.verbatim_body == assemble_base(profile).verbatim_body


# ---------------- 优先级 / 别名 ----------------


def test_higher_priority_pack_wins_first_encounter(tmp_path):
    body_low = "| 规则键 | 严重度 | 摘要 |\n|---|---|---|\n| R-DUP | low | 低优先级 |\n"
    body_high = "| 规则键 | 严重度 | 摘要 |\n|---|---|---|\n| R-DUP | high | 高优先级 |\n"
    # 声明顺序把低优先级包放前面：排序后高优先级仍必须先装配。
    profile = _write_profile(
        tmp_path,
        packs=[
            ("rules/low.md", "t/low", 100, body_low),
            ("rules/high.md", "t/high", 900, body_high),
        ],
    )
    ruleset = assemble_base(profile)
    assert ruleset.index_by_key["R-DUP"].title == "高优先级"
    assert ruleset.index_by_key["R-DUP"].severity == "high"
    assert [rule.key for rule in ruleset.rules] == ["R-DUP"]
    assert [source["pack_id"] for source in ruleset.sources] == ["t/high", "t/low"]


def test_equal_priority_uses_profile_declaration_order(tmp_path):
    body_first = "| 规则键 | 严重度 | 摘要 |\n|---|---|---|\n| R-DUP | high | 第一个 |\n"
    body_second = "| 规则键 | 严重度 | 摘要 |\n|---|---|---|\n| R-DUP | low | 第二个 |\n"
    profile = _write_profile(
        tmp_path,
        packs=[
            ("rules/first.md", "t/first", 500, body_first),
            ("rules/second.md", "t/second", 500, body_second),
        ],
    )
    assert assemble_base(profile).index_by_key["R-DUP"].title == "第一个"


def test_alias_map_covers_canonical_and_aliases(tmp_path):
    body = (
        "| 规则键 | 严重度 | 摘要 | aliases |\n"
        "|---|---|---|---|\n"
        "| R-LOG | high | 日志 | log, logging |\n"
    )
    profile = _write_profile(tmp_path, packs=[("rules/p.md", "t/p", 800, body)])
    ruleset = assemble_base(profile)
    assert ruleset.alias_map["R-LOG"] == "R-LOG"
    assert ruleset.alias_map["log"] == "R-LOG"
    assert ruleset.alias_map["logging"] == "R-LOG"


# ---------------- 激活缓存 ----------------


def test_active_rule_set_is_cached_and_invalidatable():
    set_active_rule_set(None)
    first = active_rule_set()
    assert active_rule_set() is first
    set_active_rule_set(None)
    second = active_rule_set()
    assert second is not first
    assert second.fingerprint == first.fingerprint
    set_active_rule_set(None)


def test_deactivate_invalidates_active_rule_set():
    import reviewagent.scenario.profile as profile_module

    profile = load_profile(None)
    set_active_rule_set(assemble_base(profile))
    assert resolver._ACTIVE is not None
    profile_module.deactivate()
    assert resolver._ACTIVE is None
