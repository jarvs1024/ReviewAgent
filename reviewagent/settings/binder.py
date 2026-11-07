"""Env → Config binder：唯一的环境变量解析入口。

语义与现网 `Config.from_env()` 保持一致：
  * 三个 GitLab 必填项缺失时抛 `RuntimeError("missing required env var: ...")`；
  * 周报队列名缺省时派生自主队列（`RQ_QUEUE_NAME` + "-weekly"）；
  * 仍然初始化 data/repos/worktrees/weekly_reports 目录（启动副作用）。

两个平面的空值语义不同，不要混用：
  * env 平面（`coerce`）：空串/None 都表示"未提供"，回退 schema 默认值；
  * 场景平面（`coerce_scenario`）：只有 None 表示"继承"（跳过），
    `""`/`[]` 是显式值（空字符串、空 tuple），不得回退默认值。
"""
from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from reviewagent.settings.schema import SCHEMA, SCHEMA_BY_NAME, FieldSpec

if TYPE_CHECKING:  # 仅类型；运行期由 from_env() 内部延迟 import，避免循环依赖
    from reviewagent.config import Config

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}

# "未提供"哨兵：env 平面里表示空值/缺失；场景平面里只由 None 产生。
UNSET = object()

_EXPLICIT_ENV: frozenset[str] = frozenset()

# 现网 `_env(..., required=True)` 的必填项：缺失或为空即启动失败。
_REQUIRED: tuple[str, ...] = ("gitlab_url", "gitlab_pat", "gitlab_webhook_secret")


def explicit_env() -> frozenset[str]:
    return _EXPLICIT_ENV


def _fail(spec: FieldSpec, raw: Any, why: str) -> ValueError:
    return ValueError(f"config field '{spec.name}' (env={spec.env}): {why}: {raw!r}")


def coerce(spec: FieldSpec, raw: Any) -> Any:
    """env 平面解析：空串/None = 未提供（返回 UNSET），tuple 按逗号分隔。"""
    if raw is None:
        return UNSET
    if isinstance(raw, str):
        raw = raw.strip()
        if spec.type is tuple:
            return tuple(x.strip() for x in raw.split(",") if x.strip())
        if raw == "":
            return UNSET
        if spec.type is bool:
            low = raw.lower()
            if low in _TRUE:
                return True
            if low in _FALSE:
                return False
            raise _fail(spec, raw, "invalid bool (expected 1/0/true/false/yes/no/on/off)")
        if spec.type is int:
            try:
                return int(raw)
            except ValueError as exc:
                raise _fail(spec, raw, "invalid int") from exc
        if spec.type is float:
            try:
                return float(raw)
            except ValueError as exc:
                raise _fail(spec, raw, "invalid float") from exc
        if spec.type is Path:
            return Path(raw)
        if spec.choices:
            # 保持现网 `.lower()` 语义（QODER_SDK_EDITION / IMPROVE_REVIEW_MODE / IMPROVE_MIN_SEVERITY）
            low = raw.lower()
            if low in spec.choices:
                return low
        return raw
    if spec.type is tuple and isinstance(raw, list):
        return tuple(raw)
    return raw


def coerce_scenario(spec: FieldSpec, raw: Any) -> Any:
    """场景平面解析：None = 继承（返回 UNSET）；显式空值原样保留。

    与 `coerce` 的差异（刻意不复用 env 语义）：
      * `""` / `[]` / `()` 都是显式值，不转换成 UNSET；
      * 不做空串回退，也不 strip 字符串（profile.yaml 是结构化数据）；
      * 类型不符时不在这里抛错，交给 `validate` 统一给出含字段名的错误。
    """
    if raw is None:
        return UNSET
    if spec.type is tuple:
        if isinstance(raw, str):
            return tuple(x.strip() for x in raw.split(",") if x.strip())
        if isinstance(raw, (list, tuple)):
            return tuple(raw)
        return raw
    if isinstance(raw, str):
        if spec.type is bool:
            low = raw.strip().lower()
            if low in _TRUE:
                return True
            if low in _FALSE:
                return False
            return raw
        if spec.type is int:
            try:
                return int(raw.strip())
            except ValueError:
                return raw
        if spec.type is float:
            try:
                return float(raw.strip())
            except ValueError:
                return raw
        if spec.type is Path:
            return Path(raw)
    return raw


