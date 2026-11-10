"""scenario.rules.resolver — 规则装配：优先级/去重/覆盖/禁用/别名/缓存."""
from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from reviewagent.scenario.profile import load_profile
from reviewagent.scenario.rules import resolver
from reviewagent.scenario.rules.resolver import (
    active_rule_set,
    assemble_base,
    set_active_rule_set,
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


def test_verbatim_body_checksum_is_stable():
    """规则包正文的漂移护栏：改动 prompts 以外的规则正文须同步更新基线."""
    body = assemble_base(load_profile(None)).verbatim_body.encode()
    assert hashlib.sha256(body).hexdigest() == (
        "e3e87ab8d36a432ad0964581070e8f29886ebf2415bbd9db4c585967de40420b"
    )


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


def test_load_and_activate_invalidates_active_rule_set():
    import reviewagent.scenario.profile as profile_module

    profile = load_profile(None)
    set_active_rule_set(assemble_base(profile))
    assert resolver._ACTIVE is not None
    profile_module.load_and_activate()
    assert resolver._ACTIVE is None


# ---------------- Fix round 1：装配期命名空间 / 共享包 / 唯一 id ----------------


def test_rule_key_outside_namespace_fails(tmp_path):
    body = "| 规则键 | 严重度 | 摘要 |\n|---|---|---|\n| X-FOO | high | 命名空间外 |\n"
    profile = _write_profile(tmp_path, packs=[("rules/p.md", "t/p", 800, body)])
    with pytest.raises(ValueError, match="namespace"):
        assemble_base(profile)


def test_duplicate_pack_id_fails(tmp_path):
    body_a = "| 规则键 | 严重度 | 摘要 |\n|---|---|---|\n| R-A | high | a |\n"
    body_b = "| 规则键 | 严重度 | 摘要 |\n|---|---|---|\n| R-B | high | b |\n"
    profile = _write_profile(
        tmp_path,
        packs=[
            ("rules/a.md", "t/dup", 800, body_a),
            ("rules/b.md", "t/dup", 700, body_b),
        ],
    )
    with pytest.raises(ValueError, match="duplicate"):
        assemble_base(profile)


def test_shared_pack_must_declare_languages(tmp_path):
    # profile root 即 `_shared` 根：其下规则包视为共享包，必须声明语言。
    shared_dir = tmp_path / "profiles" / "_shared"
    pack = shared_dir / "rules" / "x.md"
    pack.parent.mkdir(parents=True)
    pack.write_text(
        "---\nschema_version: 1\nid: shared/x\npriority: 800\nrender: verbatim\n---\n"
        "| 规则键 | 严重度 | 摘要 |\n|---|---|---|\n| R-LOG | high | 共享日志 |\n",
        encoding="utf-8",
    )
    profile = replace(load_profile(None), root=shared_dir, rules=("rules/x.md",))
    with pytest.raises(ValueError, match="applies_to.languages"):
        assemble_base(profile)


# ---------------- Fix round 1：指纹 / disable+override / normalize_keys ----------------


def test_fingerprint_includes_pack_priority(tmp_path):
    body = "| 规则键 | 严重度 | 摘要 |\n|---|---|---|\n| R-DUP | high | 同正文 |\n"
    high = _write_profile(
        tmp_path / "a", name="a", packs=[("rules/p.md", "t/p", 800, body)]
    )
    low = _write_profile(
        tmp_path / "b", name="b", packs=[("rules/p.md", "t/p", 500, body)]
    )
    high_set, low_set = assemble_base(high), assemble_base(low)
    assert high_set.verbatim_body == low_set.verbatim_body
    assert high_set.index_by_key["R-DUP"].severity == low_set.index_by_key["R-DUP"].severity
    assert high_set.fingerprint != low_set.fingerprint


def test_override_on_disabled_key_is_noop(tmp_path):
    body = "| 规则键 | 严重度 | 摘要 |\n|---|---|---|\n| R-LOG | low | 日志 |\n"
    profile = _write_profile(
        tmp_path,
        packs=[("rules/p.md", "t/p", 800, body)],
        rule_overrides={"R-LOG": {"severity": "high"}},
        disable=["R-LOG"],
    )
    ruleset = assemble_base(profile)
    assert "R-LOG" not in ruleset.index_by_key


def _alias_profile(tmp_path: Path):
    body = (
        "| 规则键 | 严重度 | 摘要 | aliases |\n"
        "|---|---|---|---|\n"
        "| R-LOG | high | 日志 | R-LOGGING |\n"
    )
    return _write_profile(tmp_path, packs=[("rules/p.md", "t/p", 800, body)])


def test_normalize_keys_maps_alias_to_canonical(tmp_path):
    ruleset = assemble_base(_alias_profile(tmp_path))
    assert ruleset.normalize_keys("see R-LOGGING here") == ["R-LOG"]


def test_normalize_keys_dedupes_canonical_and_alias(tmp_path):
    ruleset = assemble_base(_alias_profile(tmp_path))
    assert ruleset.normalize_keys("R-LOG then R-LOGGING") == ["R-LOG"]


def test_normalize_keys_promotion_when_any_is_case_insensitive(tmp_path):
    promoted = replace(
        _alias_profile(tmp_path),
        rule_key_promotions=({"to_key": "R-PERF", "when_any": ["perf_counter"]},),
    )
    ruleset = assemble_base(promoted)
    assert ruleset.normalize_keys("use PERF_COUNTER") == ["R-PERF"]


def test_normalize_keys_promotion_when_all(tmp_path):
    promoted = replace(
        _alias_profile(tmp_path),
        rule_key_promotions=({"to_key": "R-LOCK", "when_all": ["lock", "file"]},),
    )
    ruleset = assemble_base(promoted)
    assert ruleset.normalize_keys("lock the file") == ["R-LOCK"]
    assert ruleset.normalize_keys("lock only") == []


def test_normalize_keys_promotion_skips_existing_key(tmp_path):
    promoted = replace(
        _alias_profile(tmp_path),
        rule_key_promotions=({"to_key": "R-LOG", "when_any": ["logging"]},),
    )
    ruleset = assemble_base(promoted)
    assert ruleset.normalize_keys("R-LOG for logging") == ["R-LOG"]


def test_assemble_base_takes_repo_prefix_from_runtime_config(monkeypatch):
    """仓库键前缀以运行配置为唯一来源（review.yaml / env → rule_key_prefix）."""
    from reviewagent import config as config_module
    from reviewagent.config import current

    profile = load_profile(None)
    monkeypatch.setattr(
        config_module, "_current", replace(current(), rule_key_prefix="SSD-RULE")
    )
    ruleset = assemble_base(profile)
    assert ruleset.key_patterns.is_repo("SSD-RULE-NO-BARE-PRINT") is True
    assert ruleset.key_patterns.is_repo("ZZZ-RULE-OTHER") is False

    monkeypatch.setattr(config_module, "_current", replace(current(), rule_key_prefix="auto"))
    auto_ruleset = assemble_base(profile)
    assert auto_ruleset.key_patterns.is_repo("ZZZ-RULE-OTHER") is True
