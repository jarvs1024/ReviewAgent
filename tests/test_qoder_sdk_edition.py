"""QODER_SDK_EDITION 国际/国内版切换测试.

两包 (qoder_agent_sdk / qodercn_agent_sdk) 顶层模块名不同但 API 同名，
load_sdk() 按配置选择；本文件验证选择逻辑与未知值报错。
"""
from __future__ import annotations

import pytest

from reviewagent.llm.qoder_sdk_provider import (
    _EDITIONS,
    _brand_cli_commands,
    _resolve_cli_path,
    load_sdk,
)
from reviewagent.llm.qodercli_errors import QoderCLIError


def test_editions_registry():
    assert _EDITIONS == {"cn": "qodercn_agent_sdk", "intl": "qoder_agent_sdk"}


def test_load_sdk_intl():
    mod = load_sdk("intl")
    assert mod.__name__ == "qoder_agent_sdk"
    for attr in ("query", "QoderAgentOptions", "access_token", "QoderSDKError"):
        assert hasattr(mod, attr)


def _try_import_cn():
    try:
        import importlib
        return importlib.import_module("qodercn_agent_sdk")
    except ImportError:
        return None


@pytest.mark.skipif(_try_import_cn() is None, reason="qodercn-agent-sdk 未安装")
def test_load_sdk_cn():
    mod = load_sdk("cn")
    assert mod.__name__ == "qodercn_agent_sdk"
    assert mod._brand.SDK_BRAND == "cn"


def test_load_sdk_case_insensitive_and_strip():
    mod = load_sdk(" INTL ")
    assert mod.__name__ == "qoder_agent_sdk"


def test_load_sdk_unknown_edition():
    with pytest.raises(QoderCLIError, match="unknown QODER_SDK_EDITION"):
        load_sdk("bogus")
    with pytest.raises(QoderCLIError):
        load_sdk("")


def test_brand_cli_commands():
    cmds = _brand_cli_commands()
    assert cmds and all(isinstance(c, str) for c in cmds)


def test_resolve_cli_path_explicit_missing_raises(tmp_path):
    with pytest.raises(QoderCLIError, match="no PATH fallback"):
        _resolve_cli_path(str(tmp_path / "no-such-cli"))


def test_resolve_cli_path_path_lookup(tmp_path, monkeypatch):
    fake = tmp_path / "qoderclicn"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert _resolve_cli_path("", ("qoderclicn",)) == str(fake)
    assert _resolve_cli_path("", ("missing-one", "qoderclicn")) == str(fake)
    assert _resolve_cli_path("", ("definitely-missing",)) == ""
