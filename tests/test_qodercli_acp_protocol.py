"""QoderCLIACPClient — JSON-RPC encode / send_loop / next_id."""

from __future__ import annotations

import io
import json
import queue
import threading
import time
from dataclasses import dataclass

import pytest

from reviewagent.llm.qodercli_acp import (
    QoderCLIACPClient,
    QoderCLIACPError,
    QoderCLIProtocolError,
    _next_id,
)


@dataclass
class _FakeTransport:
    writes: io.BytesIO
    pids: list[int] = ()

    def write(self, data: bytes) -> int:
        return self.writes.write(data)

    def flush(self) -> None:
        self.writes.flush()


def _make_client() -> tuple[QoderCLIACPClient, _FakeTransport, queue.Queue]:
    writes = io.BytesIO()
    transport = _FakeTransport(writes=writes)
    pending: queue.Queue = queue.Queue()
    client = QoderCLIACPClient(
        node="/usr/bin/true",
        script="/tmp/q.js",
        model="DeepSeek-V4-Flash",
        extra_args=[],
        transport=transport,  # type: ignore[arg-type]
        pending=pending,
    )
    return client, transport, pending


def test_next_id_is_monotonic() -> None:
    assert _next_id() != _next_id()


def test_send_loop_serializes_messages() -> None:
    client, transport, _ = _make_client()
    client._enqueue({"jsonrpc": "2.0", "id": 1, "method": "ping"})
    client._enqueue({"jsonrpc": "2.0", "id": 2, "method": "pong"})
    t = threading.Thread(target=client._run_send_loop, daemon=True)
    t.start()
    time.sleep(0.2)
    client.stop()
    t.join(timeout=2)
    raw = transport.writes.getvalue().decode()
    lines = [json.loads(line) for line in raw.splitlines() if line]
    assert lines == [
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "id": 2, "method": "pong"},
    ]


def test_envelope_is_single_json_line_with_trailing_newline() -> None:
    client, transport, _ = _make_client()
    client._enqueue({"jsonrpc": "2.0", "id": 9, "method": "x"})
    t = threading.Thread(target=client._run_send_loop, daemon=True)
    t.start()
    time.sleep(0.1)
    client.stop()
    t.join(timeout=2)
    raw = transport.writes.getvalue().decode()
    assert raw.endswith("\n")
    assert raw.count("\n") == 1
    payload = json.loads(raw.strip())
    assert payload["id"] == 9
