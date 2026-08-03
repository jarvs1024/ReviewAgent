"""High-level RPCs: initialize / session/new / session/prompt / cancel / chat."""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from reviewagent.llm.qodercli_acp import (
    QoderCLIAuthError,
    QoderCLIACPClient,
    QoderCLIProtocolError,
    QoderCLITimeoutError,
)


def _make_client() -> QoderCLIACPClient:
    transport = MagicMock()
    client = QoderCLIACPClient(
        node="/usr/bin/node",
        script="/opt/q.js",
        model="DeepSeek-V4-Flash",
        extra_args=[],
        transport=transport,
    )
    client._transport = transport
    return client


def _patch_send(client: QoderCLIACPClient):
    """Stub _enqueue so RPCs record messages without spawning threads."""

    captured: list = []

    def _fake(message: dict) -> None:
        captured.append(message)

    client._enqueue = _fake
    return captured


def test_initialize_emits_envelope(monkeypatch) -> None:
    client = _make_client()
    sent = _patch_send(client)
    fake_future = MagicMock()
    fake_future.result.return_value = {"agentCapabilities": {"promptQueueing": True}}
    monkeypatch.setattr(client, "_register_pending", lambda _id: fake_future)
    out = client.initialize(client_info={"name": "ra"}, capabilities={})
    assert sent[0]["method"] == "initialize"
    assert sent[0]["params"]["clientInfo"]["name"] == "ra"
    assert out == {"agentCapabilities": {"promptQueueing": True}}


def test_initialize_raises_auth_on_minus_32000(monkeypatch) -> None:
    client = _make_client()
    _patch_send(client)
    fake_future = MagicMock()
    fake_future.result.side_effect = QoderCLIProtocolError("auth required")
    monkeypatch.setattr(client, "_register_pending", lambda _id: fake_future)
    with pytest.raises(QoderCLIAuthError):
        client.initialize(client_info={}, capabilities={})


def test_session_new_returns_id(monkeypatch) -> None:
    client = _make_client()
    sent = _patch_send(client)
    fake_future = MagicMock()
    fake_future.result.return_value = {"sessionId": "sess-1"}
    monkeypatch.setattr(client, "_register_pending", lambda _id: fake_future)
    sid = client.session_new(cwd=Path("/tmp"), mcp_servers=[])
    assert sid == "sess-1"
    assert sent[0]["method"] == "session/new"
    assert sent[0]["params"]["cwd"] == "/tmp"


def test_session_prompt_times_out(monkeypatch) -> None:
    client = _make_client()
    _patch_send(client)
    # Real Future: never set_result, so result(timeout=...) raises TimeoutError.
    real_future = Future()
    monkeypatch.setattr(client, "_register_pending", lambda _id: real_future)
    with pytest.raises(QoderCLITimeoutError):
        client.session_prompt("sess-1", "hi", timeout=0.1)


def test_chat_acquires_and_releases_semaphore(monkeypatch) -> None:
    client = _make_client()
    _patch_send(client)
    monkeypatch.setattr(client, "session_new", lambda **kw: "sess-1")
    monkeypatch.setattr(
        client, "session_prompt", lambda *a, **kw: {"stop_reason": "end_turn", "text": "{}"}
    )
    semaphore = MagicMock()

    def _acquire(*a, **kw):
        semaphore.acquire_calls += 1
        return True

    def _release(*a, **kw):
        semaphore.release_calls += 1

    semaphore.acquire = MagicMock(side_effect=_acquire)
    semaphore.release = MagicMock(side_effect=_release)
    semaphore.__enter__ = lambda s: s
    semaphore.__exit__ = lambda s, *a: None
    client._sem = semaphore
    result = client.chat(
        agent="improve",
        prompt="review",
        files=[],
        timeout=5.0,
        max_concurrent_sessions=2,
        session_reuse_window=60.0,
    )
    assert result["stop_reason"] == "end_turn"
    assert semaphore.acquire.call_count == 1
    assert semaphore.release.call_count == 1
