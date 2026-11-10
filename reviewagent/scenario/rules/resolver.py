"""规则装配（spec §6.5）：把 profile 引用的规则包合并成不可变 `RuleSet`.

装配语义：

* 规则包按 `priority` 降序、同优先级按 profile 声明顺序排列；
* 同名规则键 **首次命中获胜**（高优先级包先装配，低优先级包不得覆盖）；
* `profile.disable` 中的键不进入 `index_by_key` / `rules` / `alias_map`，
  但 **不改变** `verbatim_body`（正文由包正文拼接，逐字节保留）；
* `profile.rule_overrides` 只允许调整 `severity` / `weight`，引用未知键即
  `ValueError`（消息含 "override"）；已 `disable` 的键上的覆盖是 no-op；
* 装配期校验：规则键必须落在 profile 命名空间（内置 `R-*` 或仓库
  `<prefix>-RULE-*`）、`_shared` 下的共享包必须声明 `applies_to.languages`、
  合并范围内规则包 `id` 不得重复；
* `fingerprint` = 正文摘要 + 规则 `(key, severity, weight, priority, pack_id)`
  序列摘要。
* `with_repo()` 叠加请求级仓库规则（同键覆盖 severity/title），指纹追加
  `:repo:<digest>`，但 `verbatim_body` 逐字节不变（仓库原文经 `{{repo_rules}}`）。

`active_rule_set()` 持有启动期装配的缓存；`set_active_rule_set(None)` 用于
profile 切换时失效（不得让新 profile 看到上一个 profile 的规则）。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from reviewagent.scenario.profile import Profile
from reviewagent.scenario.rules.keys import KeyPatterns, build_key_patterns
from reviewagent.scenario.rules.model import Rule
from reviewagent.scenario.rules.parser import parse_pack

_OVERRIDABLE = frozenset({"severity", "weight"})
_SHARED = "_shared"


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
        """抽取文本中的规则键，按别名表规范化，并按提升规则补充键."""
        canonical: list[str] = []
        for key in self.key_patterns.extract(text):
            mapped = self.alias_map.get(key, key)
            if mapped not in canonical:
                canonical.append(mapped)
        for to_key in self.promotion_keys(text):
            if to_key not in canonical:
                canonical.append(to_key)
        return canonical

    def promotion_keys(self, text: str) -> list[str]:
        """仅按提升规则（`rule_key_promotions` 的 when_any / when_all）补充的键.

        与 `normalize_keys` 的 promotion 段逐字同源；单独暴露给 record 路径，
        使 promotion 的扫描文本（rationale + header）可以与精确键抽取
        （只看 rationale）分开。
        """
        lowered = (text or "").lower()
        promoted: list[str] = []
        for promo in self.promotions:
            to_key = promo.get("to_key")
            if not to_key or to_key in promoted:
                continue
            when_any = promo.get("when_any")
            when_all = promo.get("when_all")
            if when_any and any(str(word).lower() in lowered for word in when_any):
                promoted.append(to_key)
            elif when_all and all(str(word).lower() in lowered for word in when_all):
                promoted.append(to_key)
        return promoted

    def with_repo(self, overlay: tuple[Rule, ...]) -> RuleSet:
        """叠加请求级仓库规则（同键覆盖），返回新的不可变 `RuleSet`.

        只换索引/`rules`/`fingerprint`；`verbatim_body` 仍是场景规则正文
        （spec §6.4：仓库原文经 prompt 的 `{{repo_rules}}` 注入，不得改写正文）。
        `overlay` 为空时原样返回 `self`（不产生新指纹，避免缓存击穿）。
        """
        if not overlay:
            return self

        index = dict(self.index_by_key)
        for rule in overlay:
            index[rule.key] = rule
        # 不变量：index 插入顺序与 assemble_base 产出的 rules 顺序一致
        # （覆盖键保持原位、新键追加末尾），故下方 index.values() 即目标顺序。
        payload = sorted(
            [rule.key, rule.severity, rule.title, rule.priority] for rule in overlay
        )
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False).encode()
        ).hexdigest()[:12]
        return replace(
            self,
            index_by_key=index,
            rules=tuple(index.values()),
            fingerprint=f"{self.fingerprint}:repo:{digest}",
        )


def _effective_rule_keys(profile: Profile) -> dict[str, Any]:
    """仓库键前缀以运行配置为唯一来源（`review.yaml` / env → `rule_key_prefix`）.

    `load_profile()` 已把 profile.yaml / review.yaml 的 `rule_keys.repo_prefix`
    映射进配置；手工构造 Profile 时配置值仍是 schema 默认 `auto`，而 auto 是
    任意前缀的超集，不会误拒规则包.
    """
    from reviewagent.config import current

    rule_keys = dict(profile.rule_keys)
    rule_keys["repo_prefix"] = current().rule_key_prefix
    return rule_keys


def assemble_base(profile: Profile) -> RuleSet:
    """装配基础 `RuleSet`（profile 规则包 -> 去重规则 + 正文 + 键模式）."""
    key_patterns = build_key_patterns(_effective_rule_keys(profile))
    pairs = [
        (order, parse_pack(profile.root / rel, language=profile.language, root=profile.root))
        for order, rel in enumerate(profile.rules)
    ]
    pairs.sort(key=lambda item: (-item[1].priority, item[0]))
    packs = [pack for _, pack in pairs]

    shared_root = (profile.root.parent / _SHARED).resolve()
    seen_ids: set[str] = set()
    for pack in packs:
        if pack.id in seen_ids:
            raise ValueError(f"duplicate rule pack id: {pack.id}")
        seen_ids.add(pack.id)
        if Path(pack.path).is_relative_to(shared_root) and not pack.applies_to.get(
            "languages"
        ):
            raise ValueError(
                f"shared rule pack {pack.id} must declare applies_to.languages"
            )
        for rule in pack.rules:
            if not (key_patterns.is_builtin(rule.key) or key_patterns.is_repo(rule.key)):
                raise ValueError(
                    f"rule key '{rule.key}' in pack {pack.id} ({pack.path}) "
                    "is outside profile namespace"
                )

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
        if key in disabled:
            # 已 disable 的键上的 override 是 no-op（不装配该键，自然无覆盖目标）。
            continue
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
        sorted(
            [
                [
                    rule.key,
                    rule.severity,
                    rule.weight,
                    rule.priority,
                    rule.source.get("pack_id"),
                ]
                for rule in rules
            ]
        ),
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
        key_patterns=key_patterns,
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
