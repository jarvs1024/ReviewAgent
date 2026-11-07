"""运行时 manifest（可复现身份）与 RQ job 一致性校验.

manifest 把一次运行的"身份"收敛成平面字典：

* `service_version` — 代码版本（`reviewagent.__version__`）；
* `profile` / `profile_fingerprint` — 场景包名与内容指纹；
* `ruleset_fingerprint` — 生效规则集指纹（请求级仓库规则优先）；
* `dynamic_rules_fingerprint` — 动态规则内容摘要（未声明时为空串）；
* `config_fingerprint` — 生效配置摘要（`settings.binder.fingerprint`，secret 恒打码）；
* `model` / `provider` — 本轮实际模型与 LLM provider。

`verify_job()` 在 worker 侧比对这些字段，避免"入队时 A 场景、执行时 B 场景"
的静默错配。滚动升级兼容：老 job payload 没有指纹字段时只记 WARNING 并放行，
不阻塞已排队任务。

分层边界：本模块只依赖 `settings` / `logging`（以及惰性 import 的
`scenario`），**不得** import `reviewagent.commands.*` 或 `reviewagent.main`。
"""
from __future__ import annotations

from typing import Any

from reviewagent import __version__
from reviewagent.logging_setup import logger
from reviewagent.settings.binder import fingerprint as config_fingerprint
from reviewagent.settings.schema import SCHEMA

__all__ = ["build_manifest", "manifest_for_active", "verify_job"]

# 老 job payload 必须带的三项；缺任一 → 视为升级前入队，放行.
_REQUIRED_JOB_FIELDS = ("profile", "profile_fingerprint", "config_fingerprint")


def _config_fingerprint(cfg: Any) -> str:
    """按 SCHEMA 全量取 cfg 字段计算配置指纹（secret 由 binder 打码）."""
    return config_fingerprint({spec.name: getattr(cfg, spec.name) for spec in SCHEMA})


def build_manifest(
    *,
    profile: Any,
    rule_set: Any,
    cfg: Any,
    model: str,
    provider: str,
    request_ruleset: Any = None,
    dynamic_rules: Any = None,
) -> dict[str, Any]:
    """构建一次运行的 manifest 字典.

    `request_ruleset`（请求级仓库规则叠加后的 RuleSet）优先于基础
    `rule_set`，让入队侧与执行侧看到同一份生效规则指纹。
    `dynamic_rules` 为 None（profile 未声明）时动态指纹为空串。
    """
    effective_ruleset = request_ruleset if request_ruleset is not None else rule_set
    return {
        "service_version": __version__,
        "profile": profile.name,
        "profile_fingerprint": profile.fingerprint,
        "ruleset_fingerprint": effective_ruleset.fingerprint,
        "dynamic_rules_fingerprint": (
            dynamic_rules.fingerprint if dynamic_rules is not None else ""
        ),
        "config_fingerprint": _config_fingerprint(cfg),
        "model": model,
        "provider": provider,
    }


def manifest_for_active(
    *,
    cfg: Any,
    model: str,
    provider: str,
    request_ruleset: Any = None,
) -> dict[str, Any]:
    """按当前激活的 profile / RuleSet / 动态规则构建 manifest（入队侧便捷入口）."""
    from reviewagent.scenario.dynamic import load_dynamic_rules
    from reviewagent.scenario.profile import active_or_load
    from reviewagent.scenario.rules.resolver import active_rule_set

    profile = active_or_load()
    return build_manifest(
        profile=profile,
        rule_set=active_rule_set(),
        cfg=cfg,
        model=model,
        provider=provider,
        request_ruleset=request_ruleset,
        dynamic_rules=load_dynamic_rules(profile),
    )


def _mismatch(kind: str, payload_field: str, job_value: Any, active_value: Any) -> RuntimeError:
    return RuntimeError(
        f"{kind}: {payload_field} fingerprint mismatch "
        f"job={job_value!r} active={active_value!r}"
    )


def verify_job(payload: dict[str, Any]) -> None:
    """校验 job payload 与当前运行环境一致；不一致抛 `RuntimeError`.

    * 缺少 `profile` / `profile_fingerprint` / `config_fingerprint` → 老 job，
      记 `job.manifest_missing` 并放行（滚动升级期间排队任务仍可执行）；
    * profile 名 / profile 指纹 / config 指纹不一致 → `job.profile_mismatch` /
      `job.config_mismatch`（消息含 "fingerprint"）；
    * payload 带 `ruleset_fingerprint` 且与激活规则集不一致 → `job.ruleset_mismatch`.
    """
    missing = [key for key in _REQUIRED_JOB_FIELDS if not payload.get(key)]
    if missing:
        logger.warning("job.manifest_missing keys={}", missing)
        return

    from reviewagent.config import current
    from reviewagent.scenario.profile import active_or_load
    from reviewagent.scenario.rules.resolver import active_rule_set

    profile = active_or_load()
    if payload["profile"] != profile.name:
        raise RuntimeError(
            f"job.profile_mismatch: job profile {payload['profile']!r} "
            f"!= active profile {profile.name!r}"
        )
    if payload["profile_fingerprint"] != profile.fingerprint:
        raise _mismatch(
            "job.profile_mismatch", "profile",
            payload["profile_fingerprint"], profile.fingerprint,
        )

    active_config_fingerprint = _config_fingerprint(current())
    if payload["config_fingerprint"] != active_config_fingerprint:
        raise _mismatch(
            "job.config_mismatch", "config",
            payload["config_fingerprint"], active_config_fingerprint,
        )

    job_ruleset_fingerprint = payload.get("ruleset_fingerprint")
    if job_ruleset_fingerprint:
        active_ruleset_fingerprint = active_rule_set().fingerprint
        if job_ruleset_fingerprint != active_ruleset_fingerprint:
            raise _mismatch(
                "job.ruleset_mismatch", "ruleset",
                job_ruleset_fingerprint, active_ruleset_fingerprint,
            )
