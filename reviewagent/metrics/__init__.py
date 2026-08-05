"""轻量级 metrics — 单进程内存计数器，无 prometheus 依赖.

设计动机:
- 不引入 prometheus_client / psutil 等重依赖
- 单 Counter 实例足以覆盖 webhook / lock / provider / LLM 调用计数
- 不需要持久化，只需要快速查询当次进程活动
- HTTP /metrics 端点: prometheus text format 兼容（部分：仅 counter/gauge）
"""

from reviewagent.metrics.counters import (
    Metrics,
    metrics,
    inc,
    gauge,
    snapshot,
    format_prometheus,
)

__all__ = [
    "Metrics",
    "metrics",
    "inc",
    "gauge",
    "snapshot",
    "format_prometheus",
]
