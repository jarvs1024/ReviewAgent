"""DingTalk custom-robot webhook notifier.

支持:
- 可选 HMAC-SHA256 加签 (DingTalk '加签' 安全模式)
- 网络/5xx 错误时 N 次重试 + 指数退避
- dry_run 模式 — 不发请求, 只记录 payload (默认开启, 避免未配置 webhook 时发空消息)
- 只依赖 requests, 不引额外 SDK

参考: pr-agent/pr_agent/reporting/notifiers/dingtalk.py (签名 + 重试结构)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

from reviewagent.logging_setup import logger

from .base import DeliveryResult


def _sign_url(webhook_url: str, secret: str) -> str:
    """DingTalk 加签 — timestamp + sign."""
    timestamp = str(round(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    sep = "&" if "?" in webhook_url else "?"
    return f"{webhook_url}{sep}timestamp={timestamp}&sign={sign}"


@dataclass
class DingTalkNotifier:
    """DingTalk 自定义机器人 webhook 投递.

    Args:
        webhook_url: 钉钉机器人 webhook (空 = dry_run)
        secret: 加签 secret (空 = 不签名)
        retry_attempts: 失败重试次数 (默认 3)
        dry_run: True 则不实际发送, 只 log payload
        timeout: HTTP timeout 秒
    """
    webhook_url: str = ""
    secret: str = ""
    retry_attempts: int = 3
    dry_run: bool = True
    timeout: float = 10.0
    name: str = "dingtalk"

    def send(self, title: str, markdown_chunks: list[str]) -> DeliveryResult:
        if not markdown_chunks:
            return DeliveryResult(success=True, chunks_sent=0, chunks_total=0,
                                 meta={"skipped": "no_chunks"})

        if self.dry_run or not self.webhook_url:
            for idx, chunk in enumerate(markdown_chunks, start=1):
                ct = title if len(markdown_chunks) == 1 else f"{title} ({idx}/{len(markdown_chunks)})"
                logger.info(
                    "dingtalk.dry_run chunk {}/{} ({} bytes) title={!r}\n{}",
                    idx, len(markdown_chunks), len(chunk.encode("utf-8")), ct, chunk,
                )
            return DeliveryResult(
                success=True,
                chunks_sent=len(markdown_chunks),
                chunks_total=len(markdown_chunks),
                meta={"dry_run": True, "reason": "no_webhook_or_dry_run"},
            )

        url = _sign_url(self.webhook_url, self.secret) if self.secret else self.webhook_url
        last_error: str | None = None
        chunks_sent = 0

        for idx, chunk in enumerate(markdown_chunks, start=1):
            ct = title if len(markdown_chunks) == 1 else f"{title} ({idx}/{len(markdown_chunks)})"
            payload: dict[str, Any] = {
                "msgtype": "markdown",
                "markdown": {"title": ct, "text": chunk},
            }
            ok = self._post_with_retry(url, payload, idx)
            if ok:
                chunks_sent += 1
            else:
                last_error = f"chunk {idx} failed after {self.retry_attempts} attempts"

        return DeliveryResult(
            success=(chunks_sent == len(markdown_chunks)),
            chunks_sent=chunks_sent,
            chunks_total=len(markdown_chunks),
            error=last_error,
            meta={"webhook_host": urllib.parse.urlparse(url).netloc},
        )

    def _post_with_retry(self, url: str, payload: dict[str, Any], idx: int) -> bool:
        import requests
        last_err: str | None = None
        for attempt in range(1, self.retry_attempts + 1):
            try:
                r = requests.post(url, json=payload, timeout=self.timeout)
                if r.status_code == 200:
                    body = r.json()
                    if body.get("errcode", 0) == 0:
                        return True
                    last_err = f"errcode={body.get('errcode')} errmsg={body.get('errmsg')}"
                elif 500 <= r.status_code < 600:
                    last_err = f"HTTP {r.status_code}"
                else:
                    last_err = f"HTTP {r.status_code}: {r.text[:200]}"
                    break  # 4xx 不重试
            except requests.RequestException as e:
                last_err = f"request exception: {e}"
            if attempt < self.retry_attempts:
                backoff = 2 ** (attempt - 1)
                logger.warning(
                    "dingtalk chunk {} attempt {}/{} failed: {}; retry in {}s",
                    idx, attempt, self.retry_attempts, last_err, backoff,
                )
                time.sleep(backoff)
        logger.error("dingtalk chunk {} all attempts failed: {}", idx, last_err)
        return False


__all__ = ["DingTalkNotifier"]
