"""T17 日志治理：text/json 双格式、上下文绑定、脱敏与 sink 配置.

覆盖 spec §9.2.1/§9.2.2/§9.2.4：

* JSON 行稳定字段与 schema_version；
* 脱敏（显式 secret / URL 凭据 / `token=` 形式）；
* text filter 的控制字符转义与上下文后缀（无 context 时与基线一致）；
* `setup_logging` 的 sink 参数（幂等、diagnose=False、rotation 开关）；
* 线程隔离（子线程 bind 不影响主线程）。
"""
from __future__ import annotations

import json
import sys
import threading
from datetime import datetime

import pytest
from loguru import logger as loguru_logger

import reviewagent.logging_setup as logging_setup
from reviewagent.logging_setup import (
    SCHEMA_VERSION,
    TEXT_FORMAT,
    JsonFormatter,
    bind_context,
    clear_context,
    current_context,
    redact,
    setup_logging,
)


@pytest.fixture(autouse=True)
def _clean_context():
    saved = logging_setup._SECRETS
    clear_context()
    yield
    clear_context()
    logging_setup._SECRETS = saved


@pytest.fixture
def capture_text():
    """临时 text sink：与生产 sink 同 filter/format，结束一定移除."""
    lines: list[str] = []
    handler_id = loguru_logger.add(
        lambda message: lines.append(str(message).rstrip("\n")),
        level="INFO",
        format=TEXT_FORMAT,
        filter=logging_setup._text_filter,
        colorize=False,
    )
    try:
        yield lines
    finally:
        loguru_logger.remove(handler_id)


@pytest.fixture
def capture_text_plain():
    """临时 text sink（`diagnose=False`）：异常用例不打印局部变量，只验消息脱敏."""
    lines: list[str] = []
    handler_id = loguru_logger.add(
        lambda message: lines.append(str(message).rstrip("\n")),
        level="INFO",
        format=TEXT_FORMAT,
        filter=logging_setup._text_filter,
        colorize=False,
        backtrace=False,
        diagnose=False,
    )
    try:
        yield lines
    finally:
        loguru_logger.remove(handler_id)


# ---------------------------------------------------------------- 原样断言（brief）


def test_json_formatter_emits_stable_fields():
    record = {
        "time": "2026-11-07T00:00:00Z",
        "level": {"name": "INFO"},
        "message": "x",
        "extra": {"run_id": "r1"},
    }
    line = JsonFormatter().format(record)
    data = json.loads(line)
    assert data["level"] == "INFO"
    assert data["run_id"] == "r1"


def test_redact_secrets():
    assert redact("pat=abc123", secrets={"abc123"}) == "pat=***"


def test_secret_field_names_derived_from_schema():
    """脱敏名单从 SCHEMA 派生：所有 secret 字段 + webhook URL（仅日志层）."""
    from reviewagent.settings.schema import SCHEMA

    names = logging_setup._SECRET_FIELD_NAMES
    for spec in SCHEMA:
        if spec.secret:
            assert spec.name in names, spec.name
    assert "gitlab_url" in names
    assert "weekly_dingtalk_webhook_url" in names
    # webhook URL 不标 schema secret（保持 config_fingerprint 区分度）。
    assert "weekly_dingtalk_webhook_url" not in {
        spec.name for spec in SCHEMA if spec.secret
    }
    assert len(names) == len(set(names))


# ---------------------------------------------------------------- JsonFormatter


def _base_record(**overrides):
    record = {
        "time": "2026-11-07T00:00:00Z",
        "level": {"name": "INFO"},
        "message": "hello",
        "extra": {},
    }
    record.update(overrides)
    return record


def test_json_formatter_level_accepts_str_and_dict():
    as_dict = json.loads(JsonFormatter().format(_base_record(level={"name": "WARNING"})))
    as_str = json.loads(JsonFormatter().format(_base_record(level="ERROR")))
    assert as_dict["level"] == "WARNING"
    assert as_str["level"] == "ERROR"


