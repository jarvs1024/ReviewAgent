"""opencode 客户端 — 走 HTTP API（POST /session + POST /session/:id/message）.

为什么不用 subprocess `opencode run`:
    - subprocess 启动时强制 fetch models.dev（公网元数据），离线环境卡 10s 超时
    - opencode run --attach 模式实测不产出任何输出
    - HTTP API 路径不需要 models.dev 元数据（serve 启动时已加载）
    - HTTP API 返回结构化 JSON parts 数组，解析稳定

设计要点:
    - 每个任务独立 ephemeral session（POST 后 DELETE）
    - 文件通过 data URL 形式上传到 parts 数组（opencode 期望 url 字段）
    - assistant 回复提取：parts 数组中最后一个 type=text 的 part.text
"""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from reviewagent.config import config
from reviewagent.logging_setup import logger


class OpencodeError(RuntimeError):
    """opencode 调用失败基类."""


class OpencodeTimeoutError(OpencodeError):
    """opencode 任务超时."""


class OpencodeOutputError(OpencodeError):
    """opencode 输出无法解析为 JSON."""


@dataclass
class OpencodeResult:
    """opencode run 结果 — 包含解析后的 dict 和 token 统计."""
    data: dict[str, Any]
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""


class OpencodeClient:
    """opencode HTTP API 客户端.

    要求:
        - opencode serve 在 OPENCODE_URL 上跑（默认 http://127.0.0.1:4096）
        - provider / model 已在 opencode.jsonc + auth.json 中配置
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        default_timeout: int = 600,
        model: str | None = None,
    ):
        self.base_url = (base_url or config.opencode_url).rstrip("/")
        self.default_timeout = default_timeout
        # 默认模型：deepseek-v4-flash（deepseek provider 已配置）
        self.model = model or config.opencode_model
        # Basic Auth（如果配了 OPENCODE_PASSWORD）
        self._auth = (
            (config.opencode_username, config.opencode_password)
            if config.opencode_password
            else None
        )

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        url = f"{self.base_url}{path}"
        try:
            with httpx.Client(auth=self._auth, timeout=30) as c:
                r = c.request(method, url, **kwargs)
                r.raise_for_status()
                return r
        except httpx.HTTPStatusError as e:
            body = e.response.text[:500] if e.response else ""
            raise OpencodeError(f"opencode HTTP {e.response.status_code}: {body}") from e
        except httpx.RequestError as e:
            raise OpencodeError(f"opencode request failed: {e}") from e

    def run(
        self,
        *,
        agent: str,
        prompt: str,
        workdir: Path,
        files: list[Path] | None = None,
        timeout: int | None = None,
    ) -> OpencodeResult:
        """通过 HTTP API 调用 opencode serve 执行 agent；返回 OpencodeResult.

        Args:
            agent: agent 名称（与 prompts/<name>.md 的 frontmatter.name 对应）
            prompt: 用户 prompt（agent 看到的核心指令）
            workdir: opencode 进程的工作目录（一般是 git worktree）
            files: 附加为文件 context 的路径列表
            timeout: 超时秒数；默认 self.default_timeout

        Returns:
            OpencodeResult — 包含解析后的 dict 和 token 统计
        """
        workdir_str = str(workdir)
        file_names = [f.name for f in (files or [])]
        logger.info(
            "opencode.run start agent={} workdir={} files={} timeout={}",
            agent, workdir_str, file_names, timeout or self.default_timeout,
        )

        # 1. 创建 ephemeral session
        try:
            r = self._request(
                "POST", "/session",
                json={"title": f"reviewagent-{workdir.name}-{int(__import__('time').time())}"},
            )
        except OpencodeError as e:
            raise OpencodeError(f"create session failed: {e}") from e
        session = r.json()
        sid = session.get("id", "")
        if not sid:
            raise OpencodeError(f"create session returned no id: {session}")

        try:
            # 2. 构造 parts：把 file 内容直接拼到 prompt text（不开 file parts）
            #
            # 原因: opencode HTTP API 的 file parts (data URL) 在内容较大时
            #       会触发 Server 500 / 长时间无响应. 而 opencode HTTP API 不接受 cwd 字段,
            #       agent 又无法访问 worktree 目录里的 .diff.patch.
            #
            # 解决: prompt 里直接贴 diff 内容, 截断到 config.opencode_max_diff_chars.
            MAX_DIFF_CHARS = config.opencode_max_diff_chars
            prompt_text = prompt
            for fp in files or []:
                try:
                    content = fp.read_text(encoding="utf-8", errors="replace")
                except OSError as e:
                    raise OpencodeError(f"read file {fp} failed: {e}") from e
                if len(content) > MAX_DIFF_CHARS:
                    content = (
                        content[:MAX_DIFF_CHARS]
                        + f"\n\n[... diff 截断, 原始 {len(content)} 字符 ...]"
                    )
                prompt_text += f"\n\n## 文件: `{fp.name}`\n```\n{content}\n```\n"

            parts: list[dict[str, Any]] = [{"type": "text", "text": prompt_text}]

            # 3. 发消息（同步，等待完整响应）
            # 注意: 不传 cwd 字段 — opencode 会用 daemon 自己的 cwd (通常是 /root).
            #       如果传 cwd, opencode 会尝试在 worktree 里初始化 .opencode/,
            #       git worktree 里没有这个目录, 导致 Server 500.
            payload = {
                "parts": parts,
                "model": {"providerID": self.model.split("/", 1)[0],
                          "modelID": self.model.split("/", 1)[1]},
                "agent": agent,
            }
            try:
                r = self._request(
                    "POST", f"/session/{sid}/message",
                    json=payload,
                    timeout=timeout or self.default_timeout,
                )
            except httpx.TimeoutException as e:
                raise OpencodeTimeoutError(f"opencode timeout after {timeout or self.default_timeout}s") from e
            except OpencodeError as e:
                raise OpencodeError(f"send message failed: {e}") from e

            msg = r.json()
            data = self._extract_assistant_dict(msg)
            tokens = self._extract_tokens(msg)
            return OpencodeResult(
                data=data,
                prompt_tokens=tokens.get("input", 0),
                completion_tokens=tokens.get("output", 0),
                model=msg.get("info", {}).get("modelID", ""),
            )

        finally:
            # 4. 清理 session（epemeral；不污染 opencode 数据库）
            try:
                self._request("DELETE", f"/session/{sid}")
            except Exception as e:
                logger.warning("opencode session cleanup failed (non-fatal): {}", e)

    @staticmethod
    def _extract_assistant_dict(msg: dict[str, Any]) -> dict[str, Any]:
        """从 /message 响应中提取 agent 最终输出 dict.

        msg 结构:
          {
            "info": {"role": "assistant", "modelID": "...", "tokens": {...}, "finish": "stop", ...},
            "parts": [
              {"type": "step-start", ...},
              {"type": "reasoning", "text": "..."},
              {"type": "text", "text": "{...最终 JSON...}"},
              {"type": "step-finish", ...},
            ]
          }

        策略:
          1. 找最后一个 type=text 的 part，提取 text
          2. text 必须是合法 JSON（agent 在 prompt 里被强约束）
          3. 解析失败抛 OpencodeOutputError
        """
        parts = msg.get("parts") or []
        text = ""
        for part in reversed(parts):
            if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
                text = part["text"]
                break
        if not text:
            # 退化：找 reasoning 之外任何含 text 的 part
            for part in reversed(parts):
                if isinstance(part, dict) and part.get("text") and part.get("type") != "reasoning":
                    text = part["text"]
                    break

        if not text:
            finish = msg.get("info", {}).get("finish", "?")
            types = [p.get("type", "?") for p in parts]
            raise OpencodeOutputError(
                f"opencode produced no text part; finish={finish}, parts_types={types}"
            )

        text = text.strip()
        json_str = _extract_json_block(text)
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as e:
            raise OpencodeOutputError(
                f"opencode assistant text not JSON: {e}; text={text[:500]}"
            ) from e

        if not isinstance(data, dict):
            raise OpencodeOutputError(
                f"opencode assistant output not dict: {type(data).__name__}, val={str(data)[:200]}"
            )

        return data

    @staticmethod
    def _extract_tokens(msg: dict[str, Any]) -> dict[str, int]:
        """从 /message 响应中提取 token 统计.

        msg.info.tokens 结构因 provider 不同:
            {"input": 5227, "output": 2}  (deepseek/openai-compatible)
            {"prompt": ..., "completion": ...}  (其他 provider)
        """
        info = msg.get("info") or {}
        tokens = info.get("tokens") or {}
        return {
            "input": tokens.get("input", 0) or tokens.get("prompt", 0) or 0,
            "output": tokens.get("output", 0) or tokens.get("completion", 0) or 0,
        }


def _extract_json_block(text: str) -> str:
    """从文本中提取第一个完整的 JSON 对象.

    策略 (按优先级):
      1. 剥 markdown 围栏 (```json ... ```) 后整段 parse
      2. 整段 text 直接 json.loads
      3. 用 json.JSONDecoder().raw_decode 从每个 { 位置试解，
         跳过 <think>...</think> 之类的干扰
    """
    if not text:
        raise ValueError("empty text")

    # 1. 剥 markdown ```json ... ``` 围栏
    fence = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if fence:
        candidate = fence.group(1).strip()
        if candidate.startswith("{"):
            try:
                # 验证是合法 JSON
                json.loads(candidate)
                return candidate
            except json.JSONDecodeError:
                pass

    # 2. 整段尝试
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            json.loads(stripped)
            return stripped
        except json.JSONDecodeError:
            pass

    # 3. 试每个 { 位置 (raw_decode 一次解完，从 i 开始到 i+长度)
    decoder = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            obj, end = decoder.raw_decode(text, m.start())
            if isinstance(obj, dict):
                return text[m.start() : end]
        except json.JSONDecodeError:
            continue

    raise ValueError("no valid JSON object found in text")


# 全局单例
client = OpencodeClient()
