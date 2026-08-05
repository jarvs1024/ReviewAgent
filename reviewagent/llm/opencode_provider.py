"""OpencodeProvider — reviewagent.opencode.client 的纯包装.

零逻辑改动原则：所有 HTTP 调用 / JSON 解析 / 重试逻辑仍由原 OpencodeClient 负责，
本类只负责:
    1. 把 OpencodeResult 转成 LLMResult（补 provider / duration_ms / raw_output）
    2. 异常透传 — OpencodeError / OpencodeOutputError / OpencodeTimeoutError 原名 re-raise

如果以后需要重构 OpencodeClient 逻辑（例如 workdir 透传 bug 修复），可以在本类中加
适配层，但本次保持纯 wrapper 以最小化改动面.
"""
from __future__ import annotations

import time
from pathlib import Path

import httpx

from reviewagent.llm.base import BaseLLMProvider, LLMResult
from reviewagent.opencode.client import OpencodeClient, OpencodeResult


class OpencodeProvider(BaseLLMProvider):
    """包装 OpencodeClient — 走 HTTP API（POST /session + POST /session/:id/message）.

    要求:
        - opencode serve 在 OpencodeClient.base_url 上跑（默认 http://127.0.0.1:4096）
        - provider / model 已在 opencode.jsonc + auth.json 中配置
    """

    def __init__(self, client: OpencodeClient | None = None):
        # 默认复用 OpencodeClient() 全局单例里的配置；测试可注入 mock.
        self._client = client or OpencodeClient()

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
        t0 = time.monotonic()
        oc: OpencodeResult = self._client.run(
            agent=agent,
            prompt=prompt,
            workdir=workdir,
            files=files,
            timeout=timeout,
            tolerant_markdown=tolerant_markdown,
        )
        duration_ms = int((time.monotonic() - t0) * 1000)
        return LLMResult(
            data=oc.data,
            prompt_tokens=oc.prompt_tokens,
            completion_tokens=oc.completion_tokens,
            model=oc.model,
            duration_ms=duration_ms,
            provider=self.provider_name,
            raw_output=oc.raw_output,
        )

    def health_check(self) -> bool:
        """走 opencode GET /api/health; 网络或 auth 失败返回 False."""
        try:
            with httpx.Client(auth=self._client.auth, timeout=5) as c:
                r = c.get(f"{self._client.base_url}/api/health")
                return r.status_code == 200
        except Exception:
            return False

    @property
    def provider_name(self) -> str:
        return "opencode"
