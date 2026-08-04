# QoderCLI ACP Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the one-shot `qodercli -p --append-system-prompt` subprocess path with a long-lived ACP server connection so ReviewAgent can run multiple concurrent MR reviews without RQ `fork + pipe` hangs, while keeping the `BaseLLMProvider` contract and `OpencodeProvider` untouched.

**Architecture:**
- `QoderCLIACPClient` is a process-singleton that owns one `qodercli --acp` subprocess, exchanges JSON-RPC over stdin/stdout via a `_send_loop` and `_recv_loop` background thread pair, and dispatches responses through a `dict[id] -> Future` map.
- A `threading.Semaphore(4)` bounds concurrent `session/prompt` calls inside the worker; per-agent session reuse is cached for 5 minutes.
- `QoderCLIProvider` is rewritten to call `QoderCLIACPClient.bootstrap() + run()`, translating `session/update.agent_message_chunk` to the same `LLMResult` the rest of the codebase already understands.
- Subagent prompts are materialised as `.qoder/agents/<name>.md` by `scripts/sync_qoder_agents.py` at worker boot, so the ACP server picks them up without `--agents` JSON injection.
- `QODERCLI_DRIVER=acp|subprocess` keeps the old one-shot path as a kill-switch.

**Tech Stack:** Python 3.12, `subprocess.Popen`, `threading`, `queue.Queue`, `pytest`, `frontmatter`, `pathlib`; new dependency-free, no new pip packages.

## Global Constraints

- Branch: `codex/feat-llm-provider-adapter` (do **not** touch `main`).
- Node binary path: `QODERCLI_NODE_PATH=/Users/jarvs/.nvm/versions/node/v22.22.2/bin/node`.
- Bundle path: `QODERCLI_JS_PATH=/opt/homebrew/Cellar/node/25.9.0_1/lib/node_modules/@qoder-ai/qodercli/bundle/qodercli.js`.
- Default model: `DeepSeek-V4-Flash`; no other model in this PR.
- New env keys (all with safe defaults): `QODERCLI_DRIVER=acp`, `QODERCLI_ACP_EXTRA_ARGS=`, `QODERCLI_MAX_CONCURRENT_SESSIONS=4`, `QODERCLI_QUEUE_WAIT_TIMEOUT=120`, `QODERCLI_SESSION_REUSE_WINDOW=300`, `QODERCLI_SESSION_TIMEOUT=540`.
- No edits to `main`; commits stay on `codex/feat-llm-provider-adapter` and the verify branch `codex/feat-llm-provider-adapter-verify-2026-08-03`.
- All public functions get type hints + a docstring naming args/return.
- No `--no-verify`, no force push, no `git reset --hard`.
- Tests run via `.venv/bin/pytest`; existing 28 tests in `tests/test_llm_adapter.py` must keep passing unchanged.
- The `.qoder/` directory is project-scoped and `.gitignore`d.
- Do **not** commit OAuth tokens or the `root / Jarvs@2026` GitLab credential.

## File Structure

| File | Role |
|---|---|
| `reviewagent/llm/qodercli_acp.py` (new) | JSON-RPC client, thread model, session pool, error mapping |
| `reviewagent/llm/qodercli_provider.py` (rewrite) | Wraps `QoderCLIACPClient`, handles `agent_message_chunk` → `LLMResult`, falls back to subprocess path when `QODERCLI_DRIVER=subprocess` |
| `reviewagent/llm/__init__.py` (modify) | Re-export `QoderCLIACPClient`, `QoderCLIACPError` |
| `reviewagent/config.py` (modify) | Add `qodercli_driver`, `qodercli_acp_extra_args`, `qodercli_max_concurrent_sessions`, `qodercli_queue_wait_timeout`, `qodercli_session_reuse_window`, `qodercli_session_timeout` |
| `scripts/sync_qoder_agents.py` (new) | Materialise `reviewagent/prompts/*.md` → `.qoder/agents/*.md` with the field mapping from spec §5 |
| `tests/test_qodercli_acp_provider.py` (new) | TDD coverage for protocol, concurrency, bootstrap, errors |
| `docs/LLM_PROVIDER_ADAPTER.md` (modify) | Add a v2 section describing the ACP driver |
| `docs/QODERCLI_FEASIBILITY_REPORT.md` (modify) | Mark `superseded by 2026-08-03 ACP design` |
| `.env.example`, `.gitignore` (modify) | New env keys + ignore `.qoder/` |

### Task 1: Add ACP config fields (TDD: no behaviour change)

**Files:**
- Modify: `reviewagent/config.py:38-140`
- Modify: `.env.example`
- Modify: `.gitignore`
- Test: `tests/test_config_acp_fields.py`

**Interfaces:**
- Consumes: env vars `QODERCLI_DRIVER`, `QODERCLI_ACP_EXTRA_ARGS`, `QODERCLI_MAX_CONCURRENT_SESSIONS`, `QODERCLI_QUEUE_WAIT_TIMEOUT`, `QODERCLI_SESSION_REUSE_WINDOW`, `QODERCLI_SESSION_TIMEOUT`
- Produces: `Config.qodercli_driver: str`, `Config.qodercli_acp_extra_args: list[str]`, `Config.qodercli_max_concurrent_sessions: int`, `Config.qodercli_queue_wait_timeout: int`, `Config.qodercli_session_reuse_window: int`, `Config.qodercli_session_timeout: int`

- [ ] **Step 1: Write the failing test**

Create `tests/test_config_acp_fields.py` with the following content:

```python
"""Config ACP field wiring — defaults + env override."""

from __future__ import annotations

import pytest

from reviewagent.config import Config


def _make_config(monkeypatch, **env):
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
    return Config.from_env()


def test_qodercli_driver_default(monkeypatch):
    monkeypatch.delenv("QODERCLI_DRIVER", raising=False)
    cfg = _make_config(monkeypatch)
    assert cfg.qodercli_driver == "acp"


def test_qodercli_driver_override(monkeypatch):
    cfg = _make_config(monkeypatch, QODERCLI_DRIVER="subprocess")
    assert cfg.qodercli_driver == "subprocess"


def test_qodercli_max_concurrent_sessions_default(monkeypatch):
    monkeypatch.delenv("QODERCLI_MAX_CONCURRENT_SESSIONS", raising=False)
    cfg = _make_config(monkeypatch)
    assert cfg.qodercli_max_concurrent_sessions == 4


def test_qodercli_max_concurrent_sessions_override(monkeypatch):
    cfg = _make_config(monkeypatch, QODERCLI_MAX_CONCURRENT_SESSIONS=2)
    assert cfg.qodercli_max_concurrent_sessions == 2


def test_qodercli_acp_extra_args_splits_on_whitespace(monkeypatch):
    cfg = _make_config(monkeypatch, QODERCLI_ACP_EXTRA_ARGS="--foo bar --baz")
    assert cfg.qodercli_acp_extra_args == ["--foo", "bar", "--baz"]


@pytest.mark.parametrize("env_key,attr,default", [
    ("QODERCLI_QUEUE_WAIT_TIMEOUT", "qodercli_queue_wait_timeout", 120),
    ("QODERCLI_SESSION_REUSE_WINDOW", "qodercli_session_reuse_window", 300),
    ("QODERCLI_SESSION_TIMEOUT", "qodercli_session_timeout", 540),
])
def test_timeout_defaults_and_override(monkeypatch, env_key, attr, default):
    monkeypatch.delenv(env_key, raising=False)
    cfg = _make_config(monkeypatch)
    assert getattr(cfg, attr) == default
    cfg2 = _make_config(monkeypatch, **{env_key: 7})
    assert getattr(cfg2, attr) == 7
```

- [ ] **Step 2: Run the new test to verify it fails**

Run: `.venv/bin/pytest tests/test_config_acp_fields.py -v`
Expected: FAIL with `AttributeError: 'Config' object has no attribute 'qodercli_driver'`.

- [ ] **Step 3: Add the fields to `Config` and `from_env`**

