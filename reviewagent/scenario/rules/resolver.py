"""规则装配（spec §6.5）：把 profile 引用的规则包合并成不可变 `RuleSet`.

装配语义：

* 规则包按 `priority` 降序、同优先级按 profile 声明顺序排列；
* 同名规则键 **首次命中获胜**（高优先级包先装配，低优先级包不得覆盖）；
* `profile.disable` 中的键不进入 `index_by_key` / `rules` / `alias_map`，
  但 **不改变** `verbatim_body`（正文由包正文拼接，逐字节保留）；
* `profile.rule_overrides` 只允许调整 `severity` / `weight`，引用未知键即
  `ValueError`（消息含 "override"）；
* `fingerprint` = 正文摘要 + 规则 `(key, severity, weight)` 序列摘要。

`active_rule_set()` 持有启动期装配的缓存；`set_active_rule_set(None)` 用于
profile 切换时失效（不得让新 profile 看到上一个 profile 的规则）。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any

from reviewagent.scenario.profile import Profile
from reviewagent.scenario.rules.keys import KeyPatterns, build_key_patterns
from reviewagent.scenario.rules.model import Rule
from reviewagent.scenario.rules.parser import parse_pack

_OVERRIDABLE = frozenset({"severity", "weight"})


@dataclass(frozen=True)
class RuleSet:
    rules: tuple[Rule, ...]
    index_by_key: dict[str, Rule]
    alias_map: dict[str, str]
    promotions: tuple[dict[str, Any], ...]
    key_patterns: KeyPatterns
    sources: tuple[dict[str, Any], ...]
    render: str
    verbatim_body: str
    fingerprint: str

    def normalize_keys(self, text: str) -> list[str]:
        """抽取文本中的规则键，并按 `rule_key_promotions` 补充提升键."""
        keys = self.key_patterns.extract(text)
        lowered = (text or "").lower()
        for promo in self.promotions:
            to_key = promo.get("to_key")
            if not to_key or to_key in keys:
                continue
            when_any = promo.get("when_any")
            when_all = promo.get("when_all")
            if when_any and any(str(word).lower() in lowered for word in when_any):
                keys.append(to_key)
            elif when_all and all(str(word).lower() in lowered for word in when_all):
                keys.append(to_key)
        return keys


def assemble_base(profile: Profile) -> RuleSet:
    """装配基础 `RuleSet`（profile 规则包 -> 去重规则 + 正文 + 键模式）."""
    pairs = [
        (order, parse_pack(profile.root / rel, language=profile.language, root=profile.root))
        for order, rel in enumerate(profile.rules)
    ]
    pairs.sort(key=lambda item: (-item[1].priority, item[0]))
    packs = [pack for _, pack in pairs]

    disabled = set(profile.disable)
    index: dict[str, Rule] = {}
    ordered_keys: list[str] = []
    for pack in packs:
        for rule in pack.rules:
            if rule.key in disabled or rule.key in index:
                # 首次命中获胜：高优先级包先到，低优先级包/同包重复键不得覆盖。
                continue
            index[rule.key] = rule
            ordered_keys.append(rule.key)

    for key, override in profile.rule_overrides.items():
        if key not in index:
            raise ValueError(f"rule override references unknown key: {key}")
        changes = {k: v for k, v in override.items() if k in _OVERRIDABLE}
        if changes:
            index[key] = replace(index[key], **changes)

    rules = tuple(index[key] for key in ordered_keys)

    alias_map: dict[str, str] = {}
    for rule in rules:
        for alias in rule.aliases:
            alias_map.setdefault(alias, rule.key)
    # 规范键永远指向自身（别名不得覆盖真实规则键）。
    for rule in rules:
        alias_map[rule.key] = rule.key

    body = "\n\n".join(pack.body for pack in packs if pack.body)
    payload = json.dumps(
        [[rule.key, rule.severity, rule.weight] for rule in rules],
        sort_keys=True,
        ensure_ascii=False,
    )
    fingerprint = (
        hashlib.sha256(body.encode()).hexdigest()[:16]
        + hashlib.sha256(payload.encode()).hexdigest()[:16]
    )
    return RuleSet(
        rules=rules,
        index_by_key=index,
        alias_map=alias_map,
        promotions=profile.rule_key_promotions,
        key_patterns=build_key_patterns(profile.rule_keys),
        sources=tuple(
            {"kind": "profile", "path": pack.path, "pack_id": pack.id} for pack in packs
        ),
        render=packs[0].render if packs else "verbatim",
        verbatim_body=body,
        fingerprint=fingerprint,
    )


_ACTIVE: RuleSet | None = None


def set_active_rule_set(rule_set: RuleSet | None) -> None:
    """设置/清空启动期装配的 RuleSet 缓存（传 None 表示失效）."""
    global _ACTIVE
    _ACTIVE = rule_set


def active_rule_set() -> RuleSet:
    """返回当前激活的 RuleSet；未装配时按激活 profile 惰性装配."""
    global _ACTIVE
    if _ACTIVE is None:
        from reviewagent.scenario.profile import active_or_load

        _ACTIVE = assemble_base(active_or_load())
    return _ACTIVE
