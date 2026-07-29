"""Notifier protocol — 抽象投递层, 让钉钉/飞书/Slack 等 IM 都能 plug in.

参考 pr-agent: notifier 消费 renderer 切好的 markdown chunks, 负责投递 + retry
+ 记录 delivery failure 让 scheduler audit.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class DeliveryResult:
    """单次 send() 结果."""
    success: bool
    chunks_sent: int = 0
    chunks_total: int = 0
    error: str | None = None
    meta: dict[str, object] = field(default_factory=dict)


@runtime_checkable
class Notifier(Protocol):
    """notifier 协议."""
    name: str

    def send(self, title: str, markdown_chunks: list[str]) -> DeliveryResult: ...


__all__ = ["DeliveryResult", "Notifier"]
