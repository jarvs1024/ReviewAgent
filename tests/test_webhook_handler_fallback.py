"""Tests for webhook handler robustness: any exception returns 200 + reason.

Background:
    commit 491a16f added top-level except Exception in webhook handler so any
    handler crash returns 200 + reason='handler_crash' instead of 5xx.
    Why: GitLab 5xx → no retry → user clicks ✓ → state stale forever.
    Tests verify: timeout, sync_resolved crash, generic crash all return safe.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _ensure_real_workers_tasks(monkeypatch):
    """test_suggestion_actions.py leaves a fake ModuleType in sys.modules.
    Restore the real reviewagent.workers.tasks module so webhook()'s lazy
    `from reviewagent.workers.tasks import enqueue_mr_chain` works."""
    import importlib
    import sys

    sys.modules.pop("reviewagent.workers.tasks", None)
    real_module = importlib.import_module("reviewagent.workers.tasks")
    sys.modules["reviewagent.workers.tasks"] = real_module
    yield
    # 不还原 — 让后续测试也用真模块 (避免其他 fixture 触发同样污染)


def _make_request(payload):
    """Build a stub Request whose .json() returns the given payload."""
    req = MagicMock()
    req.json = AsyncMock(return_value=payload)
    return req


def _run(coro):
    """Helper: run async coroutine in sync test using a fresh event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_payload(object_kind="note"):
    return {
        "object_kind": object_kind,
        "event_type": object_kind,
        "project": {"id": 34},
        "merge_request": {"iid": 263},
        "object_attributes": {
            "id": 12345,
            "noteable_type": "MergeRequest",
            "type": "DiscussionNote",
            "system": True,
            "note": "marked this discussion as resolved",
        },
        "user": {"username": "tester"},
    }


def test_webhook_timeout_returns_200():
    """asyncio.TimeoutError 必须被 catch, 返回 timeout + 200."""
    from reviewagent.webhook.router import webhook
    payload = _make_payload("note")
    req = _make_request(payload)

    async def fake_handler(*args, **kwargs):
        raise asyncio.TimeoutError()

    with patch("reviewagent.webhook.router._handle_note_hook", side_effect=fake_handler):
        result = _run(webhook(req))

    assert result["status"] == "timeout"
    assert result["object_kind"] == "note"


def test_webhook_handler_crash_returns_200():
    """Handler 抛任意异常, 必须返回 error + 200 (不让 GitLab 重试)."""
    from reviewagent.webhook.router import webhook
    payload = _make_payload("note")
    req = _make_request(payload)

    async def fake_handler(*args, **kwargs):
        raise RuntimeError("DB connection lost")

    with patch("reviewagent.webhook.router._handle_note_hook", side_effect=fake_handler):
        result = _run(webhook(req))

    assert result["status"] == "error"
    assert result["reason"] == "handler_crash"
    assert result["object_kind"] == "note"


def test_webhook_unknown_object_kind_returns_200():
    """object_kind 不在白名单, 也要 200 + ignored (不是 5xx)."""
    from reviewagent.webhook.router import webhook
    payload = _make_payload("build")  # 不支持
    req = _make_request(payload)

    result = _run(webhook(req))

    assert result["status"] == "ignored"
    assert result["object_kind"] == "build"


def test_webhook_push_object_kind_uses_handle_code_change():
    """push webhook 走 _handle_code_change 分支 (verify dispatch)."""
    from reviewagent.webhook.router import webhook
    payload = _make_payload("push")
    req = _make_request(payload)

    with patch("reviewagent.webhook.router._handle_code_change",
               new_callable=AsyncMock) as mock_handler:
        mock_handler.return_value = {"status": "queued", "commands": []}
        result = _run(webhook(req))

    mock_handler.assert_awaited_once()
    assert result["status"] == "queued"