def validate(spec: FieldSpec, value: Any) -> Any:
    """类型归一 + choices/min/max 校验；返回归一后的值（错误消息含字段名）。

    归一规则：
      * bool 字段接受 `0/1`（转 bool），拒绝其他类型；
      * int 字段拒绝 float / bool（不做静默取整，避免精度悄悄丢失）；
      * float 字段接受 int/float 并转 float；str/tuple/Path 做严格类型检查。
    """
    t = spec.type
    if t is bool:
        if isinstance(value, bool):
            pass
        elif isinstance(value, int) and value in (0, 1):
            value = bool(value)
        else:
            raise _fail(spec, value, "expected bool (true/false or 0/1)")
    elif t is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise _fail(spec, value, f"expected int (got {type(value).__name__})")
    elif t is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _fail(spec, value, f"expected float (got {type(value).__name__})")
        value = float(value)
    elif t is str:
        if not isinstance(value, str):
            raise _fail(spec, value, f"expected str (got {type(value).__name__})")
    elif t is tuple:
        if isinstance(value, list):
            value = tuple(value)
        elif not isinstance(value, tuple):
            raise _fail(spec, value, f"expected tuple (got {type(value).__name__})")
    elif t is Path:
        if isinstance(value, str):
            value = Path(value)
        elif not isinstance(value, Path):
            raise _fail(spec, value, f"expected Path (got {type(value).__name__})")
    if spec.choices and value not in spec.choices:
        raise _fail(spec, value, f"not in {spec.choices}")
    if spec.minimum is not None and isinstance(value, (int, float)) and value < spec.minimum:
        raise _fail(spec, value, f"< minimum {spec.minimum}")
    if spec.maximum is not None and isinstance(value, (int, float)) and value > spec.maximum:
        raise _fail(spec, value, f"> maximum {spec.maximum}")
    return value


def _ensure_dirs(cfg: Config) -> None:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.repos_dir.mkdir(parents=True, exist_ok=True)
    cfg.worktrees_dir.mkdir(parents=True, exist_ok=True)
    cfg.weekly_reports_dir.mkdir(parents=True, exist_ok=True)


def resolve_env(spec: FieldSpec, src: Mapping[str, str]) -> tuple[Any, bool]:
    """按 spec 读取 env；canonical env 优先于 aliases（显式 canonical 不被覆盖）。

    返回 `(value, explicit)`；未提供时 value 为 UNSET。
    """
    value: Any = UNSET
    explicit = False
    for alias in spec.aliases:  # 先读别名，再由 canonical 覆盖
        if alias in src:
            parsed = coerce(spec, src[alias])
            if parsed is not UNSET:
                value, explicit = parsed, True
    if spec.env and spec.env in src:
        parsed = coerce(spec, src[spec.env])
        if parsed is not UNSET:
            value, explicit = parsed, True
    return value, explicit


def spec_env_override(spec: FieldSpec) -> Any:
    """二次解析 env：canonical 优先于 aliases，返回 `UNSET` 或 coerce 后的值.

    供场景装配在 config 导入之后再次用 env 覆盖 profile 值（§5.4：env 只覆盖
    运行配置，不污染 profile 数据）。空串/None 仍按 env 平面语义返回 `UNSET`。
    """
    value, _ = resolve_env(spec, os.environ)
    return value


def from_env(environ: Mapping[str, str] | None = None) -> Config:
    global _EXPLICIT_ENV
    src = dict(os.environ if environ is None else environ)
    kwargs: dict[str, Any] = {}
    explicit: set[str] = set()
    explicit_fields: set[str] = set()
    for spec in SCHEMA:
        value, is_explicit = spec.default, False
        resolved, found = resolve_env(spec, src)
        if found:
            value, is_explicit = resolved, True
        kwargs[spec.name] = validate(spec, value)
        if is_explicit:
            explicit.add(spec.env or spec.name)
            explicit_fields.add(spec.name)
    # 现网语义：周报队列默认派生自主队列，可用 RQ_WEEKLY_QUEUE_NAME 覆盖。
    if "rq_weekly_queue_name" not in explicit_fields:
        kwargs["rq_weekly_queue_name"] = f"{kwargs['rq_queue_name']}-weekly"
    for name in _REQUIRED:
        if not kwargs[name]:
            raise RuntimeError(f"missing required env var: {SCHEMA_BY_NAME[name].env}")
    _EXPLICIT_ENV = frozenset(explicit)
    from reviewagent.config import Config

    cfg = Config(**kwargs)
    _ensure_dirs(cfg)
    return cfg


def fingerprint(values: Mapping[str, Any]) -> str:
    payload = {}
    for name, value in values.items():
        spec = SCHEMA_BY_NAME[name]
        if spec.secret:
            payload[name] = "***"
        elif isinstance(value, Path):
            payload[name] = str(value)
        elif isinstance(value, tuple):
            payload[name] = list(value)
        else:
            payload[name] = value
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
