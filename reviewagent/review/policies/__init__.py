"""语言策略实现（LanguagePolicy 的具体语言版本）.

* `python.PythonPolicy`：默认场景 `ssd-python-autotest` 的实现，逻辑与旧
  ImproveCommand 成员逐字一致；
* `generic.GenericPolicy`：未注册语言的保守兜底（C/C++ 首期走这里）；
* `registry`：`language` → 工厂的注册表，引擎只依赖它 + `LanguagePolicy` 协议.

本子包只向 `review.policy` / `review.types` 单向依赖，不 import `review` 的
引擎模块（planner / context / validation 等）.
"""
from __future__ import annotations

from reviewagent.review.policies.registry import get, register

__all__ = ["get", "register"]
