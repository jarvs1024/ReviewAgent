"""In-memory counters + gauges + prometheus text format 输出."""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import Any


# metric 默认 type: counter (更常见). register_help 时显式标注 "counter"/"gauge".
_DEFAULT_TYPE = "counter"


class Metrics:
    """单进程内的 Counter + Gauge，thread-safe.

    设计:
        - 用 inc(name, **labels) / gauge_set(name, value, **labels) 写入
        - 用 register_help(name, kind="counter"|"gauge", help_text=...) 注册说明
        - format_prometheus 输出标准 prometheus text format, 每条 metric 只出现一次
        - 同名 metric 不可同时存在 counter 和 gauge, 否则后者会覆盖前者
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # counter: {name: {labels_tuple: value}}
        self._counters: dict[str, dict[tuple, float]] = defaultdict(lambda: defaultdict(float))
        # gauge: {name: {labels_tuple: value}}
        self._gauges: dict[str, dict[tuple, float]] = defaultdict(lambda: defaultdict(float))
        # 注释: name → (kind, help_text)
        self._registry: dict[str, tuple[str, str | None]] = {}

    def register_help(
        self,
        name: str,
        help_text: str | None = None,
        *,
        kind: str = _DEFAULT_TYPE,
    ) -> None:
        """注册 metric 的 kind + HELP 文本. 既能纯说明 0 计数 metric, 又不会跨段重复."""
        if kind not in ("counter", "gauge"):
            raise ValueError(f"kind 必须为 'counter' or 'gauge', got {kind!r}")
        with self._lock:
            existing_kind, existing_help = self._registry.get(name, (kind, None))
            # 同 metric 多次 register_help: kind 取最早的, help 取最后一次非空
            final_help = help_text if help_text is not None else existing_help
            self._registry[name] = (existing_kind, final_help)

    def _register_kind_on_inc(self, name: str, kind: str) -> None:
        """inc / gauge_set 时自动登记 kind, 供 format_prometheus 用."""
        if kind not in ("counter", "gauge"):
            return
        with self._lock:
            existing_kind, help_text = self._registry.get(name, (None, None))
            if existing_kind is None:
                # 首次写入: 注册 kind, help 保持 None
                self._registry[name] = (kind, help_text)
            elif existing_kind != kind:
                # 同 metric 不同 store 操作: 强行覆盖 (warning-prone)
                # 实际不可能 — 代码里每个 metric 只会作为 counter 或 gauge.
                pass

    def inc(self, name: str, amount: float = 1.0, **labels: Any) -> None:
        """Counter 自增. labels 作为 keyword arg 传入."""
        key = tuple(sorted(labels.items()))
        with self._lock:
            self._counters[name][key] += amount
            self._registry.setdefault(name, ("counter", None))

    def gauge_set(self, name: str, value: float, **labels: Any) -> None:
        """Gauge 设值."""
        key = tuple(sorted(labels.items()))
        with self._lock:
            self._gauges[name][key] = value
            self._registry.setdefault(name, ("gauge", None))

    def gauge_inc(self, name: str, amount: float = 1.0, **labels: Any) -> None:
        """Gauge 自增."""
        key = tuple(sorted(labels.items()))
        with self._lock:
            self._gauges[name][key] += amount
            self._registry.setdefault(name, ("gauge", None))

    def snapshot(self) -> dict[str, dict[tuple, float]]:
        """返回 (counter, gauge) 的当前快照."""
        with self._lock:
            return {
                "counters": {k: dict(v) for k, v in self._counters.items()},
                "gauges": {k: dict(v) for k, v in self._gauges.items()},
            }

    def format_prometheus(self) -> str:
        """输出 prometheus text exposition format (subset)."""
        with self._lock:
            # 收集所有需要输出的 metric name (按 type 分组)
            counter_names = sorted(set(self._counters) | {
                n for n, (k, _) in self._registry.items() if k == "counter"
            })
            gauge_names = sorted(set(self._gauges) | {
                n for n, (k, _) in self._registry.items() if k == "gauge"
            })

            out: list[str] = []
            for name in counter_names:
                self._render(out, name, "counter", self._counters.get(name, {}))
            for name in gauge_names:
                self._render(out, name, "gauge", self._gauges.get(name, {}))
            return "\n".join(out) + "\n"

    def _render(self, out: list[str], name: str, kind: str, labels_map: dict) -> None:
        """Render 单条 metric 到 out."""
        help_text = self._registry.get(name, (kind, None))[1]
        if help_text:
            for line in help_text.splitlines():
                out.append(f"# HELP {name} {line}")
        out.append(f"# TYPE {name} {kind}")
        if not labels_map:
            out.append(f"{name} 0")
            return
        for label_tuple, value in sorted(labels_map.items()):
            label_str = ",".join(
                f'{k}="{_escape(v)}"' for k, v in label_tuple
            )
            out.append(f"{name}{{{label_str}}} {value}")


def _escape(value: Any) -> str:
    s = str(value)
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


# 全局单例
metrics = Metrics()


# 短名 (forward compat: inc(name, **labels))
def inc(name: str, **labels: Any) -> None:
    metrics.inc(name, **labels)


def gauge(name: str, value: float, **labels: Any) -> None:
    metrics.gauge_set(name, value, **labels)


def snapshot() -> dict[str, dict[tuple, float]]:
    return metrics.snapshot()


def format_prometheus() -> str:
    return metrics.format_prometheus()


__all__ = ["Metrics", "metrics", "inc", "gauge", "snapshot", "format_prometheus"]
