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
上层 except 子句零改动；未来 QoderCLIProvider 上线后可平滑切到 LLMError 命名空间.

ACP 客户端类型也可以直接从本包 re-export，方便集成测试与上层模块不经内部路径
直接构造 QoderCLIACPClient.
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
from reviewagent.llm.qodercli_acp import (  # noqa: F401  (re-exported for tests / external callers)
    QoderCLIACPClient,
    QoderCLIACPError,
    QoderCLIAuthError,
    QoderCLIProtocolError,
)

__all__ = [
    "BaseLLMProvider",
    "LLMResult",
    "OpencodeError",
    "OpencodeOutputError",
    "OpencodeTimeoutError",
    "QoderCLIError",
    "QoderCLIOutputError",
    "QoderCLIACPClient",
    "QoderCLIACPError",
    "QoderCLIAuthError",
    "QoderCLITimeoutError",
    "QoderCLIProtocolError",
    "get_client",
    "reset_client",
]