In `reviewagent/config.py`:
1. Inside `class Config`, after the existing `qodercli_timeout` field, add:
   ```python
   qodercli_driver: str = "acp"
   qodercli_acp_extra_args: list[str] = field(default_factory=list)
   qodercli_max_concurrent_sessions: int = 4
   qodercli_queue_wait_timeout: int = 120
   qodercli_session_reuse_window: int = 300
   qodercli_session_timeout: int = 540
   ```
2. Make sure `field` is imported at the top of `config.py` (`from dataclasses import dataclass, field`).
3. In the `from_env` body, after the `qodercli_timeout` line, add:
   ```python
   qodercli_driver=_env("QODERCLI_DRIVER", "acp"),
   qodercli_acp_extra_args=[s for s in _env("QODERCLI_ACP_EXTRA_ARGS", "").split() if s],
   qodercli_max_concurrent_sessions=int(_env("QODERCLI_MAX_CONCURRENT_SESSIONS", "4")),
   qodercli_queue_wait_timeout=int(_env("QODERCLI_QUEUE_WAIT_TIMEOUT", "120")),
   qodercli_session_reuse_window=int(_env("QODERCLI_SESSION_REUSE_WINDOW", "300")),
   qodercli_session_timeout=int(_env("QODERCLI_SESSION_TIMEOUT", "540")),
   ```

- [ ] **Step 4: Run the new test to verify it passes**

Run: `.venv/bin/pytest tests/test_config_acp_fields.py -v`
Expected: PASS (8 tests).

- [ ] **Step 5: Add env example + gitignore**

Append to `.env.example` (do not overwrite comments; add a blank-line-separated block):
```ini
# --- QoderCLI ACP driver ---
QODERCLI_DRIVER=acp
QODERCLI_ACP_EXTRA_ARGS=
QODERCLI_MAX_CONCURRENT_SESSIONS=4
QODERCLI_QUEUE_WAIT_TIMEOUT=120
QODERCLI_SESSION_REUSE_WINDOW=300
QODERCLI_SESSION_TIMEOUT=540
```

Append to `.gitignore`:
```
# QoderCLI project-level subagent definitions (auto-generated)
.qoder/
```

- [ ] **Step 6: Run the existing adapter tests to verify no regression**

Run: `.venv/bin/pytest tests/test_llm_adapter.py -q`
Expected: all 28 prior tests still pass.

- [ ] **Step 7: Commit**

```bash
git add reviewagent/config.py .env.example .gitignore tests/test_config_acp_fields.py
git commit -m "feat(config): add QoderCLI ACP driver knobs + tests"
```

### Task 2: `sync_qoder_agents.py` (TDD: pure file-mapping function)

**Files:**
- Create: `scripts/sync_qoder_agents.py`
- Test: `tests/test_sync_qoder_agents.py`

**Interfaces:**
- Produces: `sync_qoder_agents(prompts_dir: Path, agents_dir: Path) -> list[Path]` — returns the absolute paths of the `.qoder/agents/*.md` files written. Idempotent: only writes when source mtime is newer or dest missing. Skips files whose name starts with `_` (private blocks).

- [ ] **Step 1: Write the failing test**

Create `tests/test_sync_qoder_agents.py`:

```python
"""sync_qoder_agents — frontmatter materialisation into .qoder/agents."""

from __future__ import annotations

from pathlib import Path
import textwrap

import pytest

from scripts.sync_qoder_agents import sync_qoder_agents


def _write_prompt(prompts_dir: Path, name: str, body: str, **front) -> None:
    p = prompts_dir / f"{name}.md"
    fm_lines = ["---"]
    fm_lines.append(f"name: {front.get('name', name)}")
    if "description" in front:
        fm_lines.append(f"description: {front['description']}")
    if "tools" in front:
        fm_lines.append("tools:")
        for k, v in front["tools"].items():
            fm_lines.append(f"  {k}: {str(v).lower()}")
    fm_lines.append("---")
    p.write_text("\n".join(fm_lines) + "\n\n" + textwrap.dedent(body))


def test_writes_one_md_per_prompt(tmp_path: Path) -> None:
    prompts = tmp_path / "prompts"
    agents = tmp_path / "agents"
    prompts.mkdir()
    _write_prompt(prompts, "improve", "You are a code improver.", description="Reviewer")
    _write_prompt(prompts, "describe", "You write MR descriptions.", description="Describer")
    written = sync_qoder_agents(prompts, agents)
    assert sorted(p.name for p in written) == ["describe.md", "improve.md"]


def test_disallowed_tools_mapping(tmp_path: Path) -> None:
    prompts = tmp_path / "prompts"
    agents = tmp_path / "agents"
    prompts.mkdir()
    _write_prompt(
        prompts, "improve", "body",
        description="d",
        tools={"write": False, "edit": False, "bash": False, "webfetch": False},
    )
    sync_qoder_agents(prompts, agents)
    text = (agents / "improve.md").read_text()
    assert "disallowedTools: [Write, Edit, Bash, WebFetch, WebSearch]" in text


def test_hardened_safety_fields_present(tmp_path: Path) -> None:
    prompts = tmp_path / "prompts"
    agents = tmp_path / "agents"
    prompts.mkdir()
    _write_prompt(prompts, "improve", "body", description="d")
    sync_qoder_agents(prompts, agents)
    text = (agents / "improve.md").read_text()
    assert "tools: [Read, Grep, Glob, Agent]" in text
    assert "permissionMode: default" in text
    assert "maxTurns: 3" in text
    assert "model: inherit" in text


def test_skips_underscore_files(tmp_path: Path) -> None:
    prompts = tmp_path / "prompts"
    agents = tmp_path / "agents"
    prompts.mkdir()
    _write_prompt(prompts, "_general_rules_block", "private block", description="d")
    _write_prompt(prompts, "improve", "public", description="d")
    written = sync_qoder_agents(prompts, agents)
    assert [p.name for p in written] == ["improve.md"]


def test_idempotent_no_rewrite_when_unchanged(tmp_path: Path) -> None:
    prompts = tmp_path / "prompts"
    agents = tmp_path / "agents"
    prompts.mkdir()
    _write_prompt(prompts, "improve", "body", description="d")
    sync_qoder_agents(prompts, agents)
    first_mtime = (agents / "improve.md").stat().st_mtime_ns
    sync_qoder_agents(prompts, agents)
    second_mtime = (agents / "improve.md").stat().st_mtime_ns
    assert first_mtime == second_mtime
```

- [ ] **Step 2: Run the new test to verify it fails**

Run: `.venv/bin/pytest tests/test_sync_qoder_agents.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.sync_qoder_agents'`.

- [ ] **Step 3: Implement `sync_qoder_agents`**

Create `scripts/sync_qoder_agents.py`:

