"""Webhook 前门契约：鉴权 + 命令解析 + 限流/冷却的 fail-open 语义.

Why 单独一份: webhook 是整个系统的入口，命令解析错一个字符就是
「机器人没反应」；鉴权漏一条就是「任意人可触发 LLM 计费」。
这两块此前**没有直接单元测试**（只被 E2E 间接覆盖），而 E2E 失败时
定位成本很高。

fail-open 的三处是**刻意的设计决策**（Redis/SQLite 故障时宁可多跑一次，
也不要整条链哑掉），但刻意决策更需要测试钉住 —— 否则有人改成 fail-closed
不会有任何测试变红。
"""
from __future__ import annotations

import pytest

from reviewagent.webhook.parsers import extract_command


# ---------------------------------------------------------------------------
# 命令解析
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("body,expected", [
    ("/describe", "describe"),
    ("/describe 请重新生成", "describe"),
    ("/improve", "improve"),
    ("/improve 再扫一遍", "improve"),
    ("先看看\n/describe\n谢谢", "describe"),
    ("line1\nline2\n/improve", "improve"),
])
def test_extract_command_matches_line_start(body: str, expected: str) -> None:
    assert extract_command(body) == expected


@pytest.mark.parametrize("body", [
    "/reviewed",          # 前缀相似但不是命令
    "看看 /describe 这个函数",
    "请 /improve 一下",
    "描述一下",
    "",
    "   ",
    "/describe的变体",    # \b 不匹配中文字符 → 不应命中
])
def test_extract_command_ignores_non_line_start(body: str) -> None:
    """行首匹配：避免 `/reviewed` 这类词误触发 LLM 调用."""
    assert extract_command(body) is None


def test_extract_command_prefers_first_matching_pattern() -> None:
    """同时含两个命令时，按 COMMAND_PATTERNS 顺序取第一个（describe 优先）."""
    assert extract_command("/describe\n/improve") == "describe"


# ---------------------------------------------------------------------------
# 鉴权
# ---------------------------------------------------------------------------


class _FakeRequest:
    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers


def _set_secret(monkeypatch, value: str) -> None:
    """覆盖 config 的只读字段（Config 禁止实例赋值，只能改类级 property）."""
    from reviewagent.config import config as _cfg

    monkeypatch.setattr(
        type(_cfg), "gitlab_webhook_secret",
        property(lambda self, _v=value: _v),
        raising=False,
    )


@pytest.mark.asyncio
async def test_verify_webhook_token_accepts_matching_token(monkeypatch) -> None:
    from reviewagent.webhook import auth

    _set_secret(monkeypatch, "s3cret")
    await auth.verify_webhook_token(_FakeRequest({"X-Gitlab-Token": "s3cret"}))  # 不抛即通过


@pytest.mark.asyncio
async def test_verify_webhook_token_rejects_missing_header(monkeypatch) -> None:
    from fastapi import HTTPException
    from reviewagent.webhook import auth

    _set_secret(monkeypatch, "s3cret")
    with pytest.raises(HTTPException) as exc:
        await auth.verify_webhook_token(_FakeRequest({}))
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_verify_webhook_token_rejects_wrong_token(monkeypatch) -> None:
    from fastapi import HTTPException
    from reviewagent.webhook import auth

    _set_secret(monkeypatch, "s3cret")
    with pytest.raises(HTTPException) as exc:
        await auth.verify_webhook_token(_FakeRequest({"X-Gitlab-Token": "wrong"}))
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_verify_webhook_token_rejects_when_config_secret_missing(monkeypatch) -> None:
    """配置侧 secret 为空时必须拒绝 —— 不能变成「无密钥即可调用」."""
    from fastapi import HTTPException
    from reviewagent.webhook import auth

    _set_secret(monkeypatch, "")
    with pytest.raises(HTTPException) as exc:
        await auth.verify_webhook_token(_FakeRequest({"X-Gitlab-Token": "anything"}))
    assert exc.value.status_code == 401


# ---------------------------------------------------------------------------
# 限流 / 冷却的 fail-open 语义
# ---------------------------------------------------------------------------


def test_cooldown_fails_open_when_redis_breaks(monkeypatch) -> None:
    """Redis 故障 → 不拦截（放行），而不是把整条链掐死."""
    from reviewagent.webhook import locks

    class _BrokenRedis:
        def set(self, *a, **k):
            raise RuntimeError("redis down")

    monkeypatch.setattr(locks.locks, "_get_redis", lambda: _BrokenRedis())
    assert locks.locks.should_skip_cooldown(1, 2, "describe") is False


def test_max_review_calls_fails_open_when_store_breaks(monkeypatch) -> None:
    """telemetry 故障 → 不拦截，放行并记 0 次."""
    from reviewagent.webhook import locks

    class _BrokenStore:
        def _conn(self):
            raise RuntimeError("db down")

    monkeypatch.setattr(
        "reviewagent.telemetry.store.get_store", lambda: _BrokenStore()
    )
    skip, count = locks.locks.should_skip_max_review_calls(1, 2, ("describe",))
    assert skip is False
    assert count == 0


def test_max_review_calls_zero_limit_never_blocks(monkeypatch) -> None:
    """limit<=0 表示不限 —— 不能当成「零次即达上限」."""
    from reviewagent.webhook import locks

    skip, _count = locks.locks.should_skip_max_review_calls(1, 2, ("describe",), max_calls=0)
    assert skip is False


def test_suggestion_cooldown_is_per_suggestion(monkeypatch) -> None:
    """不同建议的 /dismiss 各自独立计数 —— 60s 内连 dismiss 两条不能吞掉第二条."""
    from reviewagent.webhook import locks

    class _FakeRedis:
        def __init__(self) -> None:
            self.store: dict[str, str] = {}

        def set(self, key, value, ex=None, nx=None):
            if nx and key in self.store:
                return None
            self.store[key] = value
            return True

    fake = _FakeRedis()
    monkeypatch.setattr(locks.locks, "_get_redis", lambda: fake)
    assert locks.locks.should_skip_suggestion_cooldown(1, 2, "dismiss", "note-a") is False
    # 同一建议重复 → 被拦
    assert locks.locks.should_skip_suggestion_cooldown(1, 2, "dismiss", "note-a") is True
    # 另一条建议 → 独立放行
    assert locks.locks.should_skip_suggestion_cooldown(1, 2, "dismiss", "note-b") is False
