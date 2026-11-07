"""动态证据 provider 注册表 — provider id → 工厂.

与 `review/policies/registry.py` 同模式：

* `register(name, factory)`：新增或覆盖注册（入口启动期装配，后写生效）；
* `get(name, **kwargs)`：按 id 取 provider 实例，kwargs 透传给工厂（runner 传
  `fetch_callback`）；未知 id 抛 `KeyError("unknown dynamic provider: <name>")`，
  由 runner 捕获并降级，绝不阻塞 MR；
* 工厂可以是类，也可以是任意返回带 `fetch(...)` 对象 的 callable。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

_FACTORIES: dict[str, Callable[..., Any]] = {}


def register(name: str, factory: Callable[..., Any]) -> None:
    """注册（或覆盖）某 provider id 的工厂."""
    _FACTORIES[str(name)] = factory


def get(name: str, **kwargs: Any) -> Any:
    """按 id 取得 provider 实例；kwargs 透传给工厂."""
    try:
        factory = _FACTORIES[str(name)]
    except KeyError:
        raise KeyError(f"unknown dynamic provider: {name}") from None
    return factory(**kwargs)


def registered() -> tuple[str, ...]:
    """已注册的 provider id（启动期自检 / 测试用）."""
    return tuple(sorted(_FACTORIES))
