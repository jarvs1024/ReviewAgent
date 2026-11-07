"""动态规则执行骨架 — 逐规则采集证据，超时 / 异常一律降级.

语义（spec §6.7.5）：动态证据是"锦上添花"，任何失败都不得阻塞 MR，也不得让
prompt 缺段——失败时返回空证据 + `degraded=True`，由 context 渲染空模板。

* 每个启用规则在独立 daemon 线程中执行，`join(timeout)` 到期即降级为
  `reason="timeout"` 并记 WARNING（线程无法强杀，残留线程随后自行结束）；
* 超时优先级：`rule.params["timeout_seconds"]` > 调用方传入的 `timeout_s`；非法值
  （非数值 / `nan` / `inf` / `<= 0`）回退 `timeout_s`，且解析发生在起线程之前，
  异常不得外逃（`thread.join(nan)` 会直接抛 `ValueError`）；
* provider 未注册（`KeyError`）或采集抛异常 → `reason=type(exc).__name__`；
* 禁用规则不进入结果字典（消费方据此完全不渲染该规则）。
"""
from __future__ import annotations

import math
import threading
from typing import Any

from reviewagent.logging_setup import logger
from reviewagent.review.dynamic.model import DynamicEvidence, DynamicRule, DynamicRuleSet
from reviewagent.review.dynamic.registry import get as get_provider

# 连调用方 `timeout_s` 都不可转换时的最终兜底（配置彻底损坏也不无限等待）。
_FALLBACK_TIMEOUT_S = 60.0


def _degraded(rule: DynamicRule, reason: str) -> DynamicEvidence:
    return DynamicEvidence(
        rule_key=rule.key,
        provider=rule.provider,
        refs=(),
        degraded=True,
        reason=reason,
    )


def _coerce_timeout(raw: Any) -> float | None:
    """把任意值归一为有限且为正的 float 秒；非法时返回 None（不抛异常）.

    非数值之外还必须拒绝 `nan` / `inf` / `<= 0` —— `thread.join(nan)` 会抛
    `ValueError`，负值 / 零值则让超时语义失效（立即或永不判定超时）。
    """
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return value


def _resolve_timeout(rule: DynamicRule, timeout_s: float) -> float:
    """在起线程**之前**解析超时：规则参数优先，非法则回退调用方 `timeout_s`.

    非法配置（`"abc"` / `null` / dict 等）不得让异常逃出 `run_dynamic` —— 那会
    留下一个仍在运行的 daemon 线程并阻塞 MR；这里一律回退到最后可用的秒数。
    """
    timeout = _coerce_timeout(rule.params.get("timeout_seconds", timeout_s))
    if timeout is None:
        timeout = _coerce_timeout(timeout_s)
    if timeout is None:
        timeout = _FALLBACK_TIMEOUT_S
    return timeout


def _collect(
    rule: DynamicRule,
    *,
    files: Any,
    diff_by_file: Any,
    worktree: Any,
    fetch_callback: Any,
) -> tuple[dict[str, Any], ...]:
    provider = get_provider(rule.provider, fetch_callback=fetch_callback)
    # 复制一份 params 并带上规则键，仅供 provider 侧非法参数告警归属（不参与采集）。
    params = dict(rule.params or {})
    params.setdefault("rule_key", rule.key)
    refs = provider.fetch(
        files=files,
        diff_by_file=diff_by_file,
        worktree=worktree,
        params=params,
    )
    return tuple(refs)


def refs_by_file(
    evidence: dict[str, DynamicEvidence], files: Any,
) -> dict[str, list[dict]]:
    """把扁平动态证据按 `review_file` 还原为 ``{file: [ref, ...]}``.

    provider 在每条 ref 上追加了内部键 `review_file`（被检视文件）；此处剥离该
    键并分桶，使渲染器输入与迁移前（legacy 门面返回值）逐字节一致。降级证据与
    指向非本次检视文件的 ref 一律丢弃。
    """
    out: dict[str, list[dict]] = {fp: [] for fp in files}
    for ev in evidence.values():
        if ev.degraded:
            continue
        for ref in ev.refs:
            item = dict(ref)
            review_file = item.pop("review_file", None)
            if review_file in out:
                out[review_file].append(item)
    return out


def run_dynamic(
    ruleset: DynamicRuleSet,
    *,
    files: Any,
    diff_by_file: Any,
    worktree: Any,
    timeout_s: float,
    fetch_callback: Any = None,
) -> dict[str, DynamicEvidence]:
    """按启用规则采集证据；返回 `rule_key -> DynamicEvidence`（禁用的键缺席）."""
    out: dict[str, DynamicEvidence] = {}
    for rule in ruleset.rules:
        if not rule.enabled:
            continue
        box: dict[str, Any] = {}

        def _target(_rule: DynamicRule = rule, _box: dict[str, Any] = box) -> None:
            try:
                _box["refs"] = _collect(
                    _rule,
                    files=files,
                    diff_by_file=diff_by_file,
                    worktree=worktree,
                    fetch_callback=fetch_callback,
                )
            except Exception as exc:  # 降级：provider 异常不得外逃
                _box["error"] = exc

        timeout = _resolve_timeout(rule, timeout_s)
        thread = threading.Thread(target=_target, daemon=True, name=f"dynamic-{rule.key}")
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            logger.warning("dynamic.timeout rule={} provider={}", rule.key, rule.provider)
            out[rule.key] = _degraded(rule, "timeout")
        elif "error" in box:
            # 降级告警统一由调用方（review/runner.py）按 `dynamic.degraded` 输出，
            # 这里不再重复记日志（T13 自加的 `dynamic.degrade` 已移除）。
            out[rule.key] = _degraded(rule, type(box["error"]).__name__)
        else:
            out[rule.key] = DynamicEvidence(
                rule_key=rule.key,
                provider=rule.provider,
                refs=box["refs"],
            )
    return out