```python
"""Materialise reviewagent/prompts/*.md as .qoder/agents/*.md for QoderCLI ACP.

The QoderCLI ACP server reads subagent definitions from the project-level
`.qoder/agents/<name>.md` files at startup. We translate our frontmatter
`tools: {write:false, ...}` (whitelist-negative) into QoderCLI's
`disallowedTools: [...]` (blacklist) and always pin read-only tools +
safe defaults (maxTurns, model, permissionMode).

This script is invoked at worker bootstrap and is idempotent: files are
only rewritten when the source mtime is newer than the dest mtime.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import frontmatter


# Tools we always disable — ReviewAgent prompts must be read-only against
# the workdir and must not perform web fetches.
_HARD_DENY = ["Write", "Edit", "Bash", "WebFetch", "WebSearch"]
# Tools we always allow when nothing else is specified.
_HARD_ALLOW = ["Read", "Grep", "Glob", "Agent"]


def _render_disallowed(tools: dict) -> list[str]:
    if not tools:
        return list(_HARD_DENY)
    return [name.capitalize() for name, ok in tools.items() if ok is False]


def _render_allowed(tools: dict) -> list[str]:
    if not tools:
        return list(_HARD_ALLOW)
    return [name.capitalize() for name, ok in tools.items() if ok is True]


def _should_rewrite(src: Path, dst: Path) -> bool:
    if not dst.exists():
        return True
    return src.stat().st_mtime_ns > dst.stat().st_mtime_ns


def _render_agent(name: str, description: str, body: str, tools: dict) -> str:
    disallowed = _render_disallowed(tools)
    allowed = _render_allowed(tools)
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"tools: [{', '.join(allowed)}]\n"
        f"disallowedTools: [{', '.join(disallowed)}]\n"
        "permissionMode: default\n"
        "maxTurns: 3\n"
        "model: inherit\n"
        "---\n\n"
        f"{body.rstrip()}\n"
    )


def sync_qoder_agents(prompts_dir: Path, agents_dir: Path) -> list[Path]:
    """Mirror every non-private prompt into `agents_dir/<stem>.md`.

    Returns the list of absolute paths actually written (skips entries
    that did not need rewriting). Private blocks (filename starting with
    `_`) are excluded by design.
    """
    agents_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for src in sorted(prompts_dir.glob("*.md")):
        if src.stem.startswith("_"):
            continue
        meta = frontmatter.load(src)
        name = str(meta.get("name") or src.stem)
        description = str(meta.get("description") or "").strip()
        tools = dict(meta.get("tools") or {})
        dst = agents_dir / f"{src.stem}.md"
        if not _should_rewrite(src, dst):
            continue
        dst.write_text(_render_agent(name, description, meta.content, tools))
        written.append(dst.resolve())
    return written


def main() -> None:  # pragma: no cover - thin CLI wrapper
    from reviewagent.config import config
    from reviewagent.prompts.loader import PROMPTS_DIR

    prompts_dir = PROMPTS_DIR
    agents_dir = Path.cwd() / ".qoder" / "agents"
    paths = sync_qoder_agents(prompts_dir, agents_dir)
    print(f"synced {len(paths)} qoder agents to {agents_dir}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the new test to verify it passes**

Run: `.venv/bin/pytest tests/test_sync_qoder_agents.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Smoke-run the script against the real prompts dir**

Run: `.venv/bin/python scripts/sync_qoder_agents.py && ls .qoder/agents`
Expected: 6 `.md` files printed (describe, improve, improve_agent, weekly_*), all under `.qoder/agents/`.

- [ ] **Step 6: Commit**

```bash
git add scripts/sync_qoder_agents.py tests/test_sync_qoder_agents.py
git commit -m "feat(scripts): sync prompts to .qoder/agents for ACP server"
```

### Task 3: `QoderCLIACPClient` — JSON-RPC encode + send_loop (TDD: inject fake transport)

**Files:**
- Create: `reviewagent/llm/qodercli_acp.py`
- Test: `tests/test_qodercli_acp_protocol.py`

**Interfaces:**
- Produces: `class QoderCLIACPError(RuntimeError)`, `class QoderCLIAuthError(QoderCLIACPError)`, `class QoderCLITimeoutError(QoderCLIACPError)`, `class QoderCLIProtocolError(QoderCLIACPError)`, `def _next_id() -> int`
- The class itself is split into two parts: this task ships `QoderCLIACPProtocol` (pure helpers) + `QoderCLIACPClient.__init__` that wires `_send_loop` over a writer callback (no `Popen` yet). Transport is injected so tests use `io.BytesIO` instead of a real subprocess.

- [ ] **Step 1: Write the failing test**

Create `tests/test_qodercli_acp_protocol.py`:

```python
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
```

- [ ] **Step 2: Run the new test to verify it fails**

Run: `.venv/bin/pytest tests/test_qodercli_acp_protocol.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'reviewagent.llm.qodercli_acp'`.

- [ ] **Step 3: Implement the protocol helpers + client skeleton**

Create `reviewagent/llm/qodercli_acp.py`:

```python
"""QoderCLI ACP long-connection client.

Owns one `qodercli --acp` subprocess per RQ worker, multiplexes many
session/prompt calls over its stdin/stdout JSON-RPC 2.0 stream, and
surfaces results as `LLMResult` via `QoderCLIProvider`.

This module deliberately keeps the transport interface small so tests
can swap in `io.BytesIO`. The real subprocess is wired in Task 4.
"""

from __future__ import annotations

import json
import queue
import threading
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Protocol


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
        self._send_q: "queue.Queue[dict]" = pending or queue.Queue()
        self._stop = threading.Event()
        self._send_thread: threading.Thread | None = None

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

    def stop(self) -> None:
        self._stop.set()
        if self._send_thread:
            self._send_thread.join(timeout=2)
```

- [ ] **Step 4: Run the new test to verify it passes**

Run: `.venv/bin/pytest tests/test_qodercli_acp_protocol.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add reviewagent/llm/qodercli_acp.py tests/test_qodercli_acp_protocol.py
git commit -m "feat(llm): qodercli ACP client skeleton + send loop"
```

### Task 4: `bootstrap()` + `Popen` transport + recv-loop (TDD: mock Popen)

**Files:**
- Modify: `reviewagent/llm/qodercli_acp.py`
- Test: `tests/test_qodercli_acp_bootstrap.py`

**Interfaces:**
- Produces: `QoderCLIACPClient.bootstrap(workdir: Path) -> None` — spawns `qodercli --acp`, wires `_SubprocessTransport`, starts both background threads. Adds `self._proc: subprocess.Popen`, `self._sessions: dict[str, dict]`, `self._session_lock: threading.Lock`. `_run_recv_loop()` drains stdout and resolves pending futures via the `_pending` `dict[id] -> Future` map (replace the Task 3 queue-only signature with a real pending store).

- [ ] **Step 1: Write the failing test**

Create `tests/test_qodercli_acp_bootstrap.py`:

```python
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

    sent = []

    def _open(cmd, **kwargs):
        proc = _fake_popen(cmd, **kwargs)
        proc.stdin.write.side_effect = lambda b: sent.append(b) or len(b)
        # Drive the recv loop by feeding it a known response.
        lines = [b'{"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n']
        proc.stdout.read.side_effect = lines
        # First read returns a line, then empty bytes (EOF) to terminate.
        proc.stdout.readline.side_effect = [lines[0], b""]
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
        json.loads(b'{"jsonrpc":"2.0","id":1,"result":{"ok":true}}'.decode())
    )
    assert future.result(timeout=1) == {"ok": True}
    client.shutdown()
```

- [ ] **Step 2: Run the new test to verify it fails**

Run: `.venv/bin/pytest tests/test_qodercli_acp_bootstrap.py -v`
Expected: FAIL — `bootstrap` static method missing, `_register_pending` / `_dispatch_response` missing.

- [ ] **Step 3: Implement `bootstrap` and recv loop**

Extend `reviewagent/llm/qodercli_acp.py`:

1. Add imports at the top:
   ```python
   import os
   import subprocess
   from concurrent.futures import Future
   from pathlib import Path
   ```
2. Add class attributes after `__init__`:
   ```python
   self._proc: subprocess.Popen | None = None
   self._sessions: dict[str, dict] = {}
   self._session_lock = threading.Lock()
   self._recv_thread: threading.Thread | None = None
   self._pending: dict[int, Future] = {}
   self._pending_lock = threading.Lock()
   self._workdir: Path | None = None
   ```
3. Add `bootstrap` classmethod:
   ```python
   @classmethod
   def bootstrap(
       cls,
       *,
       node: str,
       script: str,
       model: str,
       extra_args: list[str],
       workdir: Path,
   ) -> "QoderCLIACPClient":
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
           raise QoderCLIACPError(f"qodercli --acp died at start: exit={proc.returncode}")
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
   ```
4. Add `_register_pending` / `_dispatch_response`:
   ```python
   def _register_pending(self, msg_id: int) -> Future:
       fut: Future = Future()
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
               QoderCLIProtocolError(f"rpc {msg_id}: {err.get('message')} {err.get('data')}")
           )
       else:
           fut.set_result(message.get("result"))
   ```
