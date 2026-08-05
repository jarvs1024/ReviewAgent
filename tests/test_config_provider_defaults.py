"""Provider config defaults — guards against silent regressions."""

from __future__ import annotations

from reviewagent.config import Config


def _make_config(monkeypatch, **env):
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
    return Config.from_env()


def test_opencode_model_defaults_to_deepseek_v4_flash(monkeypatch):
    monkeypatch.delenv("OPENCODE_MODEL", raising=False)
    cfg = _make_config(monkeypatch)
    assert cfg.opencode_model == "deepseek/deepseek-v4-flash"


def test_qodercli_model_defaults_to_deepseek_v4_flash(monkeypatch):
    """Subprocess driver uses DeepSeek-V4-Flash by default."""
    monkeypatch.delenv("QODERCLI_MODEL", raising=False)
    cfg = _make_config(monkeypatch)
    assert cfg.qodercli_model == "DeepSeek-V4-Flash"
