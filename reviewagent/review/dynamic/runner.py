"""动态规则执行骨架 — 逐规则采集证据，超时 / 异常一律降级.

语义（spec §6.7.5）：动态证据是"锦上添花"，任何失败都不得阻塞 MR，也不得让
prompt 缺段——失败时返回空证据 + `degraded=True`，由 context 渲染空模板。

* 每个启用规则在独立 daemon 线程中执行，`join(timeout)` 到期即降级为
  `reason="timeout"` 并记 WARNING（线程无法强杀，残留线程随后自行结束）；
* 超时优先级：`rule.params["timeout_seconds"]` > 调用方传入的 `timeout_s`；
* provider 未注册（`KeyError`）或采集抛异常 → `reason=type(exc).__name__`；
* 禁用规则不进入结果字典（消费方据此完全不渲染该规则）。
"""
from __future__ import annotations

import threading
from typing import Any

from reviewagent.logging_setup import logger
from reviewagent.review.dynamic.model import DynamicEvidence, DynamicRule, DynamicRuleSet
from reviewagent.review.dynamic.registry import get as get_provider


def _degraded(rule: DynamicRule, reason: str) -> DynamicEvidence:
    return DynamicEvidence(
        rule_key=rule.key,
        provider=rule.provider,
        refs=(),
        degraded=True,
        reason=reason,
    )


def _collect(
    rule: DynamicRule,
    *,
    files: Any,
    diff_by_file: Any,
    worktree: Any,
    fetch_callback: Any,
) -> tuple[dict[str, Any], ...]:
    provider = get_provider(rule.provider, fetch_callback=fetch_callback)
    refs = provider.fetch(
        files=files,
        diff_by_file=diff_by_file,
        worktree=worktree,
        params=rule.params,
    )
    return tuple(refs)


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

        thread = threading.Thread(target=_target, daemon=True, name=f"dynamic-{rule.key}")
        thread.start()
        timeout = float(rule.params.get("timeout_seconds", timeout_s))
        thread.join(timeout)
        if thread.is_alive():
            logger.warning("dynamic.timeout rule={} provider={}", rule.key, rule.provider)
            out[rule.key] = _degraded(rule, "timeout")
        elif "error" in box:
            reason = type(box["error"]).__name__
            logger.warning(
                "dynamic.degrade rule={} provider={} reason={}",
                rule.key,
                rule.provider,
                reason,
            )
            out[rule.key] = _degraded(rule, reason)
        else:
            out[rule.key] = DynamicEvidence(
                rule_key=rule.key,
                provider=rule.provider,
                refs=box["refs"],
            )
    return out