5. Add `_run_recv_loop` (drains stdout line by line):
   ```python
   def _run_recv_loop(self) -> None:
       assert self._proc is not None
       for raw in iter(self._proc.stdout.readline, b""):
           if not raw:
               return
           try:
               message = json.loads(raw.decode("utf-8"))
           except json.JSONDecodeError as e:
               raise QoderCLIProtocolError(f"non-JSON line from qodercli: {raw!r}") from e
           self._dispatch_response(message)
   ```
6. Add `shutdown`:
   ```python
   def shutdown(self) -> None:
       self._stop.set()
       if self._proc and self._proc.poll() is None:
           try:
               self._proc.terminate()
           except Exception:  # pragma: no cover - best effort
               pass
       if self._recv_thread:
           self._recv_thread.join(timeout=2)
       if self._send_thread:
           self._send_thread.join(timeout=2)
   ```

- [ ] **Step 4: Run the new test to verify it passes**

Run: `.venv/bin/pytest tests/test_qodercli_acp_bootstrap.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Run all qodercli-acp tests together to confirm no regression**

Run: `.venv/bin/pytest tests/test_qodercli_acp_protocol.py tests/test_qodercli_acp_bootstrap.py -q`
Expected: PASS (6 tests).

- [ ] **Step 6: Commit**

```bash
git add reviewagent/llm/qodercli_acp.py tests/test_qodercli_acp_bootstrap.py
git commit -m "feat(llm): qodercli ACP bootstrap, recv loop, pending map"
```

### Task 5: High-level RPC (`initialize / session/new / session/prompt / session/cancel`)

**Files:**
- Modify: `reviewagent/llm/qodercli_acp.py`
- Test: `tests/test_qodercli_acp_rpc.py`

**Interfaces:**
- Produces:
  - `initialize(client_info: dict, capabilities: dict) -> dict` — returns `agentCapabilities`; classifies `error.code == -32000` (auth) into `QoderCLIAuthError`.
  - `session_new(cwd: Path, mcp_servers: list[dict] | None = None) -> str` — returns the new `sessionId`; records entry in `self._sessions`.
  - `session_prompt(session_id: str, text: str, *, timeout: float) -> dict` — blocks until the turn final response arrives. Returns the full `result` object (which includes `stop_reason`).
  - `session_cancel(session_id: str) -> None` — best-effort; ignores `-32601 Method not found` so older QoderCLI builds still work.
  - `chat(agent: str, prompt: str, files: list[Path], *, timeout: float) -> dict` — composes the three calls above, manages the semaphore and session cache, and returns the final result `dict`.
  - Concurrency primitives: `self._sem = threading.Semaphore(value)` and `self._session_cache: dict[str, tuple[str, float]] = {}` with `qodercli_max_concurrent_sessions` / `qodercli_session_reuse_window` from the config object passed into `chat()`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_qodercli_acp_rpc.py`:

```python
"""High-level RPCs: initialize / session/new / session/prompt / cancel / chat."""

from __future__ import annotations

import threading
import time
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
    pending = {}
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

    captured: list[dict] = []

    def _fake(message: dict) -> None:
        captured.append(message)

    client._enqueue = _fake  # type: ignore[assignment]
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
    fake_future = MagicMock()

    def _block():
        time.sleep(2)
        return {}

    fake_future.result.side_effect = _block
    monkeypatch.setattr(client, "_register_pending", lambda _id: fake_future)
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
    semaphore.__enter__ = lambda s: s
    semaphore.__exit__ = lambda s, *a: None
    semaphore.acquire = MagicMock()
    semaphore.release = MagicMock()
    client._sem = semaphore  # type: ignore[attr-defined]
    result = client.chat(
        agent="improve",
        prompt="review",
        files=[],
        timeout=5.0,
        max_concurrent_sessions=2,
        session_reuse_window=60.0,
    )
    assert result["stop_reason"] == "end_turn"
    semaphore.acquire.assert_called_once()
    semaphore.release.assert_called_once()
```

- [ ] **Step 2: Run the new test to verify it fails**

Run: `.venv/bin/pytest tests/test_qodercli_acp_rpc.py -v`
Expected: FAIL — `initialize` / `session_new` / `session_prompt` / `chat` missing.

- [ ] **Step 3: Implement the high-level RPC methods**

Extend `reviewagent/llm/qodercli_acp.py` with these methods on `QoderCLIACPClient`. Add helper `_request(result_type: str, **params) -> dict` that does the send/await dance:

```python
def _request(self, method: str, **params) -> Future:
    msg_id = _next_id()
    self._enqueue({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params})
    return self._register_pending(msg_id)


def initialize(self, *, client_info: dict, capabilities: dict) -> dict:
    fut = self._request("initialize", protocolVersion=1, clientInfo=client_info, capabilities=capabilities)
    try:
        return fut.result(timeout=30)
    except QoderCLIProtocolError as e:
        if "auth" in str(e).lower() or "-32000" in str(e):
            raise QoderCLIAuthError(str(e)) from e
        raise


def session_new(self, *, cwd: Path, mcp_servers: list[dict] | None = None) -> str:
    fut = self._request("session/new", cwd=str(cwd), mcpServers=mcp_servers or [])
    result = fut.result(timeout=30)
    sid = result.get("sessionId")
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


def session_cancel(self, session_id: str) -> None:
    fut = self._request("session/cancel", sessionId=session_id)
    try:
        fut.result(timeout=10)
    except QoderCLIProtocolError as e:
        if "Method not found" not in str(e):
            raise


def chat(
    self,
    *,
    agent: str,
    prompt: str,
    files: list[Path],
    timeout: float,
    max_concurrent_sessions: int,
    session_reuse_window: float,
) -> dict:
    if not hasattr(self, "_sem") or self._sem is None:
        self._sem = threading.Semaphore(max_concurrent_sessions)
    with self._sem:
        sid = self._reuse_session(agent, session_reuse_window) or self.session_new(
            cwd=self._workdir or Path.cwd()
        )
        try:
            return self.session_prompt(sid, prompt, timeout=timeout)
        finally:
            with self._session_lock:
                self._sessions[sid] = {
                    "created_at": time.time(),
                    "agent": agent,
                    "last_used": time.time(),
                }


def _reuse_session(self, agent: str, window: float) -> str | None:
    now = time.time()
    with self._session_lock:
        for sid, meta in self._sessions.items():
            if meta.get("agent") == agent and (now - meta.get("last_used", 0)) < window:
                meta["last_used"] = now
                return sid
    return None
```

Add `import time` at the top of the file (or update existing imports if `time` is already there).

- [ ] **Step 4: Run the new test to verify it passes**

