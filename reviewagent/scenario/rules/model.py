"""规则层数据模型（spec §6.2/§6.3）：不可变 `Rule` / `RulePack`.

规则包是数据，解析/装配/渲染是纯逻辑；本模块不依赖 `reviewagent.review`，
也不读环境变量。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Rule:
    key: str
    title: str
    body: str = ""
    severity: str = "medium"
    weight: float = 1.0
    applies_to: dict[str, Any] = field(default_factory=dict)
    examples: tuple[dict[str, str], ...] = ()
    actions: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    detect_hints: tuple[str, ...] = ()
    source: dict[str, Any] = field(default_factory=dict)
    priority: int = 800


@dataclass(frozen=True)
class RulePack:
    id: str
    title: str
    priority: int
    render: str
    body: str
    rules: tuple[Rule, ...]
    index_by_key: dict[str, Rule]
    path: str
    fingerprint: str
    applies_to: dict[str, Any] = field(default_factory=dict)