def test_json_formatter_time_accepts_datetime_and_str():
    stamp = datetime(2026, 11, 7, 1, 2, 3)
    as_dt = json.loads(JsonFormatter().format(_base_record(time=stamp)))
    as_str = json.loads(JsonFormatter().format(_base_record(time="2026-11-07T00:00:00Z")))
    assert as_dt["ts"] == stamp.isoformat()
    assert as_str["ts"] == "2026-11-07T00:00:00Z"


def test_json_formatter_context_fields_and_schema_version():
    record = _base_record(
        extra={
            "run_id": "r1",
            "pipeline_id": "p1",
            "job_id": "j1",
            "project_id": 7,
            "mr_iid": 12,
            "profile": "python-ssd",
            "stage": "improve",
            "attempt": 2,
            "event": "review.start",
            "component": "worker",
        }
    )
    data = json.loads(JsonFormatter().format(record))
    assert data["schema_version"] == SCHEMA_VERSION
    assert data["event"] == "review.start"
    assert data["component"] == "worker"
    for key, value in {
        "run_id": "r1",
        "pipeline_id": "p1",
        "job_id": "j1",
        "project_id": 7,
        "mr_iid": 12,
        "profile": "python-ssd",
        "stage": "improve",
        "attempt": 2,
    }.items():
        assert data[key] == value
    # 单行输出（无裸换行）
    assert "\n" not in JsonFormatter().format(record)


def test_json_formatter_redacts_message_and_extra():
    formatter = JsonFormatter(secrets={"glpat-SECRET"})
    record = _base_record(
        message="auth with glpat-SECRET at https://bob:hunter2@gitlab.test/api",
        extra={"detail": "token=abc123", "run_id": "r1"},
    )
    data = json.loads(formatter.format(record))
    assert "glpat-SECRET" not in data["message"]
    assert "hunter2" not in data["message"]
    assert "bob" not in data["message"]
    assert data["detail"] == "token=***"
    assert data["run_id"] == "r1"


def test_json_formatter_reserved_fields_not_overridden_by_extra():
    record = _base_record(
        extra={
            "ts": "spoof-ts",
            "schema_version": 99,
            "level": "DEBUG",
            "message": "spoof-message",
            "event": "review.start",
        }
    )
    data = json.loads(JsonFormatter().format(record))
    assert data["ts"] == "2026-11-07T00:00:00Z"
    assert data["schema_version"] == SCHEMA_VERSION
    assert data["level"] == "INFO"
    assert data["message"] == "hello"
    assert data["event"] == "review.start"


def test_json_formatter_redacts_nested_extra():
    formatter = JsonFormatter(secrets={"glpat-SECRET"})
    record = _base_record(
        extra={
            "ctx": {"pat": "glpat-SECRET", "inner": [{"auth": "Authorization: Bearer xyz"}]},
            "seq": ("ok", "token=abc123"),
        }
    )
    raw = formatter.format(record)
    data = json.loads(raw)
    assert "glpat-SECRET" not in raw
    assert "xyz" not in raw
    assert "abc123" not in raw
    assert data["ctx"]["pat"] == "***"
    assert data["ctx"]["inner"][0]["auth"] == "Authorization: ***"
    assert data["seq"] == ["ok", "token=***"]


def test_redact_url_credentials_and_key_values():
    text = "clone https://user:pw@host.example/repo.git Authorization: Bearer xyz password=hunter2 secret: s3 auth=token"
    out = redact(text)
    assert "user" not in out and "pw@" not in out
    assert "hunter2" not in out
    # 真实凭据不残留（不是只断言 scheme 名 Bearer 消失）
    assert "xyz" not in out
    assert "Authorization: ***" in out
    assert "https://***@host.example/repo.git" in out
    assert "password=***" in out
    assert "secret: ***" in out


