"""日志脱敏安全用例：真实凭据不得落盘（text / json sink 双路径）.

镜像 T17 的脱敏修复（Authorization 整段吞掉 scheme、显式 secret、短值词边界），
用临时 sink 端到端验证：两种格式的输出都不含原文；text 输出不出现裸换行，
避免伪造日志行（日志注入）。
"""
from __future__ import annotations

import json

import pytest
from loguru import logger as loguru_logger

import reviewagent.logging_setup as logging_setup
from reviewagent.logging_setup import TEXT_FORMAT, JsonFormatter

SECRET = "glpat-SECRET"
MESSAGE = f"curl -H 'Authorization: Bearer xyz' with {SECRET}\n2026-11-07 00:00:00 | ERROR | fake:fn:1 - forged"


@pytest.fixture(autouse=True)
def _restore_secrets():
    saved = logging_setup._SECRETS
    yield
    logging_setup._SECRETS = saved
    logging_setup.clear_context()


@pytest.fixture
def text_sink():
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
def json_sink():
    lines: list[str] = []
    handler_id = loguru_logger.add(
        lambda message: lines.append(str(message).rstrip("\n")),
        level="INFO",
        format=logging_setup._escaped_json_template(JsonFormatter(secrets=(SECRET,))),
        filter=logging_setup._json_filter,
    )
    try:
        yield lines
    finally:
        loguru_logger.remove(handler_id)


def test_text_sink_never_leaks_credentials(text_sink):
    logging_setup._SECRETS = (SECRET,)
    loguru_logger.info(MESSAGE)

    assert len(text_sink) == 1
    out = text_sink[0]
    assert SECRET not in out
    assert "xyz" not in out
    # 一条记录一行：裸换行（含伪造的 ERROR 行）被转义
    assert "\n" not in out
    assert "\\n2026-11-07 00:00:00 | ERROR | fake:fn:1 - forged" in out
    assert "Authorization: ***" in out


def test_json_sink_never_leaks_credentials(json_sink):
    logging_setup._SECRETS = (SECRET,)
    loguru_logger.info(MESSAGE)

    assert len(json_sink) == 1
    out = json_sink[0]
    assert SECRET not in out
    assert "xyz" not in out
    assert "\n" not in out
    data = json.loads(out)
    assert data["message"].startswith("curl -H 'Authorization: ***")
    assert SECRET not in data["message"]
