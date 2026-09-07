"""QoderSDKProvider — Qoder Agent SDK (qoder-agent-sdk) 的 ReviewAgent 适配层.

替代旧 ``QoderCLIProvider`` 的 subprocess 模式：通过 ``qoder-agent-sdk`` Python
API (``query()`` + ``QoderAgentOptions``) 调用本地 qodercli CLI，但由 SDK 负责
进程管理 / 协议解析 / 流式事件分发，上层只需消费 ``LLMResult``。

调用形态：单次 ``query(prompt=..., options=QoderAgentOptions(...))`` 短生命周期
（每次调用一个 event loop）。这样：
  - 绕开历史教训（2026-08-05 qodercli --acp 长连接 stdin hang 7+ 分钟无响应）；
  - 沿用并发模型：RQ worker 是同步的，每次 ``asyncio.run()`` 开合 event loop；
  - 不强求 session 续传，ReviewAgent 现有 5 个 agent 都是单轮无状态。

异常映射：上层代码（commands / reporting）用 ``except QoderCLIError`` 之类的名字，
所以 SDK 异常 ``QoderSDKError`` / ``CLIJSONDecodeError`` / ``ProcessError`` 都映射到
``QoderCLIError`` / ``QoderCLIOutputError`` / ``QoderCLITimeoutError``，上层零改动。

Auth：``QODER_SDK_PAT``（Qoder Personal Access Token），由 ``qodercli login`` 迁出，
不依赖本机 ``~/.qoder`` 目录登录态。

调试模型：默认 ``Lite``，与原 subprocess 模式 ``QODERCLI_MODEL=Lite`` 保持一致。
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
from pathlib import Path
from typing import Any

from qoder_agent_sdk import (
    QoderAgentOptions,
    QoderSDKError,
    ResultMessage,
    TextBlock,
    access_token,
    query,
)

from reviewagent.config import config
from reviewagent.llm.base import LLMResult, _strip_fence
from reviewagent.llm.qodercli_errors import (
    QoderCLIError,
    QoderCLIOutputError,
    QoderCLITimeoutError,
)
from reviewagent.logging_setup import logger
from reviewagent.prompts import loader


# 默认模型；与 .env.example QODERCLI_MODEL=Lite 保持一致，调试用 Lite。
DEFAULT_MODEL = "Lite"


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _resolve_cli_path(cli_path: str) -> str:
    """解析 qodercli 可执行文件路径.

    优先级:
      1. 如果 QODER_SDK_CLI_PATH 非空 → 严格用该路径, 不存在则 raise (不静默 fallback)
      2. 如果 QODER_SDK_CLI_PATH 为空 → shutil.which("qodercli")
      3. 都找不到 → 返回空, __init__ 负责报错

    为什么严格: 如果用户显式配了某个路径 (比如 staging 上是 /opt/qodercli/build/qodercli),
    但临时挂掉或路径写错, 应该立即报错, 而不是静默回退到 PATH 上的另一个版本.
    """
    if cli_path:
        if not Path(cli_path).is_file():
            raise QoderCLIError(
                f"QODER_SDK_CLI_PATH={cli_path!r} not found; "
                f"explicit path must exist (no PATH fallback)"
            )
        return cli_path
    found = shutil.which("qodercli")
    return found or ""


def _build_options(
    *,
    agent: str,
    workdir: Path,
    pat: str,
    cli_path: str,
    model: str,
    max_turns: int,
    permission_mode: str,
    disallowed_tools: tuple[str, ...],
    add_dirs: list[Path] | None = None,
) -> QoderAgentOptions:
    """根据 agent prompt + config 构造 ``QoderAgentOptions``.

    system_prompt 直接传 Markdown 文本（SDK 不解析 frontmatter，由 ``loader.load``
    过滤后的 ``prompt`` 字段喂入）；上层维持 ``loader.load(agent)['prompt']`` 单源.
    """
    meta = loader.load(agent)
    system_prompt = meta["prompt"]

    add_dirs_resolved = list(add_dirs) if add_dirs else []
    if workdir not in add_dirs_resolved:
        add_dirs_resolved.append(workdir)

    kwargs: dict[str, Any] = {
        "model": model,
        "cwd": str(workdir),
        "add_dirs": [str(p) for p in add_dirs_resolved],
        "system_prompt": system_prompt,
        "disallowed_tools": list(disallowed_tools),
        "auth": access_token(token=pat),
    }
    if cli_path:
        kwargs["cli_path"] = cli_path
    if max_turns and max_turns > 0:
        kwargs["max_turns"] = max_turns
    if permission_mode:
        kwargs["permission_mode"] = permission_mode

    return QoderAgentOptions(**kwargs)


# ---------------------------------------------------------------------------
# JSON 解析：与旧 qodercli_subprocess 一致的容错策略
# ---------------------------------------------------------------------------
# SDK 的 ``ResultMessage.result`` 是模型最后一条文本。LLM 仍可能输出：
#   - markdown ```json ... ``` 围栏
#   - 前置/后置 prose
#   - 嵌套 ``{"markdown": "..."}`` 包装（周报降级形态）
# 这些情况与旧 subprocess 完全相同，复用同一套 fallback。

_LINE_NUMBER_PREFIX = re.compile(r"^\s*\d+\|\s?")


def _strip_line_number_prefix(text: str) -> str:
    """剥离偶发 stdout 行号前缀 (e.g. ``15| def foo():``).

    与 qodercli_subprocess._strip_line_number_prefix 等价；保守启发式：
    至少 50% 行 + >=2 行匹配才执行剥离，避免误删 JSON 里行首数字字段.
    """
    if not text:
        return text
    lines = text.split("\n")
    matched = sum(1 for ln in lines if _LINE_NUMBER_PREFIX.match(ln))
    if matched < 2 or matched < len(lines) * 0.5:
        return text
    stripped = "\n".join(_LINE_NUMBER_PREFIX.sub("", ln) for ln in lines)
    first_brace = stripped.find("{")
    if first_brace <= 0:
        return stripped
    pre = stripped[:first_brace]
    if not pre.strip():
        return stripped
    return stripped[first_brace:]


def _unwrap_markdown_wrapper(text: str) -> str:
    """嗅探并剥掉字面 ``{"markdown": "<json_string>"}`` 包装.

    LLM 偶尔把整段 JSON 字符串塞进 markdown wrapper 的 value 字段.
    这种情况下 caller 拿到 ``{"markdown": "..."}`` 字面字符串是误读.
    """
    text = text.strip()
    if not text.startswith("{"):
        return text
    try:
        top = json.loads(text)
    except json.JSONDecodeError:
        return text
    if (
        isinstance(top, dict)
        and set(top.keys()) == {"markdown"}
        and isinstance(top["markdown"], str)
    ):
        return top["markdown"]
    return text


def _extract_inner_json(text: str) -> dict[str, Any]:
    """解析 LLM 输出的 JSON dict，多策略 fallback.

    策略（与 qodercli_subprocess._extract_inner_json 对齐）:
        1. ``json.JSONDecoder(strict=False).raw_decode(text)`` — 处理 trailing prose
           + 字符串值内字面控制字符.
        2. 找首个 ``{`` 重试 — 处理 leading prose.
        3. 整文档 ``json.loads(strict=False)`` — 二次兜底.

    返回 dict；解析失败抛 ``json.JSONDecodeError``.
    """
    if not text:
        raise json.JSONDecodeError("empty text", text or "", 0)
    decoder = json.JSONDecoder(strict=False)
    for candidate in (text, text[text.find("{"):] if "{" in text else ""):
        if not candidate:
            continue
        try:
            obj, _end = decoder.raw_decode(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    try:
        obj = json.loads(text, strict=False)
    except json.JSONDecodeError:
        pass
    raise json.JSONDecodeError("no JSON object found", text, 0)


def _parse_result_text(text: str) -> dict[str, Any]:
    """从 ``ResultMessage.result`` 字符串中抽出 JSON dict.

    Pipeline: 行号前缀剥离 → fence 剥离 → inner JSON 抽取.
    """
    text = _strip_line_number_prefix(text)
    text = _strip_fence(text)
    text = _unwrap_markdown_wrapper(text)
    return _extract_inner_json(text)


# ---------------------------------------------------------------------------
# 异常映射
# ---------------------------------------------------------------------------


def _map_sdk_exception(e: QoderSDKError) -> QoderCLIError:
    """把 SDK 异常映射到 ReviewAgent 现有异常类（保持上层 except 不变）.

    映射表（同时填诊断字段 error_code / machine_code / session_id / etc.）:
      - CLIJSONDecodeError           → QoderCLIOutputError (stdout 不是 JSON)
      - ModelPolicyTimeoutError      → QoderCLITimeoutError (模型策略 callback 超时)
      - CLINotFoundError             → QoderCLIError (qodercli 找不到；建议重装)
      - ProcessError                 → QoderCLIError (qodercli 进程异常)
      - CLIConnectionError           → QoderCLIError (连接失败)
      - AuthNotConfiguredError /
        AuthAccessTokenEnvVarError /
        AuthServiceAccountEnvVarError → QoderCLIError (鉴权失败, retryable=False)
      - CloudAgentApiError /
        CloudAgentUnsupportedAuthError → QoderCLIError (云端 agent 错误)
      - UnsupportedCliCapabilityError → QoderCLIError (qodercli 版本不支持)
      - QoderSDKError (catchall)     → QoderCLIError

    详细子类信息（错误码 / CLI 退出码 / stderr 摘要）会附在异常消息 + 字段里，
    便于上层 (BaseCommand.run / logger / Sentry) 直接用, 不必再 str(exc).
    """
    from qoder_agent_sdk import (  # local import
        CLIJSONDecodeError, CLINotFoundError, ProcessError,
        ModelPolicyTimeoutError,
        AuthNotConfiguredError, AuthAccessTokenEnvVarError, AuthServiceAccountEnvVarError,
        CloudAgentApiError, CloudAgentUnsupportedAuthError,
        UnsupportedCliCapabilityError,
    )
    # MessageParseError 未导出到顶层, 从 _errors 直接拿
    from qoder_agent_sdk._errors import MessageParseError

    # SDK 异常类的 .code 属性 (e.g. "auth_not_configured")
    sdk_machine_code = getattr(e, "code", None)

    if isinstance(e, CLIJSONDecodeError):
        return QoderCLIOutputError(
            f"qodercli JSON decode error: {e}",
            error_code="cli_json_decode",
            machine_code=sdk_machine_code,
            retryable=False,
            original_errors=(getattr(e, "line", "")[:500],) if getattr(e, "line", None) else None,
            extra={"original_error_type": type(getattr(e, "original_error", None)).__name__}
                  if getattr(e, "original_error", None) else None,
        )
    if isinstance(e, ModelPolicyTimeoutError):
        timeout_ms = getattr(e, "timeout_ms", None)
        return QoderCLITimeoutError(
            f"qoder-sdk model policy timeout: {e}",
            error_code="model_policy_timeout",
            machine_code=sdk_machine_code,
            timeout_ms=timeout_ms,
            retryable=True,  # 拉长 resolve_model_timeout_ms 后可重试
        )
    if isinstance(e, CLINotFoundError):
        return QoderCLIError(
            f"qodercli binary not found (set QODER_SDK_CLI_PATH or install qodercli): {e}",
            error_code="cli_not_found",
            machine_code=sdk_machine_code,
            retryable=False,  # 需要用户手动装 / 改路径
        )
    if isinstance(e, ProcessError):
        exit_code = getattr(e, "exit_code", None)
        stderr_text = getattr(e, "stderr", None)
        return QoderCLIError(
            f"qodercli process error: {e}",
            error_code="cli_process_error",
            machine_code=sdk_machine_code,
            exit_code=exit_code,
            # retryable 语义:
            #   exit_code is None  → 子进程根本没起来, 通常是环境瞬时问题, 重试可恢复
            #   exit_code < 0      → 被信号杀掉 (e.g. -9=OOM/超时, -15=shutdown), 重试可恢复
            #   exit_code >= 1     → qodercli 主动返回错误码 (auth/policy/quota/...),
            #                          具体要看 stderr / error_code; 这里保守设为不重试,
            #                          上层按 error_code 决定更准确
            retryable=exit_code is None or exit_code < 0,
            original_errors=(stderr_text[:1000],) if stderr_text else None,
        )
    if isinstance(e, (AuthNotConfiguredError, AuthAccessTokenEnvVarError, AuthServiceAccountEnvVarError)):
        # 鉴权失败: 不应 retry, 需要用户修 token / env var
        env_var = getattr(e, "env_var", None)
        return QoderCLIError(
            f"qoder-sdk auth not configured / invalid: {e}",
            error_code="auth_failed",
            machine_code=sdk_machine_code,
            retryable=False,
            extra={"env_var": env_var} if env_var else None,
        )
    if isinstance(e, CloudAgentApiError):
        status = getattr(e, "status", None)
        return QoderCLIError(
            f"qoder-sdk cloud agent error: {e}",
            error_code=f"cloud_agent_{status}" if status else "cloud_agent_error",
            machine_code=sdk_machine_code,
            status_code=status,
            retryable=bool(status and status >= 500),  # 5xx 重试, 4xx 不重试
        )
    if isinstance(e, CloudAgentUnsupportedAuthError):
        return QoderCLIError(
            f"qoder-sdk cloud agent auth not supported: {e}",
            error_code="cloud_agent_auth_unsupported",
            machine_code=sdk_machine_code,
            retryable=False,
        )
    if isinstance(e, UnsupportedCliCapabilityError):
        capability = getattr(e, "capability", None)
        return QoderCLIError(
            f"qodercli version too old / unsupported capability: {e}",
            error_code="unsupported_cli_capability",
            machine_code=sdk_machine_code,
            capability=capability,
            retryable=False,  # 升级 qodercli 后才行
        )
    if isinstance(e, MessageParseError):
        return QoderCLIOutputError(
            f"qoder-sdk message parse error: {e}",
            error_code="message_parse_error",
            machine_code=sdk_machine_code,
            retryable=False,
            extra={"data_keys": list((getattr(e, "data", None) or {}).keys())[:10]}
                  if getattr(e, "data", None) else None,
        )
    # catchall: 其余 QoderSDKError 子类 (含 SDK 升级后新增类)
    return QoderCLIError(
        f"qoder-sdk error [{type(e).__name__}]: {e}",
        error_code="qoder_sdk_unknown",
        machine_code=sdk_machine_code,
        retryable=None,  # unknown, 让上层决策
    )



# ---------------------------------------------------------------------------
# ResultMessage 错误码 / retryable 分类 (qodercli wire protocol 层)
# ---------------------------------------------------------------------------
# 区别于 _map_sdk_exception (SDK 异常类 → ReviewAgent 异常):
# 这里处理 ResultMessage 里 is_error=True 的情况 — qodercli 已经跑完了,
# 但带着 error 状态返回. 错误码来自 ResultMessage.errors 列表 (字符串),
# 形如 "105: token expired" / "auth_not_configured: ..." / "..." 等.

# 错误码分类表 (来自 qodercli errors 文档):
#   105          → 鉴权失败 / token 过期 (permanent)
#   110, 113-122 → 余额 / 配额 / 计费 (permanent)
#   406, 416, 430 → 请求被拒 (policy, permanent)
#   500, 10408, 10500 → 服务暂时不可用 (retryable)
#   47902        → 超过 max_turns (partial, special — log warning, 仍 try 解析)
#   100400-100403 → 自定义模型相关 (permanent)

# 用纯字符串比较: error_code 是从 errors[0] 解析出来的机器码, 可能带也可能不带数字前缀
_PERMANENT_ERROR_CODES: frozenset[str] = frozenset({
    # 鉴权
    "105", "auth_invalid_token", "auth_not_configured",
    "auth_access_token_env_var_not_configured",
    "auth_service_account_env_var_not_configured",
    # 余额 / 配额 / 计费
    "110", "113", "114", "115", "116", "117", "118", "119", "120", "121", "122",
    # 请求被拒 (policy)
    "406", "416", "430",
    # 自定义模型相关
    "100400", "100401", "100402", "100403",
})

_RETRYABLE_ERROR_CODES: frozenset[str] = frozenset({
    "500", "10408", "10500",
})

_MAX_TURNS_ERROR_CODE = "47902"


def _parse_result_error_code(errors: list[str] | None) -> str | None:
    """从 ResultMessage.errors 里抽第一个 ``"code: msg"`` 前缀的 code.

    例: ["105: token expired", "..."]  →  "105"
        ["auth_not_configured: ..."]    →  "auth_not_configured"
        ["some plain error"]             →  None (无前缀)

    与 SDK ``_normalize_control_error`` 逻辑保持一致:
    ``re.match(r"^([a-z][a-z0-9_]*):\\s*(.*)$", message)``.
    这里同时识别数字开头的错误码 (e.g. "105: ...").
    """
    if not errors:
        return None
    first = errors[0]
    if not isinstance(first, str):
        return None
    # 数字错误码优先 (例如 105, 10408, 47902)
    m = re.match(r"^(\d{2,6}):\s*(.*)$", first)
    if m:
        return m.group(1)
    # 字母开头的语义 code (例如 auth_not_configured)
    m = re.match(r"^([a-z][a-z0-9_]*):\s*(.*)$", first)
    if m:
        return m.group(1)
    return None


def _classify_result_retryable(error_code: str | None, subtype: str | None) -> bool | None:
    """根据 ResultMessage 的 error_code + subtype 判断是否可重试.

    返回:
      True  - 明确可重试 (transient)
      False - 明确不可重试 (permanent, 需要用户/系统介入)
      None  - 未知 (留给上层决策)
    """
    if error_code == _MAX_TURNS_ERROR_CODE or subtype == "error_max_turns":
        # max_turns 是配置问题, 重试没意义 (除非拉大 max_turns)
        return False
    if error_code in _RETRYABLE_ERROR_CODES:
        return True
    if error_code in _PERMANENT_ERROR_CODES:
        return False
    return None  # unknown


# qodercli / SDK 版本号, 给错误日志加上下文
try:
    import qoder_agent_sdk as _qas
    _SDK_VERSION: str | None = getattr(_qas, "__version__", None)
except Exception:  # noqa: BLE001
    _SDK_VERSION = None


def _log_sdk_error(
    *,
    agent: str,
    err: QoderCLIError,
    context: str = "drive",
) -> None:
    """统一结构化错误日志 — error_code / session_id / retryable / qodercli+SDK version.

    logger.error() 而非 exception(), 因为这是"已知业务错误"而非未预期崩溃;
    上层 (BaseCommand.run) 还会再 wrap 一层 BaseCommandError, 避免双重 stack trace.
    """
    payload = err.to_dict()
    logger.error(
        "qoder_sdk.error context={} agent={} msg={} error_code={} "
        "machine_code={} session_id={} subtype={} exit_code={} status_code={} "
        "retryable={} qodercli_version={} sdk_version={}",
        context, agent, payload.get("message", ""),
        payload.get("error_code"), payload.get("machine_code"),
        payload.get("session_id"), payload.get("subtype"),
        payload.get("exit_code"), payload.get("status_code"),
        payload.get("retryable"),
        # qodercli binary version: 从 _build_options 路径或 shutil.which 拿不到;
        # 这里传 None 即可, 上层要查版本可以用 ``qodercli --version`` 单独调用.
        None, _SDK_VERSION,
    )


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class QoderSDKProvider:
    """LLM Provider backed by qoder-agent-sdk（推荐路径，替代旧 subprocess 模式）.

    设计要点:
      - 每次 ``run()`` 开一个短生命周期 event loop（``asyncio.run``），保持与 RQ
        同步 worker 模型兼容.
      - 配置文件加载后立即冻结（frozen dataclass），单例模式由 ``client.py``
        的 ``get_client()`` 提供.
      - ``health_check()`` 跑一次最小调用（"ping"），超时短；失败返回 False.
    """

    provider_name = "qoder-sdk"

    def __init__(self) -> None:
        # 提前解析并校验关键配置
        if not config.qoder_sdk_pat:
            raise QoderCLIError(
                "QODER_SDK_PAT is required when using QoderSDKProvider "
                "(set QODER_SDK_PAT in .env)"
            )
        self._pat = config.qoder_sdk_pat
        self._cli_path = _resolve_cli_path(config.qoder_sdk_cli_path)
        if not self._cli_path:
            raise QoderCLIError(
                "qodercli executable not found on PATH "
                "(set QODER_SDK_CLI_PATH or install qodercli)"
            )
        # model 优先级：QODER_SDK_MODEL > QODERCLI_MODEL > 默认 Lite
        self._model = (
            config.qoder_sdk_model
            or config.qodercli_model
            or DEFAULT_MODEL
        )
        self._fallback_model = (
            config.qoder_sdk_fallback_model or config.qodercli_fallback_model
        )
        self._max_turns = config.qoder_sdk_max_turns
        self._permission_mode = config.qoder_sdk_permission_mode
        self._disallowed_tools = config.qoder_sdk_disallowed_tools

        logger.info(
            "qoder_sdk.provider init model={} cli={} max_turns={} perm_mode={!r}",
            self._model, self._cli_path, self._max_turns, self._permission_mode,
        )

    # ------------------------------------------------------------------
    # 健康检查
    # ------------------------------------------------------------------
    def health_check(self) -> bool:
        """跑一次极短 prompt，超时 30s 验证 SDK + qodercli + PAT 链路可用."""
        try:
            self._run_async(
                agent="describe",  # 任意存在 prompt 即可
                prompt="ping",
                workdir=Path("/tmp"),
                files=None,
                timeout=30,
                tolerant_markdown=True,
                add_dirs=None,
                model_override=None,
            )
            return True
        except Exception as e:
            logger.warning("qoder_sdk.health_check failed: {}", e)
            return False

    # ------------------------------------------------------------------
    # 核心入口
    # ------------------------------------------------------------------
    def run(
        self,
        *,
        agent: str,
        prompt: str,
        workdir: Path,
        files: list[Path] | None = None,
        timeout: int | None = None,
        tolerant_markdown: bool = False,
    ) -> LLMResult:
        actual_timeout = timeout or config.qoder_sdk_timeout
        try:
            return self._run_async(
                agent=agent, prompt=prompt, workdir=workdir, files=files,
                timeout=actual_timeout, tolerant_markdown=tolerant_markdown,
                add_dirs=None, model_override=None,
            )
        except QoderCLIOutputError as e:
            # 主模型失败时尝试 fallback（与原 QoderCLIProvider 行为一致）
            if not self._fallback_model or self._fallback_model == self._model:
                raise
            logger.warning(
                "qoder_sdk.fallback primary={} fallback={} reason={}",
                self._model, self._fallback_model, e,
            )
            result = self._run_async(
                agent=agent, prompt=prompt, workdir=workdir, files=files,
                timeout=actual_timeout, tolerant_markdown=tolerant_markdown,
                add_dirs=None, model_override=self._fallback_model,
            )
            logger.info(
                "qoder_sdk.fallback success model={} agent={}",
                self._fallback_model, agent,
            )
            return result

    # ------------------------------------------------------------------
    # 内部：async → sync
    # ------------------------------------------------------------------
    def _run_async(
        self,
        *,
        agent: str,
        prompt: str,
        workdir: Path,
        files: list[Path] | None,
        timeout: int,
        tolerant_markdown: bool,
        add_dirs: list[Path] | None,
        model_override: str | None,
    ) -> LLMResult:
        """每次调用开一个新的 event loop（短生命周期，避免长连接 hang 复现）."""
        options = _build_options(
            agent=agent, workdir=workdir, pat=self._pat, cli_path=self._cli_path,
            model=model_override or self._model,
            max_turns=self._max_turns, permission_mode=self._permission_mode,
            disallowed_tools=self._disallowed_tools, add_dirs=add_dirs,
        )
        return asyncio.run(
            self._drive(
                options=options, agent=agent, prompt=prompt, files=files,
                timeout=timeout, tolerant_markdown=tolerant_markdown,
            )
        )

    async def _drive(
        self,
        *,
        options: QoderAgentOptions,
        agent: str,
        prompt: str,
        files: list[Path] | None,
        timeout: int,
        tolerant_markdown: bool,
    ) -> LLMResult:
        """驱动一次 ``query()`` 调用，收集 ``AssistantMessage`` + ``ResultMessage``."""
        t0 = time.monotonic()
        prompt_tokens = 0
        completion_tokens = 0
        cost_credits = 0.0
        context_usage_ratio = 0.0
        last_text = ""
        model_id = options.model or ""
        result_msg: ResultMessage | None = None

        # diff.patch 已经由 ws.worktree/diff.patch 提供（base 行为），SDK 看不到
        # --attachment 概念，但只要 workdir 在 add_dirs 里，agent 可用 Read 工具读.
        try:
            iterator = query(prompt=prompt, options=options)
            async for message in _aiter_with_timeout(iterator, timeout):
                msg_type = type(message).__name__
                if msg_type == "AssistantMessage":
                    for block in getattr(message, "content", []):
                        if isinstance(block, TextBlock):
                            last_text += block.text
                    usage = getattr(message, "usage", None)
                    if usage:
                        prompt_tokens = max(
                            prompt_tokens, int(usage.get("input_tokens", 0) or 0)
                        )
                        completion_tokens = max(
                            completion_tokens, int(usage.get("output_tokens", 0) or 0)
                        )
                        if usage.get("credits"):
                            cost_credits = float(usage["credits"])
                        if usage.get("context_usage_ratio"):
                            context_usage_ratio = float(usage["context_usage_ratio"])
                    if getattr(message, "model", None):
                        model_id = message.model
                elif isinstance(message, ResultMessage):
                    result_msg = message
                    if getattr(message, "total_credits", None):
                        cost_credits = float(message.total_credits)
                    if getattr(message, "usage", None):
                        u = message.usage
                        if u:
                            if u.get("input_tokens"):
                                prompt_tokens = max(prompt_tokens, int(u["input_tokens"]))
                            if u.get("output_tokens"):
                                completion_tokens = max(completion_tokens, int(u["output_tokens"]))
                            if u.get("credits"):
                                cost_credits = float(u["credits"])
                            if u.get("context_usage_ratio"):
                                context_usage_ratio = float(u["context_usage_ratio"])
                    if message.is_error:
                        logger.warning(
                            "qoder_sdk.result is_error subtype={} errors={}",
                            message.subtype, message.errors,
                        )
        except asyncio.TimeoutError as e:
            timeout_err = QoderCLITimeoutError(
                f"qoder-sdk query timeout after {timeout}s (agent={agent})",
                error_code="sdk_timeout",
                retryable=True,  # 拉长 timeout 重试可能成功
                timeout_ms=timeout * 1000,
            )
            _log_sdk_error(agent=agent, err=timeout_err, context="timeout")
            raise timeout_err from e
        except QoderSDKError as e:
            mapped = _map_sdk_exception(e)
            _log_sdk_error(agent=agent, err=mapped, context="sdk_exception")
            raise mapped from e

        duration_ms = int((time.monotonic() - t0) * 1000)

        raw = result_msg.result if result_msg else last_text
        if not raw:
            raw = last_text

        # 1) SDK 报告的错误必须在数据解析前就 raise，避免上层看到半截结果当成成功
        if result_msg is not None and result_msg.is_error:
            subtype = result_msg.subtype
            session_id = result_msg.session_id
            errs = list(result_msg.errors or [])
            err_summary = "; ".join(str(x) for x in errs[:3]) if errs else "(no error details)"
            error_code = _parse_result_error_code(errs)
            retryable = _classify_result_retryable(error_code, subtype)
            original_errors = tuple(errs) if errs else None
            if subtype == "error_max_turns":
                # 跑满了 max_turns 但仍可能有部分 JSON — 记 warning 让上层知道
                logger.warning(
                    "qoder_sdk error_max_turns (may be partial output); agent={} "
                    "session_id={} errs={}",
                    agent, session_id, err_summary,
                )
            else:
                # error_during_execution / 其它: 整次调用失败，不应回 data
                drive_err = QoderCLIError(
                    f"qoder-sdk reported error (subtype={subtype}, agent={agent}): {err_summary}",
                    error_code=error_code,
                    subtype=subtype,
                    session_id=session_id,
                    original_errors=original_errors,
                    retryable=retryable,
                )
                _log_sdk_error(agent=agent, err=drive_err, context="drive_is_error")
                raise drive_err

        try:
            data = _parse_result_text(raw)
        except json.JSONDecodeError:
            if tolerant_markdown:
                # tolerant 兜底：先剥 fence + 尝试剥 markdown wrapper，得到纯 markdown
                stripped = _strip_fence(raw)
                stripped = _strip_line_number_prefix(stripped)
                stripped = _unwrap_markdown_wrapper(stripped)
                return LLMResult(
                    data={"markdown": stripped},
                    provider=self.provider_name,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    cost_credits=cost_credits,
                    context_usage_ratio=context_usage_ratio,
                    duration_ms=duration_ms,
                    model=model_id,
                    raw_output=raw,
                )
            raise QoderCLIOutputError(
                f"qoder-sdk output not JSON (agent={agent}, subtype="
                f"{result_msg.subtype if result_msg else 'no_result'}); raw[:300]={raw[:300]!r}",
                error_code="output_not_json",
                subtype=(result_msg.subtype if result_msg else None),
                session_id=(result_msg.session_id if result_msg else None),
                retryable=False,  # JSON 输出格式不对, 重试不会变好
            )

        logger.info(
            "qoder_sdk.usage agent={} model={} pt={} ct={} credits={:.4f} ctx_ratio={:.4f} duration_ms={}",
            agent, model_id, prompt_tokens, completion_tokens,
            cost_credits, context_usage_ratio, duration_ms,
        )
        if result_msg and result_msg.subtype == "error_max_turns":
            logger.warning(
                "qoder_sdk output truncated (subtype=error_max_turns); agent={}",
                agent,
            )

        return LLMResult(
            data=data,
            provider=self.provider_name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_credits=cost_credits,
            context_usage_ratio=context_usage_ratio,
            duration_ms=duration_ms,
            model=model_id,
            raw_output=raw,
        )


async def _aiter_with_timeout(aiter, timeout: int):
    """async iterator + 超时（Python 3.11+ ``asyncio.timeout``）.

    把整个迭代放成 task，超时直接 cancel；不需要手写 queue/事件.
    """
    async def collect():
        out = []
        async for item in aiter:
            out.append(item)
        return out

    try:
        async with asyncio.timeout(timeout):
            items = await collect()
    except asyncio.TimeoutError:
        raise asyncio.TimeoutError() from None
    for item in items:
        yield item


__all__ = ["QoderSDKProvider"]