def test_redact_authorization_swallows_scheme_and_credential():
    bearer = redact("Authorization: Bearer xyz")
    assert "xyz" not in bearer
    assert bearer == "Authorization: ***"

    basic = redact("Authorization: Basic dXNlcjpwYXNz")
    assert "dXNlcjpwYXNz" not in basic
    assert "Basic" not in basic
    assert basic == "Authorization: ***"

    # 无 scheme 的裸值同样整段吞掉；`=` 分隔也覆盖
    assert redact("authorization=glpat-abc") == "authorization=***"
    # 原按值替换的行为保持不变
    assert redact("pat=abc123", secrets={"abc123"}) == "pat=***"


def test_redact_short_secret_uses_word_boundary():
    # 短 secret（<4）只在词边界替换，避免把普通单词切碎
    assert redact("value ab1 and xab1y", secrets={"ab1"}) == "value *** and xab1y"
    # 长 secret 仍全局替换
    assert redact("xxTOKEN99yy", secrets={"TOKEN99"}) == "xx***yy"
    # 空 / 纯空白 secret 忽略
    assert redact("nothing here", secrets={"", "   "}) == "nothing here"


# ---------------------------------------------------------------- text filter


def test_text_filter_escapes_newlines_and_forgery(capture_text):
    loguru_logger.info("a\nlevel=ERROR")
    line = "".join(capture_text)
    assert "\n" not in line
    assert "\\nlevel=ERROR" in line
    # 只有一条记录，且没有伪造出的独立 level=ERROR 行
    assert len(capture_text) == 1


def test_text_filter_escape_control_chars(capture_text):
    loguru_logger.info("x\x00y\rz")
    line = "".join(capture_text)
    assert "\\x00" in line
    assert "\\r" in line
    assert "\x00" not in line
    assert "\r" not in line


def test_text_filter_appends_context_suffix(capture_text):
    bind_context(run_id="r1", stage="improve")
    loguru_logger.info("hello")
    line = capture_text[-1]
    assert "run_id=r1" in line
    assert "stage=improve" in line

    clear_context()
    loguru_logger.info("hello-again")
    assert "run_id=" not in capture_text[-1]


def test_text_output_matches_baseline_without_context(capture_text):
    loguru_logger.info("plain message")
    line = capture_text[-1]
    assert line.endswith("plain message")
    # 基线前缀自带两个 " | "（time | level | name:func:line - message），无 context 不追加.
    assert line.count(" | ") == 2
    assert " | " not in line.split(" - ", 1)[1]


def test_text_filter_redacts_message(capture_text, monkeypatch):
    monkeypatch.setattr(logging_setup, "_SECRETS", ("glpat-SECRET",))
    loguru_logger.info("using glpat-SECRET on https://u:p@gitlab.test")
    line = capture_text[-1]
    assert "glpat-SECRET" not in line
    assert "u:p@" not in line
    assert "***" in line


def test_prepare_redacts_non_internal_underscore_keys(monkeypatch):
    monkeypatch.setattr(logging_setup, "_SECRETS", ("glpat-SECRET",))
    record = _base_record(
        extra={"_custom": "glpat-SECRET", "_nested": ["glpat-SECRET"], "_json_line": "glpat-SECRET"},
    )
    merged = logging_setup._prepare(record, escape=True)
    # 非内部 `_foo` 照常脱敏
    assert merged["_custom"] == "***"
    assert merged["_nested"] == ["***"]
    # loguru 内部键原样跳过
    assert merged["_json_line"] == "glpat-SECRET"


def test_text_filter_escapes_forged_exception_message(capture_text_plain, monkeypatch):
    secret = "glpat-" + "SECRET"  # 拼出来，避免 traceback 的源码行回显字面量
    monkeypatch.setattr(logging_setup, "_SECRETS", (secret,))
    forged = "\n2026-11-07 00:00:00 | ERROR | fake:fn:1 - forged"
    message = f"boom {secret}{forged}"
    try:
        raise RuntimeError(message)
    except RuntimeError:
        loguru_logger.exception("failed")

    # 一条记录一条输出（异常 message 的换行没有拆出新记录）
    assert len(capture_text_plain) == 1
    out = capture_text_plain[0]
    assert secret not in out
    # 伪造前缀被 `\n` 转义中和，不会以裸行首形式出现
    assert forged not in out
    assert "boom ***" + forged.replace("\n", "\\n") in out


