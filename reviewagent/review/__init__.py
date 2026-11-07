"""语言抽象层（review）— 语言无关的共享类型与策略协议.

架构边界（见 docs/superpowers/specs/2026-11-07-multi-language-review-design.md）：

* 本包只放语言无关的抽象与引擎（planner / context / assembly / runner /
  validation / dynamic 由后续任务补齐）；
* 本包**不得** import `reviewagent.review.policies.*`（策略实现由 registry
  惰性装配），避免语言实现反向绑定引擎；
* 语言差异全部经 :class:`reviewagent.review.policy.LanguagePolicy` 钩子表达.

`reviewagent.review.policies` 是策略实现所在的子包，两者方向单向：
`review.policies.*` -> `review.policy` / `review.types`.
"""
from __future__ import annotations

from reviewagent.review.policy import LanguagePolicy
from reviewagent.review.types import (
    FileContext,
    FileKind,
    RiskVerdict,
    Suggestion,
)

__all__ = [
    "FileContext",
    "FileKind",
    "LanguagePolicy",
    "RiskVerdict",
    "Suggestion",
]
