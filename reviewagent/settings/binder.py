"""Env → Config binder：唯一的环境变量解析入口。

语义与现网 `Config.from_env()` 保持一致：
  * 三个 GitLab 必填项缺失时抛 `RuntimeError("missing required env var: ...")`；
  * 周报队列名缺省时派生自主队列（`RQ_QUEUE_NAME` + "-weekly"）；
  * 仍然初始化 data/repos/worktrees/weekly_reports 目录（启动副作用）。
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
_UNSET = object()

_EXPLICIT_ENV: frozenset[str] = frozenset()

# 现网 `_env(..., required=True)` 的必填项：缺失或为空即启动失败。
_REQUIRED: tuple[str, ...] = ("gitlab_url", "gitlab_pat", "gitlab_webhook_secret")


def explicit_env() -> frozenset[str]:
    return _EXPLICIT_ENV


def _fail(spec: FieldSpec, raw: Any, why: str) -> ValueError:
    return ValueError(f"config field '{spec.name}' (env={spec.env}): {why}: {raw!r}")


def coerce(spec: FieldSpec, raw: Any) -> Any:
    if raw is None:
        return _UNSET
    if isinstance(raw, str):
        raw = raw.strip()
        if spec.type is tuple:
            return tuple(x.strip() for x in raw.split(",") if x.strip())
        if raw == "":
            return _UNSET
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


def _validate(spec: FieldSpec, value: Any) -> None:
    if spec.choices and value not in spec.choices:
        raise _fail(spec, value, f"not in {spec.choices}")
    if spec.minimum is not None and isinstance(value, (int, float)) and value < spec.minimum:
        raise _fail(spec, value, f"< minimum {spec.minimum}")
    if spec.maximum is not None and isinstance(value, (int, float)) and value > spec.maximum:
        raise _fail(spec, value, f"> maximum {spec.maximum}")


def _ensure_dirs(cfg) -> None:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.repos_dir.mkdir(parents=True, exist_ok=True)
    cfg.worktrees_dir.mkdir(parents=True, exist_ok=True)
    cfg.weekly_reports_dir.mkdir(parents=True, exist_ok=True)


def from_env(environ: Mapping[str, str] | None = None) -> Config:
    global _EXPLICIT_ENV
    src = dict(os.environ if environ is None else environ)
    kwargs: dict[str, Any] = {}
    explicit: set[str] = set()
    explicit_fields: set[str] = set()
    for spec in SCHEMA:
        value, is_explicit = spec.default, False
        if spec.env and spec.env in src:
            parsed = coerce(spec, src[spec.env])
            if parsed is not _UNSET:
                value, is_explicit = parsed, True
        for alias in spec.aliases:
            if alias in src:
                parsed = coerce(spec, src[alias])
                if parsed is not _UNSET:
                    value, is_explicit = parsed, True
        if spec.name == "data_dir" and not isinstance(value, Path):
            value = Path(value)
        _validate(spec, value)
        kwargs[spec.name] = value
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
