"""QoderCLIACPClient.bootstrap — Popen + recv loop wiring (Popen is mocked)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from reviewagent.llm.qodercli_acp import (
    QoderCLIACPClient,
    QoderCLIACPError,
)


def _fake_popen(cmd, **kwargs):
    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.stdout = MagicMock()
    proc.stderr = MagicMock()
    proc.poll.return_value = None
    proc.pid = 4242
    # Default: stdout.readline returns EOF sentinel immediately so the
    # recv loop terminates without raising inside the worker thread.
    proc.stdout.readline.side_effect = lambda *a, **kw: b""
    return proc


def test_bootstrap_uses_node_and_script(tmp_path: Path) -> None:
    captured = {}

    def _open(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _fake_popen(cmd, **kwargs)

    with patch("subprocess.Popen", side_effect=_open) as popen_patch:
        client = QoderCLIACPClient.bootstrap(
            node="/usr/bin/node",
            script="/opt/qodercli.js",
            model="DeepSeek-V4-Flash",
            extra_args=["--setting-sources", "project,user,local"],
            workdir=Path("/tmp"),
        )
    assert popen_patch.called
    cmd = captured["cmd"]
    assert cmd[0] == "/usr/bin/node"
    assert cmd[1] == "/opt/qodercli.js"
    assert "--acp" in cmd
    assert "-m" in cmd and "DeepSeek-V4-Flash" in cmd
    assert "--setting-sources" in cmd
    assert "project,user,local" in cmd
    assert captured["kwargs"]["cwd"] == Path("/tmp")
    assert captured["kwargs"]["bufsize"] == 0
    client.shutdown()


def test_bootstrap_process_dies_raises(tmp_path: Path) -> None:
    def _open_dead(cmd, **kwargs):
        proc = _fake_popen(cmd, **kwargs)
        proc.poll.return_value = 1
        return proc

    with patch("subprocess.Popen", side_effect=_open_dead):
        with pytest.raises(QoderCLIACPError, match="died"):
            QoderCLIACPClient.bootstrap(
                node="/usr/bin/node",
                script="/opt/q.js",
                model="DeepSeek-V4-Flash",
                extra_args=[],
                workdir=Path("/tmp"),
            )


def test_recv_loop_resolves_pending_response(tmp_path: Path) -> None:
    """A response on stdout should resolve the matching pending future."""

    def _open(cmd, **kwargs):
        proc = _fake_popen(cmd, **kwargs)
        # Side effect: keep returning b"" (EOF sentinel) so iter terminates
        # without the recv loop blowing up on a MagicMock.
        proc.stdout.readline.side_effect = lambda *a, **kw: b""
        return proc

    with patch("subprocess.Popen", side_effect=_open):
        client = QoderCLIACPClient.bootstrap(
            node="/usr/bin/node",
            script="/opt/q.js",
            model="DeepSeek-V4-Flash",
            extra_args=[],
            workdir=Path("/tmp"),
        )
    future = client._register_pending(1)  # type: ignore[attr-defined]
    client._dispatch_response(  # type: ignore[attr-defined]
        {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
    )
    assert future.result(timeout=1) == {"ok": True}
    client.shutdown()