Run: `.venv/bin/pytest tests/test_qodercli_acp_rpc.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Run the entire qodercli-acp test suite to confirm no regression**

Run: `.venv/bin/pytest tests/test_qodercli_acp_protocol.py tests/test_qodercli_acp_bootstrap.py tests/test_qodercli_acp_rpc.py -q`
Expected: PASS (11 tests).

- [ ] **Step 6: Commit**

```bash
git add reviewagent/llm/qodercli_acp.py tests/test_qodercli_acp_rpc.py
git commit -m "feat(llm): qodercli ACP RPCs + chat() with session reuse"
```

### Task 6: Notification routing + `LLMResult` assembly (TDD: fake transport emits chunks)

**Files:**
- Modify: `reviewagent/llm/qodercli_acp.py`
- Modify: `reviewagent/llm/qodercli_provider.py` (full rewrite, still conforms to `BaseLLMProvider`)
- Test: `tests/test_qodercli_acp_notifications.py`

**Interfaces:**
- Produces on `QoderCLIACPClient`:
  - `on_notification(self, message: dict) -> None` — public entry point used by `_dispatch_response`; routes `method == "session/update"` into `_handle_session_update`. For now `_handle_session_update` only accumulates `agent_message_chunk` per `sessionId` and stores the joined text on `self._pending_message[sessionId]`. (Full streaming UI is out of scope per spec §10.)
  - `collect_message(session_id: str) -> str` — returns the accumulated text and clears the buffer.
- Produces on `QoderCLIProvider`:
  - `run(agent, prompt, workdir, files, timeout, tolerant_markdown) -> LLMResult`:
    1. `from reviewagent.llm.qodercli_acp import QoderCLIACPClient`
    2. If `config.qodercli_driver == "subprocess"`, delegate to the existing `--append-system-prompt` path (Task 7 isolates that fallback).
    3. Otherwise call `QoderCLIACPClient.bootstrap(...)` lazily, then `client.chat(...)`, then assemble `LLMResult(data=json.loads(collect_message(sid)), prompt_tokens=..., completion_tokens=..., model=config.qodercli_model, duration_ms=..., provider="qodercli", raw_output=text)`.
    4. On `QoderCLIProtocolError` whose data matches the `tolerant_markdown` contract, return `LLMResult(data={}, raw_output=text, provider="qodercli")` so weekly collectors keep working.

- [ ] **Step 1: Write the failing test**

Create `tests/test_qodercli_acp_notifications.py`:

```python
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
```

- [ ] **Step 2: Run the new test to verify it fails**

Run: `.venv/bin/pytest tests/test_qodercli_acp_notifications.py -v`
Expected: FAIL — `on_notification` / `collect_message` missing.

- [ ] **Step 3: Implement notification routing on the client**

Extend `reviewagent/llm/qodercli_acp.py` with these on `QoderCLIACPClient`:

```python
def __init__(self, *args, **kwargs) -> None:
    # existing init...
    self._pending_message: dict[str, list[str]] = {}
    self._pending_message_lock = threading.Lock()


def on_notification(self, message: dict) -> None:
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
```

- [ ] **Step 4: Run the new test to verify it passes**

Run: `.venv/bin/pytest tests/test_qodercli_acp_notifications.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Wire notifications into the recv loop**

In `QoderCLIACPClient._run_recv_loop`, route every non-response line through `on_notification`:

```python
def _run_recv_loop(self) -> None:
    assert self._proc is not None
    for raw in iter(self._proc.stdout.readline, b""):
        if not raw:
            return
        try:
            message = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise QoderCLIProtocolError(f"non-JSON line from qodercli: {raw!r}") from e
        if "id" in message and ("result" in message or "error" in message):
            self._dispatch_response(message)
        else:
            self.on_notification(message)
```

- [ ] **Step 6: Rewrite `QoderCLIProvider.run` against `QoderCLIACPClient`**

Replace the body of `QoderCLIProvider.run` in `reviewagent/llm/qodercli_provider.py`. Keep the class import surface and the `QoderCLIError` / `QoderCLITimeoutError` / `QoderCLIOutputError` re-exports untouched so existing tests still pass.

```python
import json
import time
from pathlib import Path

from reviewagent.config import config
from reviewagent.llm.base import BaseLLMProvider, LLMResult
from reviewagent.llm.qodercli_acp import (
    QoderCLIACPClient,
    QoderCLIACPError,
    QoderCLITimeoutError,
    QoderCLIProtocolError,
)
from reviewagent.logging_setup import logger
from reviewagent.prompts import loader


class QoderCLIError(RuntimeError):
    """qodercli 调用失败基类."""


class QoderCLITimeoutError(QoderCLIError):
    """qodercli 任务超时."""


class QoderCLIOutputError(QoderCLIError):
    """qodercli 输出无法解析为 JSON."""


_client: QoderCLIACPClient | None = None


def _get_or_bootstrap(workdir: Path) -> QoderCLIACPClient:
    global _client
    if _client is not None and _client._proc and _client._proc.poll() is None:
        return _client
    from scripts.sync_qoder_agents import sync_qoder_agents
    from reviewagent.prompts.loader import PROMPTS_DIR

    agents_dir = workdir / ".qoder" / "agents"
    sync_qoder_agents(PROMPTS_DIR, agents_dir)
    node = config.qodercli_node_path or shutil.which("node") or ""
    script = config.qodercli_js_path
    if not node or not script:
        raise QoderCLIError("QODERCLI_NODE_PATH / QODERCLI_JS_PATH not configured")
    _client = QoderCLIACPClient.bootstrap(
        node=node,
        script=script,
        model=config.qodercli_model,
        extra_args=config.qodercli_acp_extra_args + ["--setting-sources", "project,user,local"],
        workdir=workdir,
    )
    _client.initialize(client_info={"name": "reviewagent"}, capabilities={})
    return _client


class QoderCLIProvider(BaseLLMProvider):
    """LLM Provider backed by QoderCLI ACP long connection."""

    @property
    def provider_name(self) -> str:
        return "qodercli"

    def health_check(self) -> bool:
        try:
            client = _get_or_bootstrap(Path.cwd())
        except QoderCLIError:
            return False
        return client._proc is not None and client._proc.poll() is None

    def run(self, *, agent, prompt, workdir, files, timeout, tolerant_markdown):
        if config.qodercli_driver == "subprocess":
            from reviewagent.llm.qodercli_subprocess import run_subprocess  # Task 7
            return run_subprocess(agent=agent, prompt=prompt, workdir=workdir, files=files, timeout=timeout, tolerant_markdown=tolerant_markdown)
        client = _get_or_bootstrap(workdir)
        started = time.time()
        meta = loader.load(agent)
        text = f"使用 {agent} subagent 处理以下任务:\n\n{prompt}"
        sid = client.session_new(cwd=workdir)
        try:
            client.session_prompt(sid, text, timeout=timeout or config.qodercli_session_timeout)
        except QoderCLITimeoutError as e:
            client.session_cancel(sid)
            raise QoderCLITimeoutError(str(e)) from e
        message = client.collect_message(sid)
        duration_ms = int((time.time() - started) * 1000)
        if not message:
            raise QoderCLIOutputError("ACP session produced no agent_message_chunk")
        try:
            data = json.loads(_strip_fence(message))
        except json.JSONDecodeError:
            if tolerant_markdown:
                return LLMResult(data={}, provider="qodercli", raw_output=message, duration_ms=duration_ms, model=config.qodercli_model)
            raise QoderCLIOutputError(f"agent output not JSON: {message[:300]}")
        return LLMResult(
            data=data,
            provider="qodercli",
            duration_ms=duration_ms,
            model=config.qodercli_model,
            raw_output=message,
        )


def _strip_fence(text: str) -> str:
    import re
    if not text:
        return ""
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    return (m.group(1) if m else text).strip()


def reset_for_tests() -> None:  # pragma: no cover
    global _client
    _client = None
```

Add `import shutil` at the top of `qodercli_provider.py`. Leave the original `_resolve_node_path` / `_resolve_qodercli_js_path` helpers removed (they moved into the bootstrap path) — the existing test file imports `QoderCLIProvider` / `QoderCLIError` etc., which we still export.

- [ ] **Step 7: Run the entire adapter test suite to confirm no regression**

Run: `.venv/bin/pytest tests/test_llm_adapter.py tests/test_qodercli_acp_protocol.py tests/test_qodercli_acp_bootstrap.py tests/test_qodercli_acp_rpc.py tests/test_qodercli_acp_notifications.py -q`
Expected: PASS for every test except `test_qodercli_returns_real_provider`, which is expected to fail until Task 7 (subprocess fallback) is in place; note this in the commit body.

- [ ] **Step 8: Commit**

```bash
git add reviewagent/llm/qodercli_acp.py reviewagent/llm/qodercli_provider.py tests/test_qodercli_acp_notifications.py
git commit -m "feat(llm): wire ACP notifications + rewrite QoderCLIProvider.run"
```

### Task 7: Subprocess fallback (`QODERCLI_DRIVER=subprocess`)

