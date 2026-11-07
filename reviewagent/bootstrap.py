"""启动装配（Task 3）：激活场景 profile、绑定 data_dir、记录启动日志、readiness 骨架.

API（`reviewagent.main` lifespan）与 worker（`ReviewAgentSpawnWorker.bootstrap`）
共用同一入口，启动顺序固定：

1. `setup_logging()`（幂等）；
2. `load_and_activate()`：按 `REVIEW_PROFILE` 加载 profile，把场景字段
   `apply_scenario()` 到运行配置；
3. `bind_instance()`：把 `{data_dir}/instance.json` 绑定到该 profile，
   同一 data_dir 换 profile 直接失败（跨场景数据隔离）；
4. 装配基础 RuleSet（`assemble_base(profile)`）并设为激活规则集；
   `BootstrapResult.rule_set` 携带该快照，`bootstrap.ready` 记录其指纹；
5. 注册动态 provider 工厂、加载动态规则（模板缺失/越界即启动失败），并对
   **每条**规则（含 disabled）校验 provider 已注册，未知 provider 直接
   `ValueError`（运行期不静默降级）；
6. 一条 `config.resolved` + 一条 `bootstrap.ready` 日志（含 profile /
   config / 规则 / 动态规则指纹）；显式出现的场景 env 追加一条
   `config.compat_env`。

`readiness()` 返回 `(ok, reasons)`：只校验 profile 已激活、data_dir 已绑定、
config 可解析、RuleSet 已装配且非空（不连网、不建表）。`runtime_readiness()`
在其之后追加 SQLite / Redis 探针，供 `/readyz` 使用（探针失败只返回原因，不抛错）。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from reviewagent.config import current
from reviewagent.logging_setup import logger, setup_logging
from reviewagent.scenario import profile as profile_module
from reviewagent.scenario.profile import bind_instance, bound_profile, load_and_activate
from reviewagent.scenario.rules.resolver import RuleSet, assemble_base, set_active_rule_set
from reviewagent.settings.binder import REQUIRED_FIELDS, explicit_env, validate
from reviewagent.settings.binder import fingerprint as config_fingerprint
from reviewagent.settings.schema import SCHEMA

# 场景平面（可被 profile 覆盖）的 env 名集合，用于提示"env 与 profile 同时设置"。
_SCENARIO_ENV_KEYS = frozenset(
    spec.env for spec in SCHEMA if spec.scope == "scenario" and spec.env
)


@dataclass(frozen=True)
class BootstrapResult:
    """一次成功启动的结果快照.

    前两个字段与 brief 保持一致（位置参数），其余为扩展信息；
    `data_dir` 不设默认值（静默取当前目录会把日志/数据写到意外位置），
    `rule_set` 是启动期装配的规则快照。
    """

    profile_name: str
    profile_fingerprint: str
    data_dir: Path
    kind: str = ""
    config_fingerprint: str = ""
    rule_set: RuleSet | None = None
    dynamic_rules: Any = None
    dynamic_rules_fingerprint: str = ""


# ---------------- readiness ----------------


def _check_config() -> list[str]:
    """config 可解析：SCHEMA 全量类型/取值校验 + 必填项非空."""
    cfg = current()
    reasons: list[str] = []
    for spec in SCHEMA:
        try:
            validate(spec, getattr(cfg, spec.name))
        except ValueError as exc:
            reasons.append(f"config invalid: {exc}")
    for name in REQUIRED_FIELDS:
        if not getattr(cfg, name):
            reasons.append(f"missing required config: {name}")
    return reasons


def _check_profile_active() -> list[str]:
    try:
        profile_module.active()
    except RuntimeError as exc:
        return [str(exc)]
    return []


def _check_instance_bound() -> list[str]:
    """data_dir 已绑定且绑定对象就是当前激活的 profile."""
    data_dir = Path(current().data_dir)
    try:
        bound = bound_profile(data_dir)
    except RuntimeError as exc:  # instance.json 损坏 / schema 版本不符
        return [str(exc)]
    if bound is None:
        return [f"data_dir not bound: {data_dir / 'instance.json'} missing"]
    try:
        active_name = profile_module.active().name
    except RuntimeError:
        return [f"data_dir {data_dir} bound to {bound!r} but no profile is active"]
    if bound != active_name:
        return [f"data_dir {data_dir} bound to {bound!r}, active profile is {active_name!r}"]
    return []


def _check_rule_set_active() -> list[str]:
    """启动期 RuleSet 已装配且非空（空规则集意味着规则包引用失效）."""
    try:
        from reviewagent.scenario.rules.resolver import active_rule_set

        rule_set = active_rule_set()
    except Exception as exc:  # noqa: BLE001 — 探针故障也是"未就绪"的原因
        return [f"rule set: {type(exc).__name__}: {exc}"]
    if not rule_set.rules:
        return ["rule set: no rules assembled"]
    return []


_DEFAULT_CHECKS: tuple[Callable[[], list[str]], ...] = (
    _check_config,
    _check_profile_active,
    _check_instance_bound,
    _check_rule_set_active,
)

# 可替换钩子：Task 15 追加 SQLite / Redis 探针。每个 check 返回原因列表（空=通过）。
_readiness_checks: list[Callable[[], list[str]]] = list(_DEFAULT_CHECKS)


def register_readiness_check(check: Callable[[], list[str]]) -> None:
    """追加一个 readiness 钩子（返回原因列表；空列表表示通过）."""
    _readiness_checks.append(check)


def readiness() -> tuple[bool, list[str]]:
    """`(ok, reasons)`；reasons 为空即就绪。单个钩子抛错降级成一条原因，不中断其余钩子."""
    reasons: list[str] = []
    for check in list(_readiness_checks):
        try:
            reasons.extend(check())
        except Exception as exc:  # noqa: BLE001 — 探针自身故障也是"未就绪"的原因
            reasons.append(f"{getattr(check, '__name__', 'check')} raised {type(exc).__name__}: {exc}")
    return (not reasons, reasons)


# ---------------- runtime readiness (SQLite / Redis) ----------------


def _check_sqlite() -> list[str]:
    """SQLite 探针：取 store 并在真实连接上执行 `SELECT 1`（`Store.ping`）."""
    try:
        from reviewagent.telemetry.store import get_store

        get_store().ping()
    except Exception as exc:  # noqa: BLE001 — 探针失败即原因
        return [f"sqlite: {type(exc).__name__}: {exc}"]
    return []


def _check_redis() -> list[str]:
    """Redis 探针：新建短超时连接 ping，绝不触碰 `get_redis()` 共享单例.

    专用 client 用完必须关闭（`try/finally`），否则每次 `/readyz` 都泄漏一个连接。
    """
    client: Any = None
    try:
        import redis

        client = redis.Redis.from_url(
            current().redis_url,
            socket_connect_timeout=1,
            socket_timeout=1,
        )
        client.ping()
    except Exception as exc:  # noqa: BLE001 — 探针失败即原因
        return [f"redis: {type(exc).__name__}: {exc}"]
    finally:
        close = getattr(client, "close", None) if client is not None else None
        if callable(close):
            try:
                close()
            except Exception as exc:  # noqa: BLE001 — 关闭失败不影响探针结论
                logger.warning("readiness.redis close failed: {}", exc)
    return []


def runtime_readiness() -> tuple[bool, list[str]]:
    """`readiness()` + SQLite / Redis 探针；任一失败即未就绪（附原因）."""
    ok, reasons = readiness()
    reasons = list(reasons)
    reasons.extend(_check_sqlite())
    reasons.extend(_check_redis())
    return (not reasons, reasons)


# ---------------- startup ----------------

_state: BootstrapResult | None = None


def startup(kind: str) -> BootstrapResult:
    """API / worker 的统一启动装配；同一 kind 重复调用直接返回首次结果（幂等）.

    状态在首次成功启动后按 `kind` 冻结（换 kind 会重新装配）；测试需要多次
    重跑启动路径时先调用 `reset_for_tests()`，否则拿到的是缓存结果。
    """
    global _state
    if _state is not None and _state.kind == kind:
        return _state
    setup_logging()
    profile = load_and_activate()
    data_dir = Path(current().data_dir)
    bind_instance(profile, data_dir)
    rule_set = assemble_base(profile)
    set_active_rule_set(rule_set)

    # 动态规则：先注册 provider 工厂（导入即注册），再加载规则（模板缺失 /
    # 越界即失败），最后对每条规则（含 disabled）校验 provider 已注册 ——
    # 配置错误必须启动即暴露，不在运行期静默降级。
    import reviewagent.review.dynamic.providers  # noqa: F401  注册工厂
    from reviewagent.review.dynamic.registry import get as get_provider
    from reviewagent.scenario.dynamic import load_dynamic_rules

    dynamic_rules = load_dynamic_rules(profile)
    for rule in dynamic_rules.rules:
        try:
            get_provider(rule.provider, fetch_callback=None)
        except KeyError as exc:
            raise ValueError(f"unknown dynamic provider: {rule.provider}") from exc

    values = {spec.name: getattr(current(), spec.name) for spec in SCHEMA}
    result = BootstrapResult(
        profile.name,
        profile.fingerprint,
        kind=kind,
        config_fingerprint=config_fingerprint(values),
        data_dir=data_dir,
        rule_set=rule_set,
        dynamic_rules=dynamic_rules,
        dynamic_rules_fingerprint=dynamic_rules.fingerprint,
    )
    logger.info(
        "config.resolved kind={} profile={} profile_fingerprint={} config_fingerprint={}",
        kind,
        profile.name,
        profile.fingerprint,
        result.config_fingerprint,
    )
    compat = sorted(key for key in explicit_env() if key in _SCENARIO_ENV_KEYS)
    if compat:
        logger.info("config.compat_env keys={}", compat)
    logger.info(
        "bootstrap.ready kind={} profile={} profile_fingerprint={} config_fingerprint={} "
        "rule_fingerprint={} dynamic_fingerprint={} data_dir={}",
        kind,
        profile.name,
        profile.fingerprint,
        result.config_fingerprint,
        rule_set.fingerprint,
        dynamic_rules.fingerprint,
        data_dir,
    )
    _state = result
    return result


def reset_for_tests() -> None:
    """清空启动缓存 / 激活状态 / RuleSet / 追加的 readiness 钩子（仅测试使用）."""
    global _state
    _state = None
    # `deactivate()` 内部已失效激活 RuleSet（避免新 profile 看到旧规则）。
    profile_module.deactivate()
    _readiness_checks[:] = list(_DEFAULT_CHECKS)
