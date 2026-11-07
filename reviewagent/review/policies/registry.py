"""语言策略注册表 — `language` 字符串 → LanguagePolicy 工厂.

* `register(language, factory)`：第三方 / 后续任务（C++ 场景包等）注册自己的
  策略工厂；工厂签名统一为 `(*, profile, cfg, read_lines) -> LanguagePolicy`；
* `get(language, *, profile, cfg, read_lines)`：显式注册优先，未注册时回退内建
  策略（python → PythonPolicy，其余 → GenericPolicy）；
* 内建策略用 importlib 惰性导入，注册表导入本身不拉起任何语言实现.
"""
from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any

from reviewagent.review.policy import LanguagePolicy

_FACTORIES: dict[str, Callable[..., LanguagePolicy]] = {}


def register(language: str, factory: Callable[..., LanguagePolicy]) -> None:
    """注册（或覆盖）某语言的策略工厂."""
    _FACTORIES[language] = factory


def _builtin(language: str) -> Callable[..., LanguagePolicy]:
    if language == "python":
        module = importlib.import_module("reviewagent.review.policies.python")
        return module.PythonPolicy
    module = importlib.import_module("reviewagent.review.policies.generic")
    return module.GenericPolicy


def get(language: str, **kwargs: Any) -> LanguagePolicy:
    """按语言取得策略实例；kwargs 透传给工厂（profile / cfg / read_lines）."""
    factory = _FACTORIES.get(language) or _builtin(language)
    return factory(**kwargs)
