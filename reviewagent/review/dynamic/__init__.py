"""动态规则框架（语言无关）：模型、provider 注册表与降级 runner.

模型本体在 `reviewagent/scenario/dynamic.py`（数据层），此处 re-export 供既有
import 路径使用；provider 实现由 T14 落地并按需 `register()`。
"""
from __future__ import annotations

from reviewagent.review.dynamic.model import DynamicEvidence, DynamicRule, DynamicRuleSet
from reviewagent.review.dynamic.registry import get, register, registered
from reviewagent.review.dynamic.runner import run_dynamic
from reviewagent.scenario.dynamic import load_dynamic_rules

__all__ = [
    "DynamicEvidence",
    "DynamicRule",
    "DynamicRuleSet",
    "get",
    "load_dynamic_rules",
    "register",
    "registered",
    "run_dynamic",
]