def test_text_filter_keeps_exception_type_when_rebuilding(capture_text_plain, monkeypatch):
    class _TwoArgError(Exception):
        def __init__(self, first, second) -> None:
            super().__init__(first, second)

    secret = "glpat-" + "SECRET"
    monkeypatch.setattr(logging_setup, "_SECRETS", (secret,))
    try:
        raise _TwoArgError(secret, "x")
    except _TwoArgError:
        loguru_logger.exception("failed")

    out = capture_text_plain[0]
    assert secret not in out
    # 两个必填参数的异常无法用 `type(value)(msg)` 重建，但类型仍保留
    assert "_TwoArgError" in out


def test_json_filter_merges_context():
    lines: list[str] = []
    handler_id = loguru_logger.add(
        lambda message: lines.append(str(message).rstrip("\n")),
        level="INFO",
        format=logging_setup._escaped_json_template(JsonFormatter()),
        filter=logging_setup._json_filter,
    )
    try:
        bind_context(run_id="r-json", event="review.end")
        loguru_logger.info("done")
    finally:
        loguru_logger.remove(handler_id)
    data = json.loads(lines[-1])
    assert data["run_id"] == "r-json"
    assert data["event"] == "review.end"
    assert data["message"] == "done"


def test_json_filter_includes_redacted_exception():
    lines: list[str] = []
    formatter = logging_setup._escaped_json_template(JsonFormatter(secrets={"glpat-SECRET"}))
    handler_id = loguru_logger.add(
        lambda message: lines.append(str(message).rstrip("\n")),
        level="INFO",
        format=formatter,
        filter=logging_setup._json_filter,
    )
    try:
        logging_setup._SECRETS = ("glpat-SECRET",)
        try:
            raise RuntimeError("boom glpat-SECRET")
        except RuntimeError:
            loguru_logger.exception("failed")
    finally:
        loguru_logger.remove(handler_id)
    raw = lines[-1]
    assert "glpat-SECRET" not in raw
    data = json.loads(raw)
    assert data["error_type"] == "RuntimeError"
    assert data["message"] == "failed"
    assert "RuntimeError" in data["traceback"]
    assert "\n" not in raw


# ---------------------------------------------------------------- context


def test_context_thread_isolation():
    bind_context(run_id="main")
    seen: list[dict] = []

    def worker() -> None:
        bind_context(run_id="t1")
        seen.append(current_context())

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    assert seen == [{"run_id": "t1"}]
    assert current_context() == {"run_id": "main"}


def test_bind_context_merges_and_ignores_none():
    bind_context(run_id="r1", stage=None, attempt=1)
    bind_context(attempt=2, profile="python-ssd")
    assert current_context() == {"run_id": "r1", "attempt": 2, "profile": "python-ssd"}
    snapshot = current_context()
    snapshot["run_id"] = "mutated"
    assert current_context()["run_id"] == "r1"


# ---------------------------------------------------------------- setup_logging


class _FakeLogger:
    def __init__(self) -> None:
        self.removed = 0
        self.sinks: list[dict] = []
        self.warnings: list[str] = []

    def remove(self, *_args, **_kwargs) -> None:
        self.removed += 1

    def add(self, sink, **kwargs) -> int:
        self.sinks.append({"sink": sink, **kwargs})
        return len(self.sinks)

    def warning(self, message, *args, **_kwargs) -> None:
        self.warnings.append(message.format(*args))


@pytest.fixture
def fake_logger(monkeypatch, tmp_path):
    fake = _FakeLogger()
    monkeypatch.setattr(logging_setup, "logger", fake)
    monkeypatch.setattr(logging_setup, "config", _FakeConfig(tmp_path), raising=False)
    return fake


