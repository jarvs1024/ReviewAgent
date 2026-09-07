"""qodercli exception hierarchy.

Subprocess driver exceptions all derive from :class:`QoderCLIError`,
so callers can write ``except QoderCLIError`` without naming the
concrete subclass. As of 2026-08-05 only the subprocess driver remains
(ACP removed — see ``qodercli_provider`` module docstring).

诊断字段 (since 2026-09-07) — 全部 keyword-only, 默认 None,
不破坏旧的 ``QoderCLIError("msg")`` 调用 (subprocess 路径仍这么用).

字段含义:
  error_code      - 机器可读错误码 (从 ResultMessage.errors[0] "code: msg" 解析,
                   或 SDK 异常类 .code 属性). 例: "105", "auth_not_configured",
                   "error_max_turns". 上层可基于此路由 (重试 / 报警 / fallback).
  machine_code    - SDK 异常类自带的语义 code (与 error_code 区别:
                   error_code 来自 qodercli wire protocol; machine_code 来自
                   SDK Python 异常 .code 属性).
  session_id      - qodercli session UUID (ResultMessage.session_id),
                   用于在 qodercli 端复现 / 翻日志.
  subtype         - ResultMessage.subtype (success / error_max_turns /
                   error_during_execution). 区分 partial vs hard failure.
  original_errors - ResultMessage.errors 原文 tuple, 完整保留上游错误串,
                   上层可拼到日志 / issue 模板.
  exit_code       - ProcessError.exit_code (qodercli 进程退出码), None 时非进程异常.
  status_code     - CloudAgentApiError.status (HTTP 状态码), 仅云端错误用.
  capability      - UnsupportedCliCapabilityError.capability (qodercli 缺的特性名),
                   用于提示用户升级 qodercli 版本.
  timeout_ms      - ModelPolicyTimeoutError.timeout_ms (resolve_model callback 超时),
                   仅 ModelPolicyTimeoutError 用.
  retryable       - 上层可否重试 (None 表示 unknown; True/False 由 SDK 错误码分类决定).
                   见 _classify_retryable() in qoder_sdk_provider.
  extra           - 兜底 dict, 装上面没列的字段 (e.g. CLIConnectionError 的 errno,
                   MessageParseError.data 等). 调试 / 日志用, 上层可忽略.
"""

from __future__ import annotations

from typing import Any


class QoderCLIError(RuntimeError):
    """Base class for all qodercli driver failures.

    构造:
      QoderCLIError("msg")                              # 旧 subprocess 路径
      QoderCLIError("msg", error_code="105", retryable=False)  # 新 SDK 路径
    """

    def __init__(self, message: str = "", **fields: Any) -> None:
        super().__init__(message)
        # 诊断字段 — 全部 None 默认, 显式构造时填充.
        # 用 setattr 而非 __init__ 参数, 这样 Pydantic / loguru 等序列化不会
        # 被 dataclass 之类机制强制声明字段.
        self.error_code: str | None = fields.get("error_code")
        self.machine_code: str | None = fields.get("machine_code")
        self.session_id: str | None = fields.get("session_id")
        self.subtype: str | None = fields.get("subtype")
        self.original_errors: tuple[str, ...] | None = fields.get("original_errors")
        self.exit_code: int | None = fields.get("exit_code")
        self.status_code: int | None = fields.get("status_code")
        self.capability: str | None = fields.get("capability")
        self.timeout_ms: int | None = fields.get("timeout_ms")
        self.retryable: bool | None = fields.get("retryable")
        self.extra: dict[str, Any] = fields.get("extra") or {}

    def to_dict(self) -> dict[str, Any]:
        """转 dict 给 logger / Sentry / 报告层用, 排除 None 字段."""
        out: dict[str, Any] = {"message": str(self)}
        for k in (
            "error_code", "machine_code", "session_id", "subtype",
            "original_errors", "exit_code", "status_code", "capability",
            "timeout_ms", "retryable",
        ):
            v = getattr(self, k)
            if v is not None:
                out[k] = v
        if self.extra:
            out["extra"] = self.extra
        return out


class QoderCLITimeoutError(QoderCLIError):
    """qodercli task exceeded its wall-clock budget."""


class QoderCLIOutputError(QoderCLIError):
    """qodercli stdout could not be parsed as the expected JSON shape."""


__all__ = ["QoderCLIError", "QoderCLITimeoutError", "QoderCLIOutputError"]