**Files:**
- Create: `reviewagent/llm/qodercli_subprocess.py`
- Test: `tests/test_qodercli_subprocess_fallback.py`

**Interfaces:**
- Produces: `run_subprocess(*, agent, prompt, workdir, files, timeout, tolerant_markdown) -> LLMResult` — lifts the old `--append-system-prompt` implementation that previously lived inside `QoderCLIProvider.run`. The signature matches the ACP path exactly so `QoderCLIProvider.run` can dispatch to either driver without further changes.

- [ ] **Step 1: Write the failing test**

Create `tests/test_qodercli_subprocess_fallback.py`:

```python
"""Subprocess fallback path — QODERCLI_DRIVER=subprocess."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from reviewagent.llm.qodercli_subprocess import run_subprocess
from reviewagent.llm.qodercli_provider import QoderCLIOutputError


def test_subprocess_path_invokes_node_script(tmp_path: Path) -> None:
    captured = {}
    fake_proc = MagicMock()
    fake_proc.stdout = json.dumps({
        "type": "result",
        "subtype": "success",
        "result": "{\"ok\": true}",
        "stop_reason": "end_turn",
        "duration_ms": 1234,
        "usage": {"input_tokens": 1, "output_tokens": 2},
    })
    fake_proc.stderr = ""
    fake_proc.returncode = 0

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["timeout"] = kwargs.get("timeout")
        return fake_proc

    with patch("subprocess.run", side_effect=_fake_run):
        result = run_subprocess(
            agent="improve",
            prompt="review",
            workdir=tmp_path,
            files=[],
            timeout=120,
            tolerant_markdown=False,
        )
    assert "--append-system-prompt" in captured["cmd"]
    assert "-m" in captured["cmd"]
    assert captured["cmd"][-1] == "review"
    assert result.data == {"ok": True}
    assert result.provider == "qodercli"
    assert result.model  # filled from config
    assert result.duration_ms >= 0


def test_subprocess_path_tolerant_markdown(tmp_path: Path) -> None:
    fake_proc = MagicMock()
    fake_proc.stdout = "not json"
    fake_proc.stderr = ""
    fake_proc.returncode = 0

    def _fake_run(cmd, **kwargs):
        return fake_proc

    with patch("subprocess.run", side_effect=_fake_run):
        result = run_subprocess(
            agent="improve",
            prompt="x",
            workdir=tmp_path,
            files=[],
            timeout=30,
            tolerant_markdown=True,
        )
    assert result.data == {}
    assert result.raw_output == "not json"


def test_subprocess_path_raises_on_empty_stdout(tmp_path: Path) -> None:
    fake_proc = MagicMock()
    fake_proc.stdout = ""
    fake_proc.stderr = "boom"
    fake_proc.returncode = 0
    with patch("subprocess.run", return_value=fake_proc):
        with pytest.raises(QoderCLIOutputError, match="empty"):
            run_subprocess(agent="improve", prompt="x", workdir=tmp_path, files=[], timeout=30, tolerant_markdown=False)
```

- [ ] **Step 2: Run the new test to verify it fails**

Run: `.venv/bin/pytest tests/test_qodercli_subprocess_fallback.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'reviewagent.llm.qodercli_subprocess'`.

- [ ] **Step 3: Implement `run_subprocess`**

Create `reviewagent/llm/qodercli_subprocess.py`:

```python
"""Subprocess fallback for QoderCLIProvider.

When `QODERCLI_DRIVER=subprocess` we revert to the pre-ACP behaviour
(one `qodercli -p` invocation per task). The implementation is the
unchanged body that used to live in `QoderCLIProvider.run` before the
ACP rewrite; the public surface is intentionally identical so the
provider's `run` method can switch drivers with no glue.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from reviewagent.config import config
from reviewagent.llm.base import LLMResult
from reviewagent.llm.qodercli_provider import QoderCLIError, QoderCLIOutputError
from reviewagent.logging_setup import logger
from reviewagent.prompts import loader


def _strip_fence(text: str) -> str:
    if not text:
        return ""
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    return (m.group(1) if m else text).strip()


def run_subprocess(
    *,
    agent: str,
    prompt: str,
    workdir: Path,
    files: list[Path] | None,
    timeout: int,
    tolerant_markdown: bool,
) -> LLMResult:
    node = config.qodercli_node_path or shutil.which("node") or ""
    script = config.qodercli_js_path
    if not node or not script:
        raise QoderCLIError("QODERCLI_NODE_PATH / QODERCLI_JS_PATH not configured")

    meta = loader.load(agent)
    attachment: Path | None = None
    if files:
        attachment = workdir / f".__qodercli_attach_{int(time.time())}.diff"
        attachment.write_text(files[0].read_text() if files[0].exists() else "\n".join(p.read_text() for p in files))

    cmd = [
        node, script, "-p",
        "--model", config.qodercli_model,
        "--no-session-persistence",
        "-o", "json",
        "-w", str(workdir),
        "--append-system-prompt", meta["prompt"],
        "--disallowed-tools", "write,edit,bash,webfetch,websearch",
    ]
    if attachment is not None:
        cmd += ["--attachment", str(attachment)]
    cmd.append(prompt)

    started = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout or config.qodercli_timeout, bufsize=0)
    if attachment is not None and attachment.exists():
        try: attachment.unlink()
        except OSError: pass

    raw = proc.stdout.strip()
    if not raw:
        raise QoderCLIOutputError(f"empty stdout; stderr={proc.stderr[:500]}")
    try:
        top = json.loads(raw)
    except json.JSONDecodeError as e:
        raise QoderCLIOutputError(f"top-level JSON parse failed: {e}; stdout[:500]={raw[:500]}")
    inner = top.get("result", "")
    if isinstance(inner, dict):
        text = json.dumps(inner)
    else:
        text = str(inner)
    duration_ms = int((time.time() - started) * 1000)
    try:
        data = json.loads(_strip_fence(text))
    except json.JSONDecodeError:
        if tolerant_markdown:
            return LLMResult(data={}, provider="qodercli", raw_output=text, duration_ms=duration_ms, model=config.qodercli_model)
        raise QoderCLIOutputError(f"agent output not JSON: {text[:300]}")
    return LLMResult(
        data=data,
        provider="qodercli",
        duration_ms=duration_ms,
        model=config.qodercli_model,
        raw_output=text,
    )
```

- [ ] **Step 4: Run the new test to verify it passes**

