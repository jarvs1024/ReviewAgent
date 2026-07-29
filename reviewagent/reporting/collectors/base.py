"""Collector protocol + result types — 周报数据采集层 (参考 pr-agent 设计).

每节 section 拉一段数据, 返回 SectionResult; scheduler 负责失败隔离
(collectors 应该 raise 而不是 swallow).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, Protocol, runtime_checkable


@dataclass
class CollectorContext:
    """周报 collector 共享上下文."""
    target_project_id: int
    data_dir: str
    timezone: str
    target_branch: str = ""


@dataclass
class SectionResult:
    """周报单个 section 结果.

    Attributes:
        status: 'ok' / 'failed'
        data: 结构化 payload, renderer 用来格式化
        markdown: 预渲染 markdown (可选, renderer 优先用)
        error: status='failed' 时的简短原因
        meta: 自由 metadata (timing, token, count 等)
    """
    status: str = "ok"
    data: Optional[dict[str, Any]] = None
    markdown: Optional[str] = None
    error: Optional[str] = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "data": self.data,
            "markdown": self.markdown,
            "error": self.error,
            "meta": self.meta,
        }


@runtime_checkable
class Collector(Protocol):
    """collector 协议 — 每个 collector 必须实现 .name 和 .collect()"""
    name: str

    def collect(
        self,
        *,
        week_start: datetime,
        week_end: datetime,
        ctx: CollectorContext,
    ) -> SectionResult: ...


__all__ = ["Collector", "CollectorContext", "SectionResult"]