class _FakeConfig:
    def __init__(self, data_dir) -> None:
        self.data_dir = data_dir
        self.log_level = "INFO"
        self.log_format = "text"
        self.log_rotation = "single"

    # secret 字段故意为空：置空时不应被当作 secret
    gitlab_pat = ""
    gitlab_webhook_secret = ""
    qoder_sdk_pat = ""
    opencode_password = ""


def test_setup_logging_configures_two_sinks(fake_logger):
    setup_logging()
    assert fake_logger.removed == 1
    assert len(fake_logger.sinks) == 2
    stderr_sink, file_sink = fake_logger.sinks
    assert stderr_sink["sink"] is sys.stderr
    assert file_sink["sink"].endswith("reviewagent.log")
    assert stderr_sink["level"] == "INFO"
    assert stderr_sink["diagnose"] is False
    assert file_sink["diagnose"] is False
    assert file_sink["enqueue"] is True
    assert file_sink["backtrace"] is True
    assert file_sink["rotation"] == "50 MB"
    assert file_sink["retention"] == "30 days"
    assert file_sink["compression"] == "gz"
    assert file_sink["encoding"] == "utf-8"
    assert stderr_sink["format"] == TEXT_FORMAT
    # 两个 sink 都挂了 filter（上下文合入 / 脱敏）
    assert stderr_sink["filter"] is logging_setup._text_filter
    assert file_sink["filter"] is logging_setup._text_filter


def test_setup_logging_json_format_callable(fake_logger):
    setup_logging(fmt="json")
    for sink in fake_logger.sinks:
        assert callable(sink["format"])
        assert sink["diagnose"] is False
        assert sink["filter"] is logging_setup._json_filter
    record = {"time": "t", "level": {"name": "INFO"}, "message": "m", "extra": {}}
    template = fake_logger.sinks[0]["format"](record)
    # loguru callable format 返回的是模板；JSON 行放在 extra 里，由 format_map 代入.
    assert template == "{extra[_json_line]}\n"
    assert json.loads(record["extra"]["_json_line"])["message"] == "m"


def test_setup_logging_rotation_none(fake_logger, monkeypatch):
    cfg = _FakeConfig(logging_setup.config.data_dir)
    cfg.log_rotation = "none"
    monkeypatch.setattr(logging_setup, "config", cfg)
    setup_logging()
    file_sink = fake_logger.sinks[1]
    assert file_sink["rotation"] is None
    assert file_sink["retention"] is None
    assert file_sink["compression"] is None


def test_setup_logging_idempotent(fake_logger):
    setup_logging()
    setup_logging()
    assert fake_logger.removed == 2
    assert len(fake_logger.sinks) == 4


def test_setup_logging_invalid_format_falls_back_to_text(fake_logger):
    setup_logging(fmt="yaml")
    assert fake_logger.sinks[0]["format"] == TEXT_FORMAT
    assert fake_logger.warnings == ["logging.format_invalid fmt=yaml fallback=text"]


def test_setup_logging_uses_config_log_format(fake_logger, monkeypatch):
    cfg = _FakeConfig(logging_setup.config.data_dir)
    cfg.log_format = "json"
    monkeypatch.setattr(logging_setup, "config", cfg)
    setup_logging()
    assert callable(fake_logger.sinks[0]["format"])
    assert fake_logger.sinks[0]["filter"] is logging_setup._json_filter


def test_setup_logging_never_leaks_secret_values(fake_logger, monkeypatch):
    cfg = _FakeConfig(logging_setup.config.data_dir)
    cfg.gitlab_pat = "glpat-SECRET"
    cfg.qoder_sdk_pat = "sdk-SECRET"
    monkeypatch.setattr(logging_setup, "config", cfg)
    setup_logging(fmt="json")
    formatter = fake_logger.sinks[0]["format"]
    record = {"time": "t", "level": "INFO", "message": "pat glpat-SECRET", "extra": {}}
    formatter(record)
    line = record["extra"]["_json_line"]
    assert "glpat-SECRET" not in line
    assert json.loads(line)["message"] == "pat ***"
    assert logging_setup._SECRETS == ("glpat-SECRET", "sdk-SECRET")
