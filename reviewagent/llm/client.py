"""LLM Provider 工厂 — 根据 config.llm_provider 返回对应 provider 实例（单例）.

上层通过 reviewagent.llm.get_client() 拿当前 provider；切换 provider 只需改 .env 的
LLM_PROVIDER=opencode|qodercli，无需改业务代码.
"""
from __future__ import annotations

from reviewagent.config import config
from reviewagent.llm.base import BaseLLMProvider
from reviewagent.llm.opencode_provider import OpencodeProvider
from reviewagent.logging_setup import logger

# 模块级单例；reset_client() 仅供测试.
_client: BaseLLMProvider | None = None


def get_client() -> BaseLLMProvider:
    """根据 config.llm_provider 返回 provider 实例（懒初始化 + 单例）.

    Returns:
        BaseLLMProvider: OpencodeProvider 或 QoderCLIProvider.

    Raises:
        ValueError: 未知的 provider 名字.
        NotImplementedError: QoderCLIProvider 尚未实现时调 __init__/run.
    """
    global _client
    if _client is not None:
        return _client

    name = config.llm_provider
    if name == "opencode":
        logger.info("llm.client init provider=opencode")
        _client = OpencodeProvider()
    elif name == "qodercli":
        from reviewagent.llm.qodercli_provider import QoderCLIProvider  # lazy import
        logger.info("llm.client init provider=qodercli")
        _client = QoderCLIProvider()
    else:
        raise ValueError(
            f"unknown LLM_PROVIDER={name!r}; expected 'opencode' or 'qodercli'"
        )

    # C3: provider init 计入 metric (用于对比 opencode vs qodercli 流量比例)
    try:
        from reviewagent.metrics import inc as _metric_inc
        _metric_inc("reviewagent_llm_provider_initialized_total", provider=name)
    except Exception:
        pass

    return _client


def reset_client() -> None:
    """清空单例 — 单元测试在切换 provider 时调用."""
    global _client
    _client = None
