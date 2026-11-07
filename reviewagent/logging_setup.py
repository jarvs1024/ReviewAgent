"""日志 — loguru 统一封装（text/json 双格式 + 关联上下文 + 脱敏）.

设计约束（spec §9.2）：

* **text 默认**，输出观感与现网一致；`LOG_FORMAT=json` 时切到机读单行 JSON，
  两个 sink 共用同一事件 schema，切换不需要改调用点；
* 所有 sink `diagnose=False`（安全修复：不打印局部变量，避免 PAT/prompt 泄漏）；
* 关联上下文走 `ContextVar`：`bind_context(**kw)` / `clear_context()` /
  `current_context()`，sink filter 在**调用线程**读取并合入 `record["extra"]`
  （loguru 的 filter 在 emit 入队前执行，因此 `enqueue=True` 也拿得到本线程 context）；
* 统一脱敏：显式 secret 值、URL 内嵌凭据、`token/password/secret/authorization`
  的 `k=v` / `k: v` 形式；text sink 另把换行/回车/控制字符转义成可见转义，
  防止伪造日志行（日志注入）。
"""
from __future__ import annotations

import json
import re
import sys
import traceback
from collections.abc import Iterable, Mapping
from contextvars import ContextVar
from typing import Any

from loguru import logger

from reviewagent.config import config

# JSON 行 schema 版本；采集侧据此兼容演进（字段名一旦发布不随意改名）.
SCHEMA_VERSION = 1

_LOG_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
    "<level>{message}</level>"
)

# text sink 在现网前缀之后追加上下文后缀；无 context 时 `_ctx_suffix` 为空串，
# 输出与旧格式逐字节一致.
TEXT_FORMAT = _LOG_FORMAT + "{extra[_ctx_suffix]}"

# json sink 的序列化结果放到 extra 的这个键；text 格式不使用.
_JSON_LINE_KEY = "_json_line"

# 需要整值脱敏的 secret 配置字段（按值替换，空值忽略）.
_SECRET_FIELD_NAMES: tuple[str, ...] = (
    "gitlab_pat",
    "gitlab_webhook_secret",
    "qoder_sdk_pat",
    "opencode_password",
)

# `scheme://user:pass@host` → `scheme://***@host`
_URL_CREDS_RE = re.compile(
    r"(?i)([a-z][a-z0-9+.\-]*://)([^/\s:@]+):([^/\s@]+)@"
)

# `Authorization: Bearer xyz` / `Authorization=Basic b64` → `Authorization: ***`
# （连 scheme 一起吞掉，只保留键名与分隔符；否则 `xyz` / base64 会残留）.
_AUTH_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])(authorization)(\s*[:=]\s*)"
    r"(?:[A-Za-z][A-Za-z0-9+._-]*\s+)?(\"[^\"]*\"|'[^']*'|[^\s,;&]+)"
)

# `token=...` / `password: ...` / `secret=...` → 值替换为 ***（保留键名）.
# `authorization` 由 `_AUTH_RE` 单独处理（需要吞掉 scheme）.
_SECRET_KEY_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])(token|password|secret)(\s*[:=]\s*)"
    r"(\"[^\"]*\"|'[^']*'|[^\s,;&]+)"
)

# loguru 内部保留的 extra 键：不参与脱敏与上下文后缀（其它 `_foo` 照常处理）.
_INTERNAL_KEYS = frozenset({"_ctx_suffix", _JSON_LINE_KEY})

_ESCAPE_MAP = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}

# 调用线程上下文；子线程 / 新 task 不会自动继承，需在任务创建时显式绑定.
# default 用 None（避免共享可变默认值），读取时统一走 `_context_values()`.
_context: ContextVar[dict[str, Any] | None] = ContextVar(
    "reviewagent_log_context", default=None
)

# setup_logging() 时解析出的 secret 值；filter 在每次 emit 时读取（保持热更新）.
_SECRETS: tuple[str, ...] = ()


# ---------------------------------------------------------------- redaction


def _replace_secret(text: str, secret: str) -> str:
    """按值替换 secret；短值（<4）用词边界，避免把普通单词切碎成 `***`."""
    if len(secret) < 4:
        pattern = rf"(?<![A-Za-z0-9]){re.escape(secret)}(?![A-Za-z0-9])"
        return re.sub(pattern, "***", text)
    return text.replace(secret, "***")


