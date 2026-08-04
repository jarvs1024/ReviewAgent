"""Config ACP field wiring — defaults + env override."""

from __future__ import annotations

import pytest

from reviewagent.config import Config


def _make_config(monkeypatch, **env):
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
    return Config.from_env()


def test_qodercli_driver_default(monkeypatch):
    monkeypatch.delenv("QODERCLI_DRIVER", raising=False)
    cfg = _make_config(monkeypatch)
    # 2026-08-04: ACP driver hangs on stdin (run 577 验证), 默认改为 subprocess
    assert cfg.qodercli_driver == "subprocess"


def test_qodercli_driver_override(monkeypatch):
    cfg = _make_config(monkeypatch, QODERCLI_DRIVER="subprocess")
    assert cfg.qodercli_driver == "subprocess"


def test_opencode_model_defaults_to_deepseek_v4_flash(monkeypatch):
    monkeypatch.delenv("OPENCODE_MODEL", raising=False)
    cfg = _make_config(monkeypatch)
    assert cfg.opencode_model == "deepseek/deepseek-v4-flash"


def test_qodercli_max_concurrent_sessions_default(monkeypatch):
    monkeypatch.delenv("QODERCLI_MAX_CONCURRENT_SESSIONS", raising=False)
    cfg = _make_config(monkeypatch)
    assert cfg.qodercli_max_concurrent_sessions == 4


def test_qodercli_max_concurrent_sessions_override(monkeypatch):
    cfg = _make_config(monkeypatch, QODERCLI_MAX_CONCURRENT_SESSIONS=2)
    assert cfg.qodercli_max_concurrent_sessions == 2


def test_qodercli_acp_extra_args_splits_on_whitespace(monkeypatch):
    cfg = _make_config(monkeypatch, QODERCLI_ACP_EXTRA_ARGS="--foo bar --baz")
    assert cfg.qodercli_acp_extra_args == ["--foo", "bar", "--baz"]


@pytest.mark.parametrize("env_key,attr,default", [
    ("QODERCLI_QUEUE_WAIT_TIMEOUT", "qodercli_queue_wait_timeout", 120),
    ("QODERCLI_SESSION_REUSE_WINDOW", "qodercli_session_reuse_window", 300),
    ("QODERCLI_SESSION_TIMEOUT", "qodercli_session_timeout", 540),
])
def test_timeout_defaults_and_override(monkeypatch, env_key, attr, default):
    monkeypatch.delenv(env_key, raising=False)
    cfg = _make_config(monkeypatch)
    assert getattr(cfg, attr) == default
    cfg2 = _make_config(monkeypatch, **{env_key: 7})
    assert getattr(cfg2, attr) == 7