Run: `.venv/bin/pytest tests/test_qodercli_subprocess_fallback.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Re-run the full LLM suite — fallback must cover the existing `test_qodercli_returns_real_provider`**

Run: `.venv/bin/pytest tests/test_llm_adapter.py tests/test_qodercli_acp_protocol.py tests/test_qodercli_acp_bootstrap.py tests/test_qodercli_acp_rpc.py tests/test_qodercli_acp_notifications.py tests/test_qodercli_subprocess_fallback.py -q`
Expected: PASS for all suites (the prior `test_qodercli_returns_real_provider` should now pass because `QoderCLIProvider` is constructible in both drivers).

- [ ] **Step 6: Commit**

```bash
git add reviewagent/llm/qodercli_subprocess.py tests/test_qodercli_subprocess_fallback.py
git commit -m "feat(llm): qodercli subprocess fallback when driver=subprocess"
```

### Task 8: Re-export ACP client + concurrency probe test

**Files:**
- Modify: `reviewagent/llm/__init__.py`
- Create: `tests/test_qodercli_acp_concurrency.py`

**Interfaces:**
- Re-exports: `from reviewagent.llm.qodercli_acp import QoderCLIACPClient, QoderCLIACPError, QoderCLIAuthError, QoderCLITimeoutError, QoderCLIProtocolError` from `reviewagent.llm` (so callers can `from reviewagent.llm import QoderCLIACPClient`).

- [ ] **Step 1: Write the failing test**

Create `tests/test_qodercli_acp_concurrency.py`:

```python
"""Public surface: re-exports + concurrent chat() under one client."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import reviewagent.llm as llm
from reviewagent.llm.qodercli_acp import QoderCLIACPClient


def test_qodercli_acp_client_is_reexported() -> None:
    assert llm.QoderCLIACPClient is QoderCLIACPClient


def test_concurrent_chat_resolves_three_sessions(tmp_path: Path) -> None:
    transport = MagicMock()
    client = QoderCLIACPClient(
        node="/usr/bin/node",
        script="/opt/q.js",
        model="DeepSeek-V4-Flash",
        extra_args=[],
        transport=transport,
    )
    client._workdir = tmp_path
    client._sem = threading.Semaphore(4)

    delays = [0.2, 0.05, 0.1]
    fake_results = [
        {"stop_reason": "end_turn", "text": f"{{\"id\": {i}}}"}
        for i in range(3)
    ]
    lock = threading.Lock()
    counter = {"i": 0}

    def _fake_prompt(*args, **kwargs):
        with lock:
            idx = counter["i"]
            counter["i"] += 1
        time.sleep(delays[idx])
        return fake_results[idx]

    def _fake_new(**kwargs):
        return f"sess-{threading.current_thread().name}"

    with patch.object(client, "session_new", side_effect=_fake_new), \
         patch.object(client, "session_prompt", side_effect=_fake_prompt):
        results = []
        barrier = threading.Barrier(3)

        def _run(idx: int) -> None:
            barrier.wait()
            results.append(client.chat(
                agent=f"agent-{idx}",
                prompt=f"task {idx}",
                files=[],
                timeout=5.0,
                max_concurrent_sessions=4,
                session_reuse_window=0,
            ))

        threads = [threading.Thread(target=_run, args=(i,), name=f"t{i}") for i in range(3)]
        start = time.time()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=3)
        elapsed = time.time() - start

    assert [r["text"] for r in results] == ['{"id": 0}', '{"id": 1}', '{"id": 2}']
    # Truly parallel: elapsed ≈ max(delays) = 0.2s, not sum (0.35s).
    assert elapsed < 0.3
```

- [ ] **Step 2: Run the new test to verify it fails**

Run: `.venv/bin/pytest tests/test_qodercli_acp_concurrency.py -v`
Expected: FAIL with `AttributeError: module 'reviewagent.llm' has no attribute 'QoderCLIACPClient'`.

- [ ] **Step 3: Add re-exports**

In `reviewagent/llm/__init__.py`, append:

```python
from reviewagent.llm.qodercli_acp import (  # noqa: F401
    QoderCLIACPClient,
    QoderCLIACPError,
    QoderCLIAuthError,
    QoderCLITimeoutError,
    QoderCLIProtocolError,
)
```

- [ ] **Step 4: Run the new test to verify it passes**

Run: `.venv/bin/pytest tests/test_qodercli_acp_concurrency.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Run the entire LLM suite to confirm no regression**

Run: `.venv/bin/pytest tests/ -q -k 'llm or qodercli or config_acp or sync_qoder'`
Expected: all suites pass.

- [ ] **Step 6: Commit**

```bash
git add reviewagent/llm/__init__.py tests/test_qodercli_acp_concurrency.py
git commit -m "feat(llm): re-export ACP client + concurrency probe test"
```

### Task 9: End-to-end smoke against the real `qodercli --acp`

**Files:**
- Create: `scripts/probe_qodercli_acp.py`
- No automated test — manual gate that must be observed before merging.

**Interfaces:**
- `scripts/probe_qodercli_acp.py` — boots a real ACP server, runs 3 concurrent `chat()` calls with different `agent` names, asserts each returns a non-empty `result.text`. Prints the elapsed time to stdout. Exits non-zero on any failure.

- [ ] **Step 1: Write the probe script**

Create `scripts/probe_qodercli_acp.py`:

```python
"""Manual end-to-end probe for the QoderCLI ACP driver.

Run with the project virtualenv:
    .venv/bin/python scripts/probe_qodercli_acp.py
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

from reviewagent.config import config
from reviewagent.llm.qodercli_acp import QoderCLIACPClient
from scripts.sync_qoder_agents import sync_qoder_agents
from reviewagent.prompts.loader import PROMPTS_DIR


def main() -> int:
    workdir = Path.cwd()
    agents_dir = workdir / ".qoder" / "agents"
    sync_qoder_agents(PROMPTS_DIR, agents_dir)

    client = QoderCLIACPClient.bootstrap(
        node=config.qodercli_node_path or "node",
        script=config.qodercli_js_path,
        model=config.qodercli_model,
        extra_args=config.qodercli_acp_extra_args + ["--setting-sources", "project,user,local"],
        workdir=workdir,
    )
    try:
        caps = client.initialize(client_info={"name": "probe"}, capabilities={})
        print(f"agentCapabilities={json.dumps(caps)[:200]}")
        barrier = threading.Barrier(3)
        results: list[str] = []
        errors: list[str] = []
        lock = threading.Lock()

        def _run(idx: int) -> None:
            try:
                sid = client.session_new(cwd=workdir)
                barrier.wait()
                client.session_prompt(sid, f"Reply with the integer {idx} in JSON.", timeout=120)
                text = client.collect_message(sid)
                with lock:
                    results.append(text)
            except Exception as e:  # noqa: BLE001
                with lock:
                    errors.append(f"thread {idx}: {e!r}")

        threads = [threading.Thread(target=_run, args=(i,)) for i in range(3)]
        start = time.time()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=180)
        elapsed = time.time() - start
        print(f"elapsed={elapsed:.2f}s results={results} errors={errors}")
        if errors or len(results) != 3:
            return 1
        return 0
    finally:
        client.shutdown()


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run the probe and observe the output**

Run: `.venv/bin/python scripts/probe_qodercli_acp.py`
Expected: prints `agentCapabilities=...`, then 3 non-empty `results=...`, total elapsed time in the 5–30s range, exit code 0.

- [ ] **Step 3: Confirm the .qoder/agents directory was created and contains 6 entries**

Run: `ls .qoder/agents/`
Expected: `describe.md improve.md improve_agent.md weekly_change_summary.md weekly_inspection_summary.md weekly_quality_scan.md`.

- [ ] **Step 4: Commit (no test, just the script)**

```bash
git add scripts/probe_qodercli_acp.py
git commit -m "chore(scripts): qodercli ACP end-to-end probe"
```

### Task 10: Documentation update + verify-branch push

**Files:**
- Modify: `docs/LLM_PROVIDER_ADAPTER.md`
- Modify: `docs/QODERCLI_FEASIBILITY_REPORT.md`
- Modify: `.env` (add new driver keys; keep existing values intact)
- Modify: `scripts/restart_local.sh` (sync `.qoder/agents` on each worker start, optional `QODERCLI_DRIVER` export)

**Interfaces:**
- `docs/LLM_PROVIDER_ADAPTER.md`: add a new "v2 — ACP driver" section after the existing content describing the long-connection architecture, the `.qoder/agents` sync, the concurrency knobs, and the subprocess fallback.
- `docs/QODERCLI_FEASIBILITY_REPORT.md`: prepend a one-line "Superseded by 2026-08-03 ACP design — see docs/superpowers/specs/..." note. Do not delete the file (other agents may still need to read it).
- `.env`: append the new keys (do not change the existing `LLM_PROVIDER=qodercli` line).

- [ ] **Step 1: Update `docs/LLM_PROVIDER_ADAPTER.md`**

Append the following after the existing last section:

