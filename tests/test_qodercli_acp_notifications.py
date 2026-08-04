"""Notification routing + LLMResult assembly on the ACP client."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from reviewagent.llm.qodercli_acp import QoderCLIACPClient


def _make_client() -> QoderCLIACPClient:
    client = QoderCLIACPClient(
        node="/usr/bin/node",
        script="/opt/q.js",
        model="DeepSeek-V4-Flash",
        extra_args=[],
        transport=MagicMock(),
    )
    return client


def test_collect_message_concatenates_chunks() -> None:
    client = _make_client()
    sid = "sess-1"
    client.on_notification({
        "method": "session/update",
        "params": {"sessionId": sid, "update": {"sessionUpdate": "agent_message_chunk", "content": {"text": "hello "}}},
    })
    client.on_notification({
        "method": "session/update",
        "params": {"sessionId": sid, "update": {"sessionUpdate": "agent_message_chunk", "content": {"text": "world"}}},
    })
    assert client.collect_message(sid) == "hello world"
    # buffer cleared after collect
    assert client.collect_message(sid) == ""


def test_collect_message_ignores_non_chunk_updates() -> None:
    client = _make_client()
    sid = "sess-1"
    client.on_notification({
        "method": "session/update",
        "params": {"sessionId": sid, "update": {"sessionUpdate": "available_commands_update", "availableCommands": []}},
    })
    client.on_notification({
        "method": "session/update",
        "params": {"sessionId": sid, "update": {"sessionUpdate": "agent_thought_chunk", "content": {"text": "thinking"}}},
    })
    assert client.collect_message(sid) == ""


def test_on_notification_passes_through_non_session_update() -> None:
    client = _make_client()
    # Should not raise.
    client.on_notification({"method": "$/cancel", "params": {"reason": "user"}})
