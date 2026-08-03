"""LLM 适配层单元测试 — OpencodeProvider wrapper / factory / QoderCLI stub.

设计:
    - OpencodeProvider 通过注入 OpencodeClient mock,无需真实 opencode daemon
    - factory 用 monkeypatch 改 config.llm_provider 后 reset_client()
    - QoderCLIProvider 暂时是 stub,只验证 raise NotImplementedError
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from reviewagent.llm import (
    BaseLLMProvider,
    LLMResult,
    OpencodeError,
    OpencodeOutputError,
    OpencodeTimeoutError,
    get_client,
    reset_client,
)
from reviewagent.llm.opencode_provider import OpencodeProvider
from reviewagent.llm.qodercli_provider import (
    QoderCLIProvider,
    QoderCLIError,
    QoderCLITimeoutError,
    QoderCLIOutputError,
)
from reviewagent.opencode.client import OpencodeResult


# ============================== LLMResult dataclass ==============================

class TestLLMResult:
    def test_minimal_required(self):
        r = LLMResult(data={"k": "v"})
        assert r.data == {"k": "v"}
        assert r.prompt_tokens == 0
        assert r.completion_tokens == 0
        assert r.model == ""
        assert r.duration_ms == 0
        assert r.provider == ""
        assert r.raw_output == ""

    def test_all_fields(self):
        r = LLMResult(
            data={"x": 1},
            prompt_tokens=10,
            completion_tokens=20,
            model="deepseek/deepseek-v4-flash",
            duration_ms=1234,
            provider="opencode",
            raw_output="<raw>",
        )
        assert r.prompt_tokens == 10
        assert r.model == "deepseek/deepseek-v4-flash"
        assert r.provider == "opencode"


# ============================== OpencodeProvider wrapper ==============================

class TestOpencodeProvider:
    def _make(self, mock_result_or_exception):
        """构造 OpencodeProvider: 注入 mock client,run() 返回 mock_result_or_exception."""
        mock_client = MagicMock()
        if isinstance(mock_result_or_exception, Exception):
            mock_client.run.side_effect = mock_result_or_exception
        else:
            mock_client.run.return_value = mock_result_or_exception
        return OpencodeProvider(client=mock_client), mock_client

    def test_run_translates_result(self):
        oc_result = OpencodeResult(
            data={"summary_md": "ok", "suggestions": []},
            prompt_tokens=100,
            completion_tokens=20,
            model="deepseek/deepseek-v4-flash",
        )
        provider, mock_client = self._make(oc_result)
        result = provider.run(
            agent="code-improver",
            prompt="review this diff",
            workdir=Path("/tmp/wt"),
            files=[],
            timeout=60,
            tolerant_markdown=False,
        )
        # OpencodeClient.run 被调用 1 次,参数全透传
        mock_client.run.assert_called_once_with(
            agent="code-improver",
            prompt="review this diff",
            workdir=Path("/tmp/wt"),
            files=[],
            timeout=60,
            tolerant_markdown=False,
        )
        # LLMResult 字段正确转换
        assert isinstance(result, LLMResult)
        assert result.data == {"summary_md": "ok", "suggestions": []}
        assert result.prompt_tokens == 100
        assert result.completion_tokens == 20
        assert result.model == "deepseek/deepseek-v4-flash"
        assert result.provider == "opencode"
        assert result.duration_ms >= 0  # 至少非负,真实值依赖时钟
        assert result.raw_output == ""  # OpencodeClient 不暴露原文

    def test_run_passes_through_opencode_error(self):
        provider, _ = self._make(OpencodeError("upstream down"))
        with pytest.raises(OpencodeError, match="upstream down"):
            provider.run(agent="x", prompt="p", workdir=Path("/tmp"))

    def test_run_passes_through_output_error(self):
        provider, _ = self._make(OpencodeOutputError("bad json"))
        with pytest.raises(OpencodeOutputError, match="bad json"):
            provider.run(agent="x", prompt="p", workdir=Path("/tmp"))

    def test_run_passes_through_timeout_error(self):
        provider, _ = self._make(OpencodeTimeoutError("600s"))
        with pytest.raises(OpencodeTimeoutError, match="600s"):
            provider.run(agent="x", prompt="p", workdir=Path("/tmp"))

    def test_health_check_returns_bool(self):
        provider, _ = self._make(OpencodeResult(data={}))
        with patch("httpx.Client") as MockClient:
            cm = MagicMock()
            cm.__enter__.return_value.get.return_value = MagicMock(status_code=200)
            MockClient.return_value = cm
            assert provider.health_check() is True

    def test_health_check_false_on_exception(self):
        provider, _ = self._make(OpencodeResult(data={}))
        with patch("httpx.Client", side_effect=RuntimeError("connection refused")):
            assert provider.health_check() is False

    def test_provider_name(self):
        provider, _ = self._make(OpencodeResult(data={}))
        assert provider.provider_name == "opencode"

    def test_is_base_provider(self):
        provider, _ = self._make(OpencodeResult(data={}))
        assert isinstance(provider, BaseLLMProvider)


# ============================== QoderCLIProvider (subprocess mock) ==============================

# 模拟 -o json 顶层响应（含 result 字段二次 parse 字符串）
_QODERCLI_OK_TOP = json.dumps({
    "type": "result", "subtype": "success",
    "result": json.dumps({"summary_md": "ok", "suggestions": []}, ensure_ascii=False),
    "stop_reason": "end_turn",
    "duration_ms": 3700, "total_cost_usd": 0, "num_turns": 1,
    "usage": {"input_tokens": 100, "output_tokens": 20, "context_usage_ratio": 0.023},
    "modelUsage": {"dfmodel": {"inputTokens": 100, "outputTokens": 20}},
    "modelID": "DeepSeek-V4-Flash",
    "providerID": "dfmodel",
    "session_id": "sess_test",
})

_QODERCLI_TRUNCATED_TOP = json.dumps({
    "type": "result", "subtype": "success",
    "result": json.dumps({"partial": True}),
    "stop_reason": "max_tokens",
    "duration_ms": 1200,
    "usage": {"input_tokens": 50, "output_tokens": 200, "context_usage_ratio": 0.95},
    "modelID": "DeepSeek-V4-Flash",
})

_QODERCLI_RESULT_NOT_JSON_TOP = json.dumps({
    "type": "result", "subtype": "success",
    "result": "这是普通 markdown 文本，没有 JSON",
    "stop_reason": "end_turn",
    "duration_ms": 1000,
    "usage": {"input_tokens": 10, "output_tokens": 5, "context_usage_ratio": 0.01},
    "modelID": "DeepSeek-V4-Flash",
})


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestQoderCLIProvider:
    """subprocess 通过 patch 注入 fake，避免真调 qodercli。"""

    def _make(self, completed: _FakeCompleted):
        return QoderCLIProvider(
            node_path="/usr/bin/node",
            js_path="/fake/qodercli.js",
            model="DeepSeek-V4-Flash",
        ), completed

    def _patch_run(self, monkeypatch, completed):
        calls = {"args": None, "kwargs": None}

        def fake_run(*args, **kwargs):
            calls["args"] = args
            calls["kwargs"] = kwargs
            return completed

        monkeypatch.setattr("subprocess.run", fake_run)
        return calls

    def test_run_success(self, monkeypatch):
        completed = _FakeCompleted(returncode=0, stdout=_QODERCLI_OK_TOP)
        provider, _ = self._make(completed)
        calls = self._patch_run(monkeypatch, completed)

        result = provider.run(
            agent="improve",
            prompt="review this",
            workdir=Path("/tmp/wt"),
            files=[],
            timeout=60,
            tolerant_markdown=False,
        )
        # subprocess.run 被调一次
        assert calls["args"] is not None
        cmd = calls["args"][0]
        # cmd 关键 flag 校验
        assert cmd[0] == "/usr/bin/node"
        assert cmd[1] == "/fake/qodercli.js"
        assert "-p" in cmd
        assert "--model" in cmd and "DeepSeek-V4-Flash" in cmd
        assert "--no-session-persistence" in cmd
        assert "-o" in cmd and "json" in cmd
        assert "-w" in cmd and "/tmp/wt" in cmd
        assert "--append-system-prompt" in cmd
        # append-system-prompt 后面是 agent body (本测试用的是 improve.md 正文, 不是短字符串)
        assert cmd[-1] == "review this"  # prompt 是最后一个位置参数

        # LLMResult 字段
        assert isinstance(result, LLMResult)
        assert result.data == {"summary_md": "ok", "suggestions": []}
        assert result.prompt_tokens == 100
        assert result.completion_tokens == 20
        assert result.model == "DeepSeek-V4-Flash"
        assert result.provider == "qodercli"
        assert result.duration_ms >= 0
        assert "summary_md" in result.raw_output

    def test_run_with_disallowed_tools(self, monkeypatch):
        completed = _FakeCompleted(returncode=0, stdout=_QODERCLI_OK_TOP)
        provider, _ = self._make(completed)
        calls = self._patch_run(monkeypatch, completed)
        provider.run(agent="improve", prompt="p", workdir=Path("/tmp"), files=[], timeout=60)
        cmd = calls["args"][0]
        # improve.md 的 tools_disabled 是 [write, edit, bash, webfetch]
        idx = cmd.index("--disallowed-tools")
        disabled = cmd[idx + 1]
        assert "write" in disabled
        assert "edit" in disabled
        assert "bash" in disabled

    def test_run_with_attachment(self, monkeypatch, tmp_path):
        completed = _FakeCompleted(returncode=0, stdout=_QODERCLI_OK_TOP)
        provider, _ = self._make(completed)
        # 准备一个临时 diff 文件
        diff_file = tmp_path / "diff.patch"
        diff_file.write_text("diff --git a/foo.py\n+new line", encoding="utf-8")

        calls = self._patch_run(monkeypatch, completed)
        provider.run(
            agent="improve", prompt="p",
            workdir=tmp_path, files=[diff_file], timeout=60,
        )
        cmd = calls["args"][0]
        # --attachment 应该出现，且指向 workdir 下的临时文件
        assert "--attachment" in cmd
        idx = cmd.index("--attachment")
        attach_path = Path(cmd[idx + 1])
        # 调用结束后临时文件应被清理（finally 块 unlink）
        assert not attach_path.exists(), "attachment 临时文件应被清理"

    def test_run_timeout(self, monkeypatch):
        provider, _ = self._make(_FakeCompleted())
        def fake_run(*_a, **_k):
            raise subprocess.TimeoutExpired(cmd=["node"], timeout=10)
        monkeypatch.setattr("subprocess.run", fake_run)
        with pytest.raises(QoderCLITimeoutError, match="timeout after 10s"):
            provider.run(agent="improve", prompt="p", workdir=Path("/tmp"), timeout=10)

    def test_run_nonzero_exit(self, monkeypatch):
        provider, _ = self._make(_FakeCompleted(returncode=1, stderr="boom"))
        self._patch_run(monkeypatch, _FakeCompleted(returncode=1, stderr="boom"))
        with pytest.raises(QoderCLIError, match="exit=1"):
            provider.run(agent="improve", prompt="p", workdir=Path("/tmp"))

    def test_run_truncated_warns_but_returns(self, monkeypatch):
        completed = _FakeCompleted(returncode=0, stdout=_QODERCLI_TRUNCATED_TOP)
        provider, _ = self._make(completed)
        self._patch_run(monkeypatch, completed)

        # loguru 不走标准 logging — spy logger.warning 看是否被调用
        warnings: list[tuple] = []
        def fake_warning(*args, **kwargs):
            warnings.append((args, kwargs))
        monkeypatch.setattr(
            "reviewagent.llm.qodercli_provider.logger.warning", fake_warning,
        )

        result = provider.run(agent="improve", prompt="p", workdir=Path("/tmp"))
        assert result.data == {"partial": True}
        flat = str(warnings)  # tuple of (args, kwargs) — 模糊匹配 max_tokens
        assert "max_tokens" in flat, f"expected warn with max_tokens; got {warnings}"

    def test_run_tolerant_markdown_fallback(self, monkeypatch):
        completed = _FakeCompleted(returncode=0, stdout=_QODERCLI_RESULT_NOT_JSON_TOP)
        provider, _ = self._make(completed)
        self._patch_run(monkeypatch, completed)
        result = provider.run(
            agent="improve", prompt="p",
            workdir=Path("/tmp"), tolerant_markdown=True,
        )
        assert "markdown" in result.data
        assert "普通 markdown 文本" in result.data["markdown"]

    def test_run_non_json_without_tolerant_raises(self, monkeypatch):
        completed = _FakeCompleted(returncode=0, stdout=_QODERCLI_RESULT_NOT_JSON_TOP)
        provider, _ = self._make(completed)
        self._patch_run(monkeypatch, completed)
        with pytest.raises(QoderCLIOutputError, match="result not JSON"):
            provider.run(
                agent="improve", prompt="p",
                workdir=Path("/tmp"), tolerant_markdown=False,
            )

    def test_provider_name(self):
        provider, _ = self._make(_FakeCompleted())
        assert provider.provider_name == "qodercli"

    def test_is_base_provider(self):
        provider, _ = self._make(_FakeCompleted())
        assert isinstance(provider, BaseLLMProvider)

    def test_health_check_ok(self, monkeypatch):
        provider, _ = self._make(_FakeCompleted())
        # 两次 subprocess.run 都返回 0
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **k: _FakeCompleted(returncode=0, stdout="v22.0.0\nv1.1.12\n"),
        )
        assert provider.health_check() is True

    def test_health_check_false_on_failure(self, monkeypatch):
        provider, _ = self._make(_FakeCompleted())
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **k: _FakeCompleted(returncode=1, stderr="not found"),
        )
        assert provider.health_check() is False

    def test_health_check_false_on_timeout(self, monkeypatch):
        provider, _ = self._make(_FakeCompleted())
        def boom(*_a, **_k):
            raise subprocess.TimeoutExpired(cmd=["node"], timeout=5)
        monkeypatch.setattr("subprocess.run", boom)
        assert provider.health_check() is False


# ============================== get_client() factory ==============================

@pytest.fixture(autouse=True)
def _clean_singleton():
    """每个 case 前清空单例 + 还原 env."""
    reset_client()
    yield
    reset_client()
    # 还原 env（避免污染其他测试）
    for k in ("LLM_PROVIDER",):
        os.environ.pop(k, None)


class TestGetClient:
    def _mock_config(self, monkeypatch, llm_provider: str):
        # config 是 frozen dataclass,在模块 import 时 from_env() 已确定.
        # 测试里直接替换 reviewagent.llm.client 模块引用的 config 对象.
        fake_cfg = SimpleNamespace(llm_provider=llm_provider)
        monkeypatch.setattr("reviewagent.llm.client.config", fake_cfg)

    def test_default_returns_opencode(self, monkeypatch):
        self._mock_config(monkeypatch, "opencode")
        client = get_client()
        assert isinstance(client, OpencodeProvider)
        assert client.provider_name == "opencode"

    def test_singleton(self, monkeypatch):
        self._mock_config(monkeypatch, "opencode")
        a = get_client()
        b = get_client()
        assert a is b

    def test_unknown_provider_raises(self, monkeypatch):
        self._mock_config(monkeypatch, "weird-thing")
        with pytest.raises(ValueError, match="unknown LLM_PROVIDER"):
            get_client()

    def test_qodercli_returns_real_provider(self, monkeypatch):
        # QoderCLIProvider 已完整实现，get_client() 直接返回实例.
        self._mock_config(monkeypatch, "qodercli")
        client = get_client()
        assert isinstance(client, QoderCLIProvider)
        assert client.provider_name == "qodercli"

    def test_reset_client_clears_cache(self, monkeypatch):
        self._mock_config(monkeypatch, "opencode")
        a = get_client()
        reset_client()
        b = get_client()
        # reset 后 _client 是 None,会重新构造一个新实例（OpencodeProvider 内部复用全局单例
        # 但 OpencodeProvider 实例本身是新的）
        assert a is not b