def redact(text: str, *, secrets: Iterable[str] = ()) -> str:
    """脱敏：显式 secret 值 → URL 内嵌凭据 → Authorization 头 → `k=v` / `k: v`.

    顺序固定：先按值替换（`pat=abc123` + secret `abc123` → `pat=***`），
    再处理带凭据的 URL，然后整段吞掉 `Authorization` 的 scheme 与凭据，
    最后按键名规则兜底；全程保留键名便于排障.
    """
    if not text:
        return text
    result = str(text)
    values = {str(s) for s in secrets if str(s).strip()}
    for secret in sorted(values, key=len, reverse=True):
        result = _replace_secret(result, secret)
    result = _URL_CREDS_RE.sub(lambda m: f"{m.group(1)}***@", result)
    result = _AUTH_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}***", result)
    result = _SECRET_KEY_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}***", result)
    return result


def _escape_control(text: str) -> str:
    """把换行/回车/控制字符转义成可见转义，保证一条记录一行."""
    if not text:
        return text
    out: list[str] = []
    for ch in text:
        escaped = _ESCAPE_MAP.get(ch)
        if escaped is not None:
            out.append(escaped)
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\x{ord(ch):02x}")
        else:
            out.append(ch)
    return "".join(out)


def _safe_redact(value: Any, secrets: Iterable[str]) -> Any:
    """递归脱敏：str 直接 redact，dict/list/tuple 逐项处理，其它类型原样返回."""
    if isinstance(value, str):
        return redact(value, secrets=secrets)
    if isinstance(value, Mapping):
        return {key: _safe_redact(item, secrets) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        items = [_safe_redact(item, secrets) for item in value]
        try:
            return type(value)(items)
        except Exception:  # pragma: no cover - 具名元组等构造失败的兜底
            return items
    return value


# ---------------------------------------------------------------- json


def _level_name(level: Any) -> str:
    """兼容 `{"name": "INFO"}` / loguru `RecordLevel(name=..., no=...)` / 裸字符串."""
    if isinstance(level, str):
        return level
    if isinstance(level, Mapping):
        return str(level.get("name") or "")
    name = getattr(level, "name", None)
    if name is not None:
        return str(name)
    return "" if level is None else str(level)


def _time_text(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:  # pragma: no cover - 畸形 datetime 兜底
            return str(value)
    return str(value)


class JsonFormatter:
    """单行 JSON 序列化（机读；`ensure_ascii=False` 保留中文）.

    固定字段：`ts` / `schema_version` / `level` / `event` / `component` / `message`；
    其余 `record["extra"]` 里的上下文字段（`run_id` / `pipeline_id` / `job_id` /
    `project_id` / `mr_iid` / `profile` / `stage` / `attempt` …）原样展开；
    message 与字符串 extra 统一走 `redact`.
    """

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._secrets: tuple[str, ...] = tuple(str(s) for s in secrets if s)

    def format(self, record: Mapping[str, Any]) -> str:
        extra = dict(record.get("extra") or {})
        for key in _INTERNAL_KEYS:
            extra.pop(key, None)
        event = extra.pop("event", "")
        component = extra.pop("component", "")

        payload: dict[str, Any] = {
            "ts": _time_text(record.get("time")),
            "schema_version": SCHEMA_VERSION,
            "level": _level_name(record.get("level")),
            "event": _safe_redact(event, self._secrets),
            "component": _safe_redact(component, self._secrets),
            "message": redact(str(record.get("message", "")), secrets=self._secrets),
        }
        for key, value in extra.items():
            if key in payload:  # ts/schema_version/level/event/component/message 不可被 extra 覆盖
                continue
            payload[key] = _safe_redact(value, self._secrets)
        return json.dumps(payload, ensure_ascii=False, default=str)


def _escaped_json_template(formatter: JsonFormatter):
    """把 `JsonFormatter.format` 适配成 loguru 的 callable `format` 参数.

    loguru 对 callable `format` 的语义是「返回格式化模板字符串」（不是最终文本）：
    模板随后走 `Colorizer.prepare_format` + `str.format_map`，行尾与 `{exception}`
    也不会自动追加。若把 JSON 行本身当模板返回，行内的 `{}` 与 `<...>` 会被当成
    格式化字段 / 颜色标签（`ValueError: Tag "<string>" ...`），且动态模板按记录
    编码进 LRU 会反复重解析.

    因此采用 loguru 官方 JSON 配方：把序列化结果放进 `extra`，返回**固定模板**
    `{extra[_json_line]}\\n`（常量模板只会被解析一次，用户内容由 format_map 代入）.
    """

    def _template(record: dict[str, Any]) -> str:
        extra = record.get("extra")
        if not isinstance(extra, dict):  # pragma: no cover - loguru 记录恒为 dict
            extra = {}
            record["extra"] = extra
        extra[_JSON_LINE_KEY] = formatter.format(record)
        return "{extra[" + _JSON_LINE_KEY + "]}\n"

    return _template


# ---------------------------------------------------------------- context


def _context_values() -> dict[str, Any]:
    return dict(_context.get() or {})


def bind_context(**kwargs: Any) -> None:
    """合并式绑定上下文（`None` 值忽略，其余覆盖同名旧值）."""
    merged = _context_values()
    for key, value in kwargs.items():
        if value is None:
            continue
        merged[key] = value
    _context.set(merged)


def clear_context() -> None:
    """清空当前（线程 / task）上下文."""
    _context.set({})


def current_context() -> dict[str, Any]:
    """当前上下文副本（调用方可安全修改）."""
    return _context_values()


# ---------------------------------------------------------------- sink filters


def _exception_redacted(
    record: Mapping[str, Any], secrets: Iterable[str], *, escape: bool = False,
) -> Any:
    """尽量把异常 message 二次脱敏；构造失败时保持原异常（不破坏 traceback）.

    `escape=True`（text sink）在脱敏后再把换行/控制字符转义，避免异常 message
    里的伪造成日志行；JSON sink 只脱敏，转义交给序列化器.
    """
    exc = record.get("exception")
    if not exc:
        return exc
    value = getattr(exc, "value", None)
    if value is None:
        return exc
    try:
        text = str(value)
        fixed = redact(text, secrets=secrets)
        if escape:
            fixed = _escape_control(fixed)
        if fixed == text:
            return exc
        new_value = _rebuild_exception_value(value, fixed)
        return type(exc)(exc.type, new_value, exc.traceback)
    except Exception:  # pragma: no cover - 脱敏失败不应影响日志输出
        return exc


def _rebuild_exception_value(value: BaseException, fixed: str) -> BaseException:
    """用脱敏后的 message 重建同类型异常（尽量保留类型，便于告警分类）."""
    try:
        return type(value)(fixed)
    except Exception:
        pass
    try:
        new_value = type(value).__new__(type(value), fixed)
        new_value.args = (fixed,)
        return new_value
    except Exception:  # pragma: no cover - 无法重建类型时退化为 RuntimeError
        return RuntimeError(fixed)


def _exception_fields(exc: Any) -> dict[str, str]:
    """JSON sink 用：异常类型 / message / 堆栈（整段脱敏，交给序列化器转义）."""
    exc_type = getattr(exc, "type", None)
    fields: dict[str, str] = {
        "error_type": getattr(exc_type, "__name__", "") or "",
        "error_message": "",
        "traceback": "",
    }
    try:
        fields["error_message"] = str(getattr(exc, "value", ""))
    except Exception:  # pragma: no cover - 异常 str() 自身失败
        pass
    try:
        fields["traceback"] = "".join(
            traceback.format_exception(exc.type, exc.value, exc.traceback)
        )
    except Exception:  # pragma: no cover - 畸形 traceback 兜底
        fields["traceback"] = ""
    return {key: redact(value, secrets=_SECRETS) for key, value in fields.items()}


def _prepare(record: dict[str, Any], *, escape: bool) -> dict[str, Any]:
    """把本线程 context 合入 extra（显式 extra 优先），并对 message/extra 脱敏."""
    context = _context_values()
    explicit = record.get("extra")
    merged = dict(context)
    if isinstance(explicit, Mapping):
        merged.update(explicit)
    record["extra"] = merged
    record["exception"] = _exception_redacted(record, _SECRETS, escape=escape)
    record["message"] = redact(str(record.get("message", "")), secrets=_SECRETS)

    suffix_parts: list[str] = []
    for key in list(merged):
        if key in _INTERNAL_KEYS:  # loguru 内部保留键：不脱敏、不进后缀
            continue
        merged[key] = _safe_redact(merged[key], _SECRETS)
        if key in context and context[key] is not None:
            text = str(merged[key])
            suffix_parts.append(f"{key}={_escape_control(text) if escape else text}")

    if escape:
        record["message"] = _escape_control(record["message"])
        merged["_ctx_suffix"] = " | " + " ".join(suffix_parts) if suffix_parts else ""
    else:
        merged.pop("_ctx_suffix", None)
    return merged


def _text_filter(record: dict[str, Any]) -> bool:
    """text sink：合入 context、脱敏、控制字符转义，并生成 `_ctx_suffix`."""
    _prepare(record, escape=True)
    return True


def _json_filter(record: dict[str, Any]) -> bool:
    """json sink：合入 context、脱敏；转义交给 json 序列化器.

    loguru 的 callable format 不会自动追加 `{exception}`（只有字符串 format 才会），
    因此这里把异常类型 / message / 堆栈落成结构化字段，保持单行 JSON.
    """
    merged = _prepare(record, escape=False)
    merged.pop(_JSON_LINE_KEY, None)
    exc = record.get("exception")
    if exc:
        merged.update(_exception_fields(exc))
    return True


# ---------------------------------------------------------------- setup


def _configured_secrets() -> tuple[str, ...]:
    values: list[str] = []
    for name in _SECRET_FIELD_NAMES:
        raw = getattr(config, name, "") or ""
        value = str(raw)
        if value:
            values.append(value)
    return tuple(values)


def setup_logging(*, fmt: str | None = None) -> None:
    """全局日志配置；幂等（先 `logger.remove()`）.

    输出目标：
    - stderr（console / journalctl）：level=`config.log_level`，`backtrace=False`
    - `{data_dir}/log/reviewagent.log`：`enqueue=True`（多进程安全）、
      `backtrace=True`、`diagnose=False`，单进程默认 50MB/30 天/gz 轮转；
      `LOG_ROTATION=none` 时关闭轮转并同时清空 retention/compression，
      由外部 logrotate / journald 接管，避免多进程双重轮转.
    """
    global _SECRETS

    logger.remove()
    secrets = _configured_secrets()
    _SECRETS = secrets

    requested = fmt if fmt is not None else getattr(config, "log_format", "text")
    selected = str(requested or "").strip().lower()
    invalid = selected not in ("text", "json")
    if invalid:
        selected = "text"

    json_mode = selected == "json"
    sink_filter = _json_filter if json_mode else _text_filter
    sink_format: Any = (
        _escaped_json_template(JsonFormatter(secrets=secrets)) if json_mode else TEXT_FORMAT
    )

    # 1) stderr — 保持原有观感（带颜色, 方便终端查看）
    logger.add(
        sys.stderr,
        level=config.log_level,
        format=sink_format,
        filter=sink_filter,
        backtrace=False,
        diagnose=False,
    )

    # 2) 文件日志 — 默认 50MB 轮转 / 保留 30 天 / gzip 压缩
    log_dir = config.data_dir / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    rotation = (
        None
        if str(getattr(config, "log_rotation", "single")).strip().lower() == "none"
        else "50 MB"
    )
    logger.add(
        str(log_dir / "reviewagent.log"),
        level=config.log_level,
        format=sink_format,
        filter=sink_filter,
        rotation=rotation,
        retention="30 days" if rotation is not None else None,
        compression="gz" if rotation is not None else None,
        encoding="utf-8",
        enqueue=True,        # 多进程安全 (worker fork 后不丢日志)
        backtrace=True,      # 文件日志保留 traceback, 方便排查
        diagnose=False,      # 安全修复: 不打印局部变量, 防止 PAT/prompt 泄漏
    )

    if invalid:
        logger.warning("logging.format_invalid fmt={} fallback=text", requested)


__all__ = [
    "JsonFormatter",
    "SCHEMA_VERSION",
    "TEXT_FORMAT",
    "bind_context",
    "clear_context",
    "current_context",
    "logger",
    "redact",
    "setup_logging",
]
