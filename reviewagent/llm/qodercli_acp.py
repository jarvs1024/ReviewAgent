"""QoderCLI ACP long-connection client.

Owns one `qodercli --acp` subprocess per RQ worker, multiplexes many
session/prompt calls over its stdin/stdout JSON-RPC 2.0 stream, and
surfaces results as `LLMResult` via `QoderCLIProvider`.

This module deliberately keeps the transport interface small so tests
can swap in `io.BytesIO`. The real subprocess is wired in Task 4.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import queue
import threading
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Protocol


class QoderCLIACPError(RuntimeError):
    """Base class for qodercli ACP failures."""


class QoderCLIAuthError(QoderCLIACPError):
    """QoderCLI authentication failed (token expired, login missing)."""


class QoderCLITimeoutError(QoderCLIACPError):
    """session/prompt exceeded the configured timeout."""


class QoderCLIProtocolError(QoderCLIACPError):
    """ACP server returned a malformed envelope or unknown method."""


_id_lock = threading.Lock()
_id_counter = 0


def _next_id() -> int:
    """Monotonically increasing JSON-RPC id within a process."""
    global _id_counter
    with _id_lock:
        _id_counter += 1
        return _id_counter


class _Writer(Protocol):
    def write(self, data: bytes) -> int: ...
    def flush(self) -> None: ...


@dataclass
class _SubprocessTransport:
    """Default transport backed by `Popen.stdin` (real subprocess)."""

    proc: Any  # subprocess.Popen — kept Any to avoid import at module load.

    def write(self, data: bytes) -> int:  # pragma: no cover - real path
        return self.proc.stdin.write(data)

    def flush(self) -> None:  # pragma: no cover - real path
        self.proc.stdin.flush()


class QoderCLIACPClient:
    """Single-process wrapper around a long-lived `qodercli --acp` server.

    The class is split across Tasks 3, 4 and 5:
    - Task 3 ships `_enqueue` / `_run_send_loop` / `stop` and the
      `transport` injection seam.
    - Task 4 wires `bootstrap()` to actually `Popen` the binary.
    - Task 5 adds `initialize / session/new / session/prompt`.
    """

    def __init__(
        self,
        *,
        node: str,
        script: str,
        model: str,
        extra_args: Iterable[str],
        transport: _Writer,
        pending: "queue.Queue[dict] | None" = None,
    ) -> None:
        self._node = node
        self._script = script
        self._model = model
        self._extra_args = list(extra_args)
        self._transport = transport
        self._send_q: "queue.Queue[dict]" = pending if pending is not None else queue.Queue()
        self._stop = threading.Event()
        self._send_thread: threading.Thread | None = None
        # Filled in by bootstrap() (Task 4).
        self._proc = None
        self._workdir = None
        self._sessions = {}
        self._session_lock = threading.Lock()
        self._recv_thread = None
        self._pending = {}
        self._pending_lock = threading.Lock()
        self._pending_message: dict = {}
        self._pending_message_lock = threading.Lock()

    # ---- send loop (Task 3) ----

    def _enqueue(self, message: dict) -> None:
        """Push a JSON-RPC message onto the outbound queue."""
        self._send_q.put(message)

    def _run_send_loop(self) -> None:
        """Worker thread: serialise queued messages to the transport."""
        while not self._stop.is_set():
            try:
                message = self._send_q.get(timeout=0.2)
            except queue.Empty:
                continue
            line = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
            self._transport.write(line)
            self._transport.flush()

    def start_send_loop(self) -> None:
        if self._send_thread and self._send_thread.is_alive():
            return
        self._send_thread = threading.Thread(
            target=self._run_send_loop, name="qodercli-acp-send", daemon=True
        )
        self._send_thread.start()

    def stop(self, drain_timeout: float = 2.0) -> None:
        """Signal the send loop to exit. If there are still queued
        messages, give the worker `drain_timeout` seconds to flush them
        out so a graceful shutdown does not lose in-flight RPCs."""
        # Wait until the queue is drained (or the stop event is honoured).
        deadline = time.monotonic() + drain_timeout
        while not self._send_q.empty() and time.monotonic() < deadline:
            time.sleep(0.02)
        self._stop.set()
        if self._send_thread:
            self._send_thread.join(timeout=2)

    def shutdown(self) -> None:
        """Terminate the subprocess (if any) and join background threads.

        Safe to call multiple times. Tests typically patch subprocess.Popen
        so the process never really exists; in that case terminate() is a
        no-op and the recv thread exits when its readline yields b""."""
        self.stop()
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception:
                pass
        if self._recv_thread is not None:
            self._recv_thread.join(timeout=2)

    @classmethod
    def bootstrap(
        cls,
        *,
        node: str,
        script: str,
        model: str,
        extra_args: list,
        workdir: Path,
    ):
        cmd = [node, script, "--acp", "-m", model, *extra_args]
        proc = subprocess.Popen(
            cmd,
            cwd=workdir,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        if proc.poll() is not None:
            raise QoderCLIACPError(
                f"qodercli --acp died at start: exit={proc.returncode}"
            )
        client = cls(
            node=node,
            script=script,
            model=model,
            extra_args=extra_args,
            transport=_SubprocessTransport(proc=proc),
        )
        client._proc = proc
        client._workdir = workdir
        client.start_send_loop()
        client._recv_thread = threading.Thread(
            target=client._run_recv_loop, name="qodercli-acp-recv", daemon=True
        )
        client._recv_thread.start()
        return client

    def _register_pending(self, msg_id: int) -> Future:
        fut = Future()
        with self._pending_lock:
            self._pending[msg_id] = fut
        return fut

    def _dispatch_response(self, message: dict) -> None:
        msg_id = message.get("id")
        with self._pending_lock:
            fut = self._pending.pop(msg_id, None)
        if fut is None:
            return
        if "error" in message:
            err = message["error"]
            fut.set_exception(
                QoderCLIProtocolError(
                    f"rpc {msg_id}: {err.get('message')} {err.get('data')}"
                )
            )
        else:
            fut.set_result(message.get("result"))

    # ---- notification routing (Task 6) ----

    def on_notification(self, message: dict) -> None:
        """Receive a JSON-RPC notification and accumulate
        `agent_message_chunk` text per session."""
        if message.get("method") != "session/update":
            return
        params = message.get("params") or {}
        sid = params.get("sessionId")
        update = params.get("update") or {}
        if not sid or update.get("sessionUpdate") != "agent_message_chunk":
            return
        text = (update.get("content") or {}).get("text", "")
        if not text:
            return
        with self._pending_message_lock:
            self._pending_message.setdefault(sid, []).append(text)

    def collect_message(self, session_id: str) -> str:
        with self._pending_message_lock:
            chunks = self._pending_message.pop(session_id, [])
        return "".join(chunks)

    def _run_recv_loop(self) -> None:
        assert self._proc is not None
        for raw in iter(self._proc.stdout.readline, b""):
            if not raw:
                return
            try:
                message = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as e:
                raise QoderCLIProtocolError(
                    f"non-JSON line from qodercli: {raw!r}"
                ) from e
            if "id" in message and ("result" in message or "error" in message):
                self._dispatch_response(message)
            else:
                self.on_notification(message)

    # ---- high-level RPCs (Task 5) ----

    def _request(self, method: str, **params) -> "Future":
        """Send a JSON-RPC request and return the Future that will resolve
        when the matching response arrives."""
        msg_id = _next_id()
        self._enqueue({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params})
        return self._register_pending(msg_id)

    def initialize(self, *, client_info: dict, capabilities: dict) -> dict:
        fut = self._request(
            "initialize",
            protocolVersion=1,
            clientInfo=client_info,
            capabilities=capabilities,
        )
        try:
            return fut.result(timeout=30)
        except QoderCLIProtocolError as e:
            msg = str(e).lower()
            if "auth" in msg or "-32000" in msg or "login" in msg:
                raise QoderCLIAuthError(str(e)) from e
            raise

    def session_new(self, *, cwd: Path, mcp_servers=None) -> str:
        fut = self._request(
            "session/new", cwd=str(cwd), mcpServers=mcp_servers or []
        )
        result = fut.result(timeout=30)
        sid = result.get("sessionId") if isinstance(result, dict) else None
        if not sid:
            raise QoderCLIProtocolError(f"session/new missing sessionId: {result!r}")
        with self._session_lock:
            self._sessions[sid] = {"created_at": time.time(), "agent": None}
        return sid

    def session_prompt(self, session_id: str, text: str, *, timeout: float) -> dict:
        payload = [{"type": "text", "text": text}]
        fut = self._request("session/prompt", sessionId=session_id, prompt=payload)
        try:
            return fut.result(timeout=timeout)
        except QoderCLIProtocolError as e:
            if "timeout" in str(e).lower():
                raise QoderCLITimeoutError(str(e)) from e
            raise
        except TimeoutError as e:
            raise QoderCLITimeoutError(f"session/prompt exceeded {timeout}s") from e

    def session_cancel(self, session_id: str) -> None:
        fut = self._request("session/cancel", sessionId=session_id)
        try:
            fut.result(timeout=10)
        except QoderCLIProtocolError as e:
            # Older QoderCLI builds may not implement session/cancel; ignore.
            if "Method not found" not in str(e):
                raise

    def _reuse_session(self, agent: str, window: float):
        now = time.time()
        with self._session_lock:
            for sid, meta in self._sessions.items():
                if meta.get("agent") == agent and (now - meta.get("last_used", 0)) < window:
                    meta["last_used"] = now
                    return sid
        return None

    def chat(
        self,
        *,
        agent: str,
        prompt: str,
        files,
        timeout: float,
        max_concurrent_sessions: int,
        session_reuse_window: float,
    ) -> dict:
        if not hasattr(self, "_sem") or self._sem is None:
            self._sem = threading.Semaphore(max_concurrent_sessions)
        self._sem.acquire()
        try:
            sid = self._reuse_session(agent, session_reuse_window) or self.session_new(
                cwd=self._workdir or Path.cwd()
            )
            try:
                return self.session_prompt(sid, prompt, timeout=timeout)
            finally:
                with self._session_lock:
                    self._sessions[sid] = {
                        "created_at": self._sessions.get(sid, {}).get("created_at", time.time()),
                        "agent": agent,
                        "last_used": time.time(),
                    }
        finally:
            self._sem.release()
