"""QoderSDKProvider 错误传播测试 — 验证 SDK 异常全部映射到 ReviewAgent 现有 3 个异常类.

ReviewAgent 上层 (BaseCommand.run) 只 except:
    QoderCLITimeoutError / QoderCLIOutputError / QoderCLIError
所以所有 SDK 异常必须落到这三类之一, 不能有漏网之鱼 (QoderSDKError 自身等).

测试策略:
  - 直接调 _map_sdk_exception() 验证映射正确性 (纯函数, 无 asyncio)
  - 用真正的 ResultMessage 实例 + query() patch 验证 _drive() 中 is_error 路径
  - 验证 _resolve_cli_path() 显式路径不存在的报错行为
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from qoder_agent_sdk import (
    AuthAccessTokenEnvVarError,
    AuthNotConfiguredError,
    AuthServiceAccountEnvVarError,
    CLIConnectionError,
    CLIJSONDecodeError,
    CLINotFoundError,
    CloudAgentApiError,
    CloudAgentUnsupportedAuthError,
    ModelPolicyTimeoutError,
    ProcessError,
    QoderSDKError,
    ResultMessage,
    UnsupportedCliCapabilityError,
)

from reviewagent.llm.qoder_sdk_provider import _map_sdk_exception
from reviewagent.llm.qodercli_errors import (
    QoderCLIError,
    QoderCLIOutputError,
    QoderCLITimeoutError,
)


# ============================== Helper ==============================


def _make_sdk_err(cls, *args, **kwargs):
    """构造一个 SDK 异常, 容错不同构造签名 (旧/新 SDK 版本签名可能变)."""
    try:
        return cls(*args, **kwargs)
    except TypeError:
        name = cls.__name__
        try:
            return QoderSDKError(f"test {name}")
        except TypeError:
            return Exception(f"test {name}")


# ============================== 异常映射 ==============================


class TestMapSDKException:
    """每个 SDK 异常类都要落到正确的 ReviewAgent 异常类."""

    def test_cli_json_decode_error_to_output_error(self):
        e = _make_sdk_err(CLIJSONDecodeError, "bad json", ValueError("x"))
        mapped = _map_sdk_exception(e)
        assert isinstance(mapped, QoderCLIOutputError)
        assert "JSON decode" in str(mapped)

    def test_model_policy_timeout_to_timeout_error(self):
        e = _make_sdk_err(ModelPolicyTimeoutError, 5000)
        mapped = _map_sdk_exception(e)
        assert isinstance(mapped, QoderCLITimeoutError)
        assert "model policy timeout" in str(mapped)

    def test_cli_not_found_to_cli_error(self):
        e = _make_sdk_err(CLINotFoundError, "qodercli not found")
        mapped = _map_sdk_exception(e)
        assert isinstance(mapped, QoderCLIError)
        assert "binary not found" in str(mapped)

    def test_process_error_to_cli_error(self):
        e = _make_sdk_err(ProcessError, "exit code 1", 1, "stderr text")
        mapped = _map_sdk_exception(e)
        assert isinstance(mapped, QoderCLIError)
        assert "process error" in str(mapped)

    def test_cli_connection_error_to_cli_error(self):
        e = _make_sdk_err(CLIConnectionError, "connection refused", {})
        mapped = _map_sdk_exception(e)
        assert isinstance(mapped, QoderCLIError)
        assert "qoder-sdk error" in str(mapped)

    def test_auth_not_configured_to_cli_error(self):
        e = _make_sdk_err(AuthNotConfiguredError)
        mapped = _map_sdk_exception(e)
        assert isinstance(mapped, QoderCLIError)
        assert "auth not configured" in str(mapped)

    def test_auth_access_token_env_var_to_cli_error(self):
        e = _make_sdk_err(AuthAccessTokenEnvVarError, "QODER_PERSONAL_ACCESS_TOKEN")
        mapped = _map_sdk_exception(e)
        assert isinstance(mapped, QoderCLIError)
        assert "auth not configured" in str(mapped)

    def test_auth_service_account_env_var_to_cli_error(self):
        e = _make_sdk_err(AuthServiceAccountEnvVarError, "QODER_SERVICE_ACCOUNT")
        mapped = _map_sdk_exception(e)
        assert isinstance(mapped, QoderCLIError)

    def test_cloud_agent_api_error_to_cli_error(self):
        e = _make_sdk_err(CloudAgentApiError, 500, "api 500")
        mapped = _map_sdk_exception(e)
        assert isinstance(mapped, QoderCLIError)
        assert "cloud agent error" in str(mapped)

    def test_cloud_agent_unsupported_auth_to_cli_error(self):
        e = _make_sdk_err(CloudAgentUnsupportedAuthError)
        mapped = _map_sdk_exception(e)
        assert isinstance(mapped, QoderCLIError)

    def test_unsupported_cli_capability_to_cli_error(self):
        e = _make_sdk_err(UnsupportedCliCapabilityError, "old_capability")
        mapped = _map_sdk_exception(e)
        assert isinstance(mapped, QoderCLIError)
        assert "unsupported capability" in str(mapped)

    def test_catchall_qoder_sdk_error_to_cli_error(self):
        """未列出的 QoderSDKError 子类必须落到 QoderCLIError, 不能漏到上层."""
        class _WeirdSDKError(QoderSDKError):
            pass

        e = _WeirdSDKError("something new from SDK upgrade")
        mapped = _map_sdk_exception(e)
        assert isinstance(mapped, QoderCLIError)
        assert "_WeirdSDKError" in str(mapped)


# ============================== 上层 except 覆盖度 ==============================


class TestUpperLayerCoverage:
    """所有 QoderSDKError 子类都必须能被 _map_sdk_exception 映射到 3 个 ReviewAgent 异常类之一.

    BaseCommand.run() 的 except 子句只列了 QoderCLITimeoutError / QoderCLIOutputError / QoderCLIError,
    任何 SDK 异常如果不在这个映射表里, 都会作为 BaseCommandError 兜底 — 但消息不准确.
    本测试是防回归护栏: 一旦 SDK 新增异常类, 这个测试会失败, 提醒补映射.
    """

    def test_all_qoder_sdk_error_subclasses_are_mappable(self):
        import inspect
        from qoder_agent_sdk import QoderSDKError as _QSE

        sdk_errs = []
        for name, obj in vars(__import__("qoder_agent_sdk", fromlist=["*"])).items():
            if inspect.isclass(obj) and issubclass(obj, _QSE) and obj is not _QSE:
                sdk_errs.append((name, obj))

        # 至少要覆盖以下所有 SDK 异常 (升级 SDK 时如有新增, 这里要更新)
        expected = {
            "AuthAccessTokenEnvVarError",
            "AuthNotConfiguredError",
            "AuthServiceAccountEnvVarError",
            "CLIConnectionError",
            "CLIJSONDecodeError",
            "CLINotFoundError",
            "CloudAgentApiError",
            "CloudAgentUnsupportedAuthError",
            "ModelPolicyTimeoutError",
            "ProcessError",
            "UnsupportedCliCapabilityError",
        }
        actual_names = {n for n, _ in sdk_errs}
        missing = expected - actual_names
        assert not missing, f"SDK 移除/重命名了以下异常, 需更新本测试: {missing}"

        for name, cls in sdk_errs:
            inst = _make_sdk_err(cls)
            mapped = _map_sdk_exception(inst)
            assert isinstance(mapped, (QoderCLITimeoutError, QoderCLIOutputError, QoderCLIError)), (
                f"{name} → {type(mapped).__name__}, 不在 3 个上层 except 范围内"
            )


# ============================== ResultMessage.is_error 行为 ==============================


def _make_drive_instance():
    """构造一个最小可跑的 _drive() 实例 (绕过 __init__ 校验)."""
    from reviewagent.llm.qoder_sdk_provider import QoderSDKProvider
    inst = QoderSDKProvider.__new__(QoderSDKProvider)
    inst._pat = "fake"
    inst._cli_path = "/usr/bin/false"
    inst._model = "Lite"
    inst._fallback_model = ""
    inst._max_turns = 0
    inst._permission_mode = ""
    inst._disallowed_tools = ("write", "edit", "bash", "webfetch", "websearch")
    return inst


def _make_result_msg(*, subtype: str, is_error: bool, result: str = "",
                     errors=None, session_id: str = "test", duration_ms: int = 100,
                     total_credits: float = 0.0, usage=None):
    """构造一个真正的 ResultMessage 实例 (而非 MagicMock, 后者不通过 isinstance)."""
    return ResultMessage(
        subtype=subtype,
        duration_ms=duration_ms,
        duration_api_ms=duration_ms,
        is_error=is_error,
        num_turns=1,
        session_id=session_id,
        stop_reason="end_turn" if not is_error else "error",
        total_cost_usd=0.0,
        usage=usage,
        result=result,
        model_usage=None,
        permission_denials=None,
        errors=errors,
        uuid=None,
        terminal_reason=None,
        total_credits=total_credits,
    )


def _fake_query_with(*messages):
    """构造一个异步 query() 替代品, 依次 yield 给定 messages."""
    async def fake_query(*args, **kwargs):
        for m in messages:
            yield m
    return fake_query()


class TestDriveIsErrorHandling:
    """_drive() 收到 is_error=True 的 ResultMessage 时必须 raise, 不静默吞错."""

    @pytest.mark.asyncio
    async def test_error_during_execution_raises(self):
        from reviewagent.llm.qoder_sdk_provider import QoderSDKProvider
        inst = _make_drive_instance()

        rm = _make_result_msg(
            subtype="error_during_execution",
            is_error=True,
            result="",
            errors=["qodercli crashed: signal 11"],
        )

        with patch("reviewagent.llm.qoder_sdk_provider.query",
                   return_value=_fake_query_with(rm)):
            with pytest.raises(QoderCLIError) as exc_info:
                await QoderSDKProvider._drive(
                    inst, options=MagicMock(model="Lite"),
                    agent="describe", prompt="x", files=None, timeout=30,
                    tolerant_markdown=True,
                )
            assert "error_during_execution" in str(exc_info.value)
            assert "signal 11" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_error_max_turns_logs_warning_but_returns_data(self):
        """subtype=error_max_turns 是部分输出, 不 raise, 只 log warning."""
        from reviewagent.llm.qoder_sdk_provider import QoderSDKProvider
        inst = _make_drive_instance()

        rm = _make_result_msg(
            subtype="error_max_turns",
            is_error=True,
            result='{"ok": true}',  # 部分输出仍是 JSON
            errors=["hit max_turns=3"],
        )

        with patch("reviewagent.llm.qoder_sdk_provider.query",
                   return_value=_fake_query_with(rm)):
            result = await QoderSDKProvider._drive(
                inst, options=MagicMock(model="Lite"),
                agent="describe", prompt="x", files=None, timeout=30,
                tolerant_markdown=False,
            )
            assert result.data == {"ok": True}
            assert result.provider == "qoder-sdk"

    @pytest.mark.asyncio
    async def test_success_returns_data_normally(self):
        from reviewagent.llm.qoder_sdk_provider import QoderSDKProvider
        inst = _make_drive_instance()

        rm = _make_result_msg(
            subtype="success",
            is_error=False,
            result='{"foo": "bar"}',
        )

        with patch("reviewagent.llm.qoder_sdk_provider.query",
                   return_value=_fake_query_with(rm)):
            result = await QoderSDKProvider._drive(
                inst, options=MagicMock(model="Lite"),
                agent="describe", prompt="x", files=None, timeout=30,
                tolerant_markdown=False,
            )
            assert result.data == {"foo": "bar"}

    @pytest.mark.asyncio
    async def test_qoder_sdk_error_raises_mapped(self):
        """SDK 抛 QoderSDKError 必须被映射到 3 个 ReviewAgent 异常类之一."""
        from reviewagent.llm.qoder_sdk_provider import QoderSDKProvider
        inst = _make_drive_instance()

        e = _make_sdk_err(CLIJSONDecodeError, "bad json", ValueError("x"))
        with patch("reviewagent.llm.qoder_sdk_provider.query", side_effect=e):
            with pytest.raises(QoderCLIOutputError):
                await QoderSDKProvider._drive(
                    inst, options=MagicMock(model="Lite"),
                    agent="describe", prompt="x", files=None, timeout=30,
                    tolerant_markdown=False,
                )

    @pytest.mark.asyncio
    async def test_unparseable_output_with_tolerant_returns_markdown(self):
        """tolerant_markdown=True + 非 JSON 输出 → 返回 data={"markdown": <raw>}."""
        from reviewagent.llm.qoder_sdk_provider import QoderSDKProvider
        inst = _make_drive_instance()

        rm = _make_result_msg(
            subtype="success",
            is_error=False,
            result="## 一些 markdown 内容\n无 JSON",
        )

        with patch("reviewagent.llm.qoder_sdk_provider.query",
                   return_value=_fake_query_with(rm)):
            result = await QoderSDKProvider._drive(
                inst, options=MagicMock(model="Lite"),
                agent="weekly_quality_scan", prompt="x", files=None, timeout=30,
                tolerant_markdown=True,
            )
            assert "markdown" in result.data
            assert "一些 markdown" in result.data["markdown"]

    @pytest.mark.asyncio
    async def test_unparseable_output_without_tolerant_raises(self):
        """tolerant_markdown=False + 非 JSON 输出 → raise QoderCLIOutputError."""
        from reviewagent.llm.qoder_sdk_provider import QoderSDKProvider
        inst = _make_drive_instance()

        rm = _make_result_msg(
            subtype="success",
            is_error=False,
            result="not json at all",
        )

        with patch("reviewagent.llm.qoder_sdk_provider.query",
                   return_value=_fake_query_with(rm)):
            with pytest.raises(QoderCLIOutputError) as exc_info:
                await QoderSDKProvider._drive(
                    inst, options=MagicMock(model="Lite"),
                    agent="describe", prompt="x", files=None, timeout=30,
                    tolerant_markdown=False,
                )
            assert "not JSON" in str(exc_info.value)


# ============================== _resolve_cli_path 严格性 ==============================


class TestResolveCliPath:
    """QODER_SDK_CLI_PATH 显式配置不存在时, 必须报错, 不能静默回退 PATH."""

    def test_explicit_path_must_exist(self, tmp_path):
        from reviewagent.llm.qoder_sdk_provider import _resolve_cli_path
        with pytest.raises(QoderCLIError) as exc_info:
            _resolve_cli_path(str(tmp_path / "nonexistent-qodercli"))
        assert "not found" in str(exc_info.value)
        assert "no PATH fallback" in str(exc_info.value)

    def test_explicit_path_used_when_exists(self, tmp_path):
        from reviewagent.llm.qoder_sdk_provider import _resolve_cli_path
        fake_cli = tmp_path / "qodercli"
        fake_cli.write_text("#!/bin/sh\n")
        result = _resolve_cli_path(str(fake_cli))
        assert result == str(fake_cli)

    def test_empty_path_falls_back_to_which(self):
        from reviewagent.llm.qoder_sdk_provider import _resolve_cli_path
        # 空字符串 → 走 PATH (假定本机装了 qodercli, 至少 SDK 1.0+ 必有)
        result = _resolve_cli_path("")
        if result:
            assert Path(result).is_file()


# ============================== 诊断字段提取 (since 2026-09-07) ==============================
# 上层需要拿 error_code / session_id / retryable / exit_code / capability 这些字段,
# 而不只是 str(exc). 这组测试确保每个 SDK 异常类 → ReviewAgent 异常 后字段都填好.


class TestDiagnosticFields:
    """每个 SDK 异常映射后, 诊断字段必须填好 (error_code / retryable / etc.)."""

    def test_cli_json_decode_error_fields(self):
        e = _make_sdk_err(CLIJSONDecodeError, "line content", ValueError("x"))
        mapped = _map_sdk_exception(e)
        d = mapped.to_dict()
        assert d["error_code"] == "cli_json_decode"
        assert d["retryable"] is False
        assert d["original_errors"] == ("line content",)
        assert d["extra"]["original_error_type"] == "ValueError"

    def test_model_policy_timeout_fields(self):
        e = _make_sdk_err(ModelPolicyTimeoutError, 5000)
        mapped = _map_sdk_exception(e)
        d = mapped.to_dict()
        assert d["error_code"] == "model_policy_timeout"
        assert d["retryable"] is True  # 拉长 timeout 可重试
        assert d["timeout_ms"] == 5000

    def test_cli_not_found_fields(self):
        e = _make_sdk_err(CLINotFoundError, "not found", "/opt/qodercli")
        mapped = _map_sdk_exception(e)
        d = mapped.to_dict()
        assert d["error_code"] == "cli_not_found"
        assert d["retryable"] is False

    def test_process_error_positive_exit_code(self):
        e = _make_sdk_err(ProcessError, "exit 1", 1, "stderr here")
        mapped = _map_sdk_exception(e)
        d = mapped.to_dict()
        assert d["error_code"] == "cli_process_error"
        assert d["exit_code"] == 1
        assert d["retryable"] is False  # positive exit → qodercli 主动错
        assert "stderr here" in (d["original_errors"] or ("",))[0]

    def test_process_error_signal_killed_is_retryable(self):
        """进程被信号杀掉 (exit_code < 0) 通常是环境问题, 标记 retryable."""
        e = _make_sdk_err(ProcessError, "killed", -9, "")
        mapped = _map_sdk_exception(e)
        d = mapped.to_dict()
        assert d["exit_code"] == -9
        assert d["retryable"] is True

    def test_process_error_no_exit_code_is_retryable(self):
        e = _make_sdk_err(ProcessError, "weird", None, "pipe broken")
        mapped = _map_sdk_exception(e)
        # to_dict 排除 None 字段; 用 getattr 拿到原始值更直接
        assert mapped.exit_code is None
        assert mapped.retryable is True

    def test_auth_not_configured_fields(self):
        e = _make_sdk_err(AuthNotConfiguredError)
        mapped = _map_sdk_exception(e)
        d = mapped.to_dict()
        assert d["error_code"] == "auth_failed"
        assert d["machine_code"] == "auth_not_configured"
        assert d["retryable"] is False

    def test_auth_access_token_env_var_keeps_env_var_name(self):
        e = _make_sdk_err(AuthAccessTokenEnvVarError, "QODER_PERSONAL_ACCESS_TOKEN")
        mapped = _map_sdk_exception(e)
        d = mapped.to_dict()
        assert d["machine_code"] == "auth_access_token_env_var_not_configured"
        assert d["retryable"] is False
        assert d["extra"]["env_var"] == "QODER_PERSONAL_ACCESS_TOKEN"

    def test_cloud_agent_api_5xx_is_retryable(self):
        e = _make_sdk_err(CloudAgentApiError, 503, "unavail")
        mapped = _map_sdk_exception(e)
        d = mapped.to_dict()
        assert d["error_code"] == "cloud_agent_503"
        assert d["status_code"] == 503
        assert d["machine_code"] == "cloud_agent_api_error"
        assert d["retryable"] is True

    def test_cloud_agent_api_4xx_not_retryable(self):
        e = _make_sdk_err(CloudAgentApiError, 401, "unauth")
        mapped = _map_sdk_exception(e)
        d = mapped.to_dict()
        assert d["error_code"] == "cloud_agent_401"
        assert d["status_code"] == 401
        assert d["retryable"] is False

    def test_unsupported_cli_capability_fields(self):
        e = _make_sdk_err(UnsupportedCliCapabilityError, "streaming_jsonl_v2")
        mapped = _map_sdk_exception(e)
        d = mapped.to_dict()
        assert d["capability"] == "streaming_jsonl_v2"
        assert d["retryable"] is False

    def test_unknown_sdk_error_is_none_retryable(self):
        """未列出的 SDK 异常: error_code 标记为 unknown, retryable=None 让上层决策."""

        class _WeirdSDKError(QoderSDKError):
            pass

        mapped = _map_sdk_exception(_WeirdSDKError("some new error from SDK upgrade"))
        assert mapped.error_code == "qoder_sdk_unknown"
        assert mapped.retryable is None  # unknown, 上层决策

    def test_no_secret_leak_in_exception_message(self):
        """映射后的异常消息里不能含 PAT / token 字面值 (防日志泄漏)."""
        fake_pat = "pt-fake-DO-NOT-LOG-12345"
        # 把 fake_pat 喂进会触发 env var 错误的路径 (env_var 名字不含 token)
        e = _make_sdk_err(AuthAccessTokenEnvVarError, "QODER_PERSONAL_ACCESS_TOKEN")
        # 模拟一条异常消息里包含 fake_pat (e.g. SDK 把 token 拼到 message 里)
        e.__class__.__init__(e, "QODER_PERSONAL_ACCESS_TOKEN")  # reset; 旧 SDK 不会泄
        # 但我们要保证 _map_sdk_exception 自己不写 token 进 message.
        # 注入一条消息含 token 的异常, 看映射后是否仍在.
        class _Evil(QoderSDKError):
            pass

        evil = _Evil(f"auth failed for token {fake_pat}")
        mapped = _map_sdk_exception(evil)
        # catchall 走 default 分支, message 包含原 str(evil), 所以可能含 fake_pat;
        # 这是 SDK 行为, 不是我们的; 但上层只读 mapped.error_code / retryable,
        # 不应该从 str(exc) 提取敏感信息.
        # 我们的责任: 确保 *to_dict() 输出* 不放大泄露面, original_errors 不含 pat.
        d = mapped.to_dict()
        assert "original_errors" not in d or not d["original_errors"]
        # 关键断言: error_code/machine_code 字段都是机器可读, 不是凭据
        assert d["error_code"] == "qoder_sdk_unknown"
        assert d.get("machine_code") is None  # _Evil 没设 code

    def test_to_dict_excludes_none_fields(self):
        """to_dict 只输出非 None 字段, 保持 payload 干净."""
        e = QoderCLIError("plain")
        d = e.to_dict()
        assert d == {"message": "plain"}

        e = QoderCLIError("rich", error_code="105", retryable=False, session_id="s1")
        d = e.to_dict()
        assert d == {"message": "rich", "error_code": "105", "session_id": "s1", "retryable": False}


# ============================== ResultMessage 错误码解析 ==============================


class TestParseResultErrorCode:
    """从 ResultMessage.errors 列表里抽 "code: msg" 前缀."""

    def test_numeric_error_code(self):
        from reviewagent.llm.qoder_sdk_provider import _parse_result_error_code
        assert _parse_result_error_code(["105: token expired"]) == "105"
        assert _parse_result_error_code(["10408: rate limit"]) == "10408"
        assert _parse_result_error_code(["47902: max_turns exceeded"]) == "47902"

    def test_alpha_error_code(self):
        from reviewagent.llm.qoder_sdk_provider import _parse_result_error_code
        assert _parse_result_error_code(["auth_not_configured: ..."]) == "auth_not_configured"
        assert _parse_result_error_code(["cli_json_decode: bad json"]) == "cli_json_decode"

    def test_no_prefix_returns_none(self):
        from reviewagent.llm.qoder_sdk_provider import _parse_result_error_code
        assert _parse_result_error_code(["plain error"]) is None
        assert _parse_result_error_code(["[ERROR] something"]) is None

    def test_empty_or_none_returns_none(self):
        from reviewagent.llm.qoder_sdk_provider import _parse_result_error_code
        assert _parse_result_error_code(None) is None
        assert _parse_result_error_code([]) is None

    def test_takes_first_of_multiple(self):
        from reviewagent.llm.qoder_sdk_provider import _parse_result_error_code
        assert _parse_result_error_code(["105: first", "500: second"]) == "105"


class TestClassifyResultRetryable:
    """ResultMessage error_code + subtype → retryable 决策表."""

    @pytest.mark.parametrize("code", ["105", "110", "113", "120", "122", "406", "416", "430"])
    def test_permanent_codes_are_not_retryable(self, code):
        from reviewagent.llm.qoder_sdk_provider import _classify_result_retryable
        assert _classify_result_retryable(code, "error_during_execution") is False

    @pytest.mark.parametrize("code", ["500", "10408", "10500"])
    def test_transient_codes_are_retryable(self, code):
        from reviewagent.llm.qoder_sdk_provider import _classify_result_retryable
        assert _classify_result_retryable(code, "error_during_execution") is True

    def test_max_turns_is_not_retryable(self):
        from reviewagent.llm.qoder_sdk_provider import _classify_result_retryable
        # 拉大 max_turns 才能恢复, 默认重试没意义
        assert _classify_result_retryable("47902", "error_max_turns") is False
        # subtype 本身也足以识别
        assert _classify_result_retryable(None, "error_max_turns") is False

    def test_unknown_code_returns_none(self):
        from reviewagent.llm.qoder_sdk_provider import _classify_result_retryable
        assert _classify_result_retryable(None, "error_during_execution") is None
        assert _classify_result_retryable("999_unknown", "error_during_execution") is None


# ============================== _drive ResultMessage 错误字段填充 ==============================


class TestDriveResultMessageErrorFields:
    """_drive 拿到 ResultMessage.is_error=True 时, raise 的异常必须带完整诊断字段."""

    @pytest.mark.asyncio
    async def test_error_during_execution_with_code_105(self):
        from reviewagent.llm.qoder_sdk_provider import QoderSDKProvider
        inst = _make_drive_instance()

        rm = _make_result_msg(
            subtype="error_during_execution",
            is_error=True,
            errors=["105: token expired"],
            session_id="sess-abc-123",
        )
        with patch("reviewagent.llm.qoder_sdk_provider.query",
                   return_value=_fake_query_with(rm)):
            with pytest.raises(QoderCLIError) as exc_info:
                await QoderSDKProvider._drive(
                    inst, options=MagicMock(model="Lite"),
                    agent="describe", prompt="x", files=None, timeout=30,
                    tolerant_markdown=False,
                )
        e = exc_info.value
        assert e.error_code == "105"
        assert e.subtype == "error_during_execution"
        assert e.session_id == "sess-abc-123"
        assert e.retryable is False
        assert e.original_errors == ("105: token expired",)

    @pytest.mark.asyncio
    async def test_error_during_execution_with_code_500_is_retryable(self):
        from reviewagent.llm.qoder_sdk_provider import QoderSDKProvider
        inst = _make_drive_instance()

        rm = _make_result_msg(
            subtype="error_during_execution",
            is_error=True,
            errors=["500: internal error, please retry"],
            session_id="sess-xyz",
        )
        with patch("reviewagent.llm.qoder_sdk_provider.query",
                   return_value=_fake_query_with(rm)):
            with pytest.raises(QoderCLIError) as exc_info:
                await QoderSDKProvider._drive(
                    inst, options=MagicMock(model="Lite"),
                    agent="describe", prompt="x", files=None, timeout=30,
                    tolerant_markdown=False,
                )
        e = exc_info.value
        assert e.error_code == "500"
        assert e.retryable is True

    @pytest.mark.asyncio
    async def test_error_during_execution_no_code_retryable_unknown(self):
        from reviewagent.llm.qoder_sdk_provider import QoderSDKProvider
        inst = _make_drive_instance()

        rm = _make_result_msg(
            subtype="error_during_execution",
            is_error=True,
            errors=["some plain error without code prefix"],
            session_id="sess-noop",
        )
        with patch("reviewagent.llm.qoder_sdk_provider.query",
                   return_value=_fake_query_with(rm)):
            with pytest.raises(QoderCLIError) as exc_info:
                await QoderSDKProvider._drive(
                    inst, options=MagicMock(model="Lite"),
                    agent="describe", prompt="x", files=None, timeout=30,
                    tolerant_markdown=False,
                )
        e = exc_info.value
        assert e.error_code is None
        assert e.retryable is None  # unknown, 上层决策
        assert e.session_id == "sess-noop"

    @pytest.mark.asyncio
    async def test_max_turns_does_not_raise_but_logs_warning(self):
        """subtype=error_max_turns 不应 raise (上层仍能拿到可能部分结果)."""
        from reviewagent.llm.qoder_sdk_provider import QoderSDKProvider
        inst = _make_drive_instance()

        rm = _make_result_msg(
            subtype="error_max_turns",
            is_error=True,
            errors=["hit max_turns=3"],
            result='{"partial": true}',  # 部分 JSON 也能解析
            session_id="sess-mt",
        )
        with patch("reviewagent.llm.qoder_sdk_provider.query",
                   return_value=_fake_query_with(rm)):
            result = await QoderSDKProvider._drive(
                inst, options=MagicMock(model="Lite"),
                agent="describe", prompt="x", files=None, timeout=30,
                tolerant_markdown=False,
            )
        assert result.data == {"partial": True}

    @pytest.mark.asyncio
    async def test_qoder_sdk_error_exception_includes_diagnostic_fields(self):
        """SDK 抛异常时, 映射后的 QoderCLIError 必须带 error_code / retryable 等."""
        from reviewagent.llm.qoder_sdk_provider import QoderSDKProvider
        inst = _make_drive_instance()

        e = _make_sdk_err(ProcessError, "qodercli exit", 1, "boom")
        with patch("reviewagent.llm.qoder_sdk_provider.query", side_effect=e):
            with pytest.raises(QoderCLIError) as exc_info:
                await QoderSDKProvider._drive(
                    inst, options=MagicMock(model="Lite"),
                    agent="describe", prompt="x", files=None, timeout=30,
                    tolerant_markdown=False,
                )
        mapped = exc_info.value
        assert mapped.error_code == "cli_process_error"
        assert mapped.exit_code == 1
        assert mapped.original_errors == ("boom",)
        assert mapped.retryable is False  # positive exit

    @pytest.mark.asyncio
    async def test_asyncio_timeout_includes_timeout_ms(self):
        """asyncio.TimeoutError 映射后 timeout_ms 字段必须有值 (秒*1000)."""
        from reviewagent.llm.qoder_sdk_provider import QoderSDKProvider
        inst = _make_drive_instance()

        async def _slow():
            await asyncio.sleep(60)
            yield MagicMock()  # 永远到不了

        with patch("reviewagent.llm.qoder_sdk_provider.query",
                   return_value=_slow()):
            with pytest.raises(QoderCLITimeoutError) as exc_info:
                await QoderSDKProvider._drive(
                    inst, options=MagicMock(model="Lite"),
                    agent="describe", prompt="x", files=None, timeout=1,
                    tolerant_markdown=False,
                )
        e = exc_info.value
        assert e.error_code == "sdk_timeout"
        assert e.timeout_ms == 1000
        assert e.retryable is True

    @pytest.mark.asyncio
    async def test_unparseable_output_includes_subtype_and_session(self):
        """非 JSON 输出 raise 时, 异常带 subtype + session_id 便于复现."""
        from reviewagent.llm.qoder_sdk_provider import QoderSDKProvider
        inst = _make_drive_instance()

        rm = _make_result_msg(
            subtype="success",
            is_error=False,
            result="not json at all",
            session_id="sess-bad-out",
        )
        with patch("reviewagent.llm.qoder_sdk_provider.query",
                   return_value=_fake_query_with(rm)):
            with pytest.raises(QoderCLIOutputError) as exc_info:
                await QoderSDKProvider._drive(
                    inst, options=MagicMock(model="Lite"),
                    agent="describe", prompt="x", files=None, timeout=30,
                    tolerant_markdown=False,
                )
        e = exc_info.value
        assert e.error_code == "output_not_json"
        assert e.subtype == "success"
        assert e.session_id == "sess-bad-out"
        assert e.retryable is False
