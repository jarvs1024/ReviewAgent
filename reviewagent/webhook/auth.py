"""Webhook 鉴权 — X-Gitlab-Token 比对."""
from __future__ import annotations

import hmac

from fastapi import HTTPException, Request, status

from reviewagent.config import config


async def verify_webhook_token(request: Request) -> None:
    """校验 X-Gitlab-Token header 与配置的 secret 一致.

    使用 hmac.compare_digest 防时序攻击.
    失败抛 401.
    """
    provided = request.headers.get("X-Gitlab-Token", "")
    expected = config.gitlab_webhook_secret
    if not provided or not expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing X-Gitlab-Token header or config secret",
        )
    if not hmac.compare_digest(provided.encode(), expected.encode()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid webhook token",
        )