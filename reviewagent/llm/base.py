"""LLM Provider 抽象基类 + LLMResult dataclass.

所有 LLM 调用通过 BaseLLMProvider.run() 统一接口进入，Provider 自己实现:
    - 参数拼装（HTTP / subprocess / 直接 API）
    - JSON 提取（适配层统一实现 _extract_json_block fallback）
    - 重试逻辑（截断 → 减半 diff 重试；可选）
    - 异常包装

详见 docs/LLM_PROVIDER_ADAPTER.md.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class LLMResult:
    """统一 LLM 调用结果.

    Attributes:
        data: 解析后的 agent 输出 dict（agent prompt 强约束的 JSON）.
        prompt_tokens: input token 数；部分 provider / model 可能为 0.
        completion_tokens: output token 数.
        model: 实际调用的模型名（空字符串表示 provider 未填充）.
        duration_ms: 端到端耗时（毫秒）.
        provider: provider 名 "opencode" | "qodercli"；用于 telemetry / 日志.
        raw_output: 原始输出文本（调试 / tolerant_markdown 兜底用）.
    """

    data: dict[str, Any]
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""
    duration_ms: int = 0
    provider: str = ""
    raw_output: str = ""


class BaseLLMProvider(ABC):
    """LLM Provider 基类 — 所有 Provider 必须实现 run / health_check / provider_name."""

    @abstractmethod
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
        """调用 agent 执行任务，返回 LLMResult.

        Args:
            agent: agent 名称（与 reviewagent/prompts/<name>.md 对应）.
            prompt: user prompt 文本.
            workdir: 工作目录（git worktree 路径或 Path.cwd()）.
            files: 附加文件路径列表（qodercli 走 --attachment；opencode 拼到 prompt）.
            timeout: 超时秒数；None 用 provider 默认.
            tolerant_markdown: 周报兜底模式；JSON 解析失败时把原文当 markdown 兜底.
        """
        raise NotImplementedError

    @abstractmethod
    def health_check(self) -> bool:
        """provider 健康检查；返回 True 表示可用."""
        raise NotImplementedError

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """provider 名（用于 telemetry / 日志）."""
        raise NotImplementedError
