"""LLM Provider 适配层 — 通过 config.llm_provider 在 opencode / qodercli 之间切换.

迁移路径（上层代码从 opencode client 切到本适配层，零行为改动）:
    # 旧:
    from reviewagent.opencode.client import OpencodeError, client as opencode
    oc_result = opencode.run(agent=..., prompt=..., workdir=..., files=...)

    # 新:
    from reviewagent.llm import get_client, OpencodeError
    client = get_client()
    result = client.run(agent=..., prompt=..., workdir=..., files=...)

异常类仍 re-export 原 OpencodeError / OpencodeOutputError / OpencodeTimeoutError，
上层 except 子句零改动.
"""
from __future__ import annotations

from reviewagent.opencode.client import (
    OpencodeError,
    OpencodeOutputError,
    OpencodeTimeoutError,
)
from reviewagent.llm.base import BaseLLMProvider, LLMResult
from reviewagent.llm.client import get_client, reset_client
from reviewagent.llm.qodercli_errors import (  # noqa: F401  (re-exported for tests / external callers)
    QoderCLIError,
    QoderCLITimeoutError,
    QoderCLIOutputError,
)

__all__ = [
    "BaseLLMProvider",
    "LLMResult",
    "OpencodeError",
    "OpencodeOutputError",
    "OpencodeTimeoutError",
    "QoderCLIError",
    "QoderCLIOutputError",
    "QoderCLITimeoutError",
    "get_client",
    "reset_client",
]