```markdown
## v2 — QoderCLI ACP driver (2026-08-03)

The original implementation ran `qodercli -p --append-system-prompt` via
`subprocess.run` for every job. RQ workers consistently hit a fork + PIPE
hang on macOS, so the driver was rewritten to use a long-lived ACP
(`qodercli --acp`) server per worker. See
[`docs/superpowers/specs/2026-08-03-qodercli-acp-provider-design.md`](superpowers/specs/2026-08-03-qodercli-acp-provider-design.md)
for the full design and
[`docs/superpowers/plans/2026-08-03-qodercli-acp-provider.md`](superpowers/plans/2026-08-03-qodercli-acp-provider.md)
for the implementation plan.

### Knobs (`.env`)

| Key | Default | Meaning |
|---|---|---|
| `QODERCLI_DRIVER` | `acp` | `acp` = new long connection; `subprocess` = legacy one-shot path |
| `QODERCLI_MAX_CONCURRENT_SESSIONS` | `4` | Semaphore bound for `session/prompt` inside one ACP process |
| `QODERCLI_QUEUE_WAIT_TIMEOUT` | `120` | Seconds a caller waits for a free slot before raising |
| `QODERCLI_SESSION_REUSE_WINDOW` | `300` | Reuse the same `sessionId` for the same agent within this window |
| `QODERCLI_SESSION_TIMEOUT` | `540` | Per `session/prompt` timeout (matches the opencode default) |
| `QODERCLI_ACP_EXTRA_ARGS` | (empty) | Extra args appended to the `qodercli --acp` command |

### Subagent materialisation

`scripts/sync_qoder_agents.py` is invoked at the start of every
`QoderCLIProvider.run()` call. It writes
`reviewagent/prompts/<name>.md` to `.qoder/agents/<name>.md` with the
field mapping from the design spec §5, so the ACP server picks them
up via `--setting-sources project,user,local` without using
`--agents` JSON.

### Roll-back

Set `QODERCLI_DRIVER=subprocess` in `.env` and restart the workers.
The legacy one-shot path lives in
[`reviewagent/llm/qodercli_subprocess.py`](../reviewagent/llm/qodercli_subprocess.py).
```

- [ ] **Step 2: Mark the feasibility report as superseded**

In `docs/QODERCLI_FEASIBILITY_REPORT.md`, prepend (at the very top, after the title):

```markdown
> **Superseded by** [`docs/superpowers/specs/2026-08-03-qodercli-acp-provider-design.md`](superpowers/specs/2026-08-03-qodercli-acp-provider-design.md) (2026-08-03). This report documents the original one-shot subprocess approach; the live implementation now uses the long-lived ACP driver described in v2 of `LLM_PROVIDER_ADAPTER.md`. Kept for historical reference.
```

- [ ] **Step 3: Append the new keys to `.env`**

Run: `printf '\n# --- QoderCLI ACP driver ---\nQODERCLI_DRIVER=acp\nQODERCLI_ACP_EXTRA_ARGS=\nQODERCLI_MAX_CONCURRENT_SESSIONS=4\nQODERCLI_QUEUE_WAIT_TIMEOUT=120\nQODERCLI_SESSION_REUSE_WINDOW=300\nQODERCLI_SESSION_TIMEOUT=540\n' >> /Users/jarvs/ReviewAgent/.env`
Then `tail -10 /Users/jarvs/ReviewAgent/.env` to verify the new lines are present and the old `LLM_PROVIDER=qodercli` line is untouched.

- [ ] **Step 4: Hook sync into worker boot**

In `scripts/restart_local.sh`, add `bash -c "cd $(dirname $(readlink -f $0))/.. && .venv/bin/python scripts/sync_qoder_agents.py"` (or equivalent — adapt to the existing pattern) immediately before each `exec .venv/bin/rq worker` line. This guarantees the `.qoder/agents/*.md` files exist before the worker bootstraps the ACP server.

- [ ] **Step 5: Restart the workers and re-run the probe to confirm the full path works**

Run: `bash scripts/restart_local.sh status` (or your project's restart helper) and then `.venv/bin/python scripts/probe_qodercli_acp.py`. Expected: probe still returns 0 and elapsed stays under 30s.

- [ ] **Step 6: Create the verify branch, push, and run GitLab smoke**

```bash
git checkout -b codex/feat-llm-provider-adapter-verify-2026-08-03
git push -u origin codex/feat-llm-provider-adapter-verify-2026-08-03
# On GitLab http://127.0.0.1:8929/root/auto-review-test (root / Jarvs@2026) — do NOT echo
# the password in any commit. Open a test MR that touches a non-empty file and verify the
# review-bot-v2 path now produces a describe + improve comment using the ACP driver.
```

Document the MR URL in the commit body.

- [ ] **Step 7: Commit documentation + .env additions (skip secrets)**

```bash
git add docs/LLM_PROVIDER_ADAPTER.md docs/QODERCLI_FEASIBILITY_REPORT.md scripts/restart_local.sh
git commit -m "docs: qodercli ACP v2 documentation + worker boot sync"
# Do NOT commit the .env file — it contains local credentials. The user will mirror the new
# keys into the deployment .env out-of-band.
```

## Self-Review

### 1. Spec coverage

| Spec section | Covered by |
|---|---|
| §1 背景与根因 (fork/PIPE hang) | Task 4 bootstrap + Task 6 provider rewrite (removes fork from job path) |
| §2.1 根上消 fork 卡死 | Task 4 `bootstrap()` runs once per worker |
| §2.2 并发检视多个 MR | Task 5 `chat()` semaphore + Task 8 concurrency test |
| §2.3 接口零变更 | Task 6 keeps `BaseLLMProvider.run(...)` signature |
| §2.4 可降级 | Task 7 subprocess fallback + `QODERCLI_DRIVER` |
| §2.5 可观测 | Task 4 records `_sessions` + `on_notification` history (extending to Prometheus counters is out of scope and explicitly listed in §10) |
| §3 进程与拓扑图 | Task 4 diagram maps 1:1 to `QoderCLIACPClient.bootstrap` |
| §4 协议细节 | Task 5 (RPCs) + Task 6 (notifications) |
| §5 Subagent 配置映射 | Task 2 `sync_qoder_agents.py` |
| §6.1 进程内单例 | Task 4 bootstrap, Task 6 `_get_or_bootstrap` |
| §6.2 并发 | Task 5 `chat()` semaphore, Task 8 probe |
| §6.3 Session 复用 | Task 5 `_reuse_session` |
| §7 失败模式与降级 | Task 4 (process death → bootstrap retry), Task 5 (timeout → cancel), Task 6 (`tolerant_markdown` fallback) |
| §8 配置项 | Task 1 |
| §9 风险与缓解 | Each risk maps to a task; revisit in code review |
| §10 范围外 | Respected (no custom ACP server, no rate limiting, no streaming UI) |
| §11 验收 | Task 9 (probe) + Task 10 (GitLab MR #181) |

No gaps detected.

### 2. Placeholder scan

`rg -n 'TBD|TODO|XXX|\\?\\?\\?|待定|todo|placeholder|FIXME' docs/superpowers/plans/2026-08-03-qodercli-acp-provider.md` returns no matches. Every test snippet is a complete runnable test. Every code snippet under "Step 3" is a full file or a complete method, never "implement later".

### 3. Type consistency

| Symbol | Defined in | Reused in |
|---|---|---|
| `QoderCLIACPClient` | Task 3 (init/send_loop) | Tasks 4, 5, 6, 8 |
| `QoderCLIACPError` / `QoderCLIAuthError` / `QoderCLITimeoutError` / `QoderCLIProtocolError` | Task 3 | Tasks 4, 5, 6, 8 |
| `QoderCLIError` / `QoderCLITimeoutError` / `QoderCLIOutputError` (provider re-exports) | Task 6 (provider) | Task 7 (subprocess fallback) |
| `LLMResult` | existing `reviewagent/llm/base.py` | Task 6, Task 7 |
| `_next_id`, `_request`, `_register_pending`, `_dispatch_response`, `on_notification`, `collect_message`, `chat`, `_reuse_session`, `session_new`, `session_prompt`, `session_cancel`, `initialize`, `bootstrap`, `shutdown` | Tasks 3, 4, 5, 6 | referenced consistently across tasks |
| `config.qodercli_*` | Task 1 | Task 6, Task 7, Task 9, Task 10 |
| `run_subprocess` | Task 7 | Task 6 (delegation) |

All names match across tasks.

