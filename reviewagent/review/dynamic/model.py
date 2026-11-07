"""动态规则模型（数据层归属 `scenario.dynamic`，此处仅 re-export）.

设计边界：`scenario` 不得 import `review`，因此模型定义在
`reviewagent/scenario/dynamic.py`；本模块保持既有 import 路径
（`reviewagent.review.dynamic.model`）不变。
"""
from __future__ import annotations

from reviewagent.scenario.dynamic import (
    DynamicEvidence,
    DynamicRule,
    DynamicRuleSet,
)

__all__ = ["DynamicEvidence", "DynamicRule", "DynamicRuleSet"]
