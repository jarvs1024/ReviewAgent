"""QoderCLIProvider — wires ReviewAgent commands onto the subprocess driver.

The default driver (``subprocess``, as of 2026-08-04) uses the one-shot
``qodercli -p`` invocation that re-runs per call. The previous default
ACP driver (long-lived ``qodercli --acp`` server shared across jobs)
is preserved as a code path but is **disabled by default** because
the ACP session/prompt response stream hangs in production (verified
on MR 176 run 577 — 7+ min no response, CPU 0%, killed via SIGTERM).

Re-enable once upstream bug is fixed:
    1. Edit :meth:`QoderCLIProvider.run` so the ACP branch runs when
       ``config.qodercli_driver == "acp"`` (drop the unconditional
       subprocess dispatch).
    2. Set ``QODERCLI_DRIVER=acp`` in .env.
    3. Verify on a non-trivial MR before deploying.

Constructing ``QoderCLIProvider(node_path=..., js_path=..., model=...)``
(legacy compat constructor) always uses the subprocess driver.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from pathlib import Path

from reviewagent.config import config
from reviewagent.llm.base import BaseLLMProvider, LLMResult, _strip_fence
from reviewagent.llm.qodercli_acp import (
    QoderCLIACPClient,
    QoderCLIACPError,
    QoderCLITimeoutError as _QoderCLITimeoutErrorACP,
    QoderCLIProtocolError,
)
from reviewagent.logging_setup import logger
from reviewagent.prompts import loader


from reviewagent.llm.qodercli_errors import (
    QoderCLIError,
    QoderCLITimeoutError,
    QoderCLIOutputError,
)


_acp_client: QoderCLIACPClient | None = None
_acp_lock = threading.Lock()


def _get_or_bootstrap(workdir: Path) -> QoderCLIACPClient:
    """Return the worker-scoped ACP client, bootstrapping it on first use.

    .. warning::
       Currently unreachable from :meth:`QoderCLIProvider.run` — the run
       method unconditionally dispatches to subprocess (see commit
       ``3d09f43``). Kept for the day upstream ACP stdin hang is fixed.
    """
    global _acp_client
    with _acp_lock:
        if _acp_client is not None and _acp_client._proc and _acp_client._proc.poll() is None:
            return _acp_client
        node = config.qodercli_node_path or shutil.which("node") or ""
        script = config.qodercli_js_path
        if not node or not script:
            raise QoderCLIError(
                "QODERCLI_NODE_PATH / QODERCLI_JS_PATH not configured for ACP driver"
            )
        # Materialise .qoder/agents/ so the ACP server picks up the agents
        # on startup. We import here to keep this module importable without
        # bootstrapping the project.
        from scripts.sync_qoder_agents import sync_qoder_agents
        from reviewagent.prompts.loader import PROMPTS_DIR

        agents_dir = workdir / ".qoder" / "agents"
        sync_qoder_agents(PROMPTS_DIR, agents_dir)
        extra = list(config.qodercli_acp_extra_args) + [
            "--setting-sources", "project,user,local",
        ]
        client = QoderCLIACPClient.bootstrap(
            node=node,
            script=script,
            model=config.qodercli_model,
            extra_args=extra,
            workdir=workdir,
        )
        try:
            client.initialize(
                client_info={"name": "reviewagent"},
                capabilities={},
            )
        except Exception as e:  # noqa: BLE001
            client.shutdown()
            raise QoderCLIError(f"qodercli --acp initialize failed: {e}") from e
        _acp_client = client
        return client


def reset_for_tests() -> None:
    """Clear the worker-scoped client; tests use this between cases.

    Shutdown happens *outside* the lock so a hung shutdown does not
    deadlock subsequent callers. Reference is cleared first so concurrent
    ``_get_or_bootstrap`` won't pick up a stale client.
    """
    global _acp_client
    with _acp_lock:
        stale = _acp_client
        _acp_client = None
    if stale is not None:
        try:
            stale.shutdown()
        except Exception:  # pragma: no cover
            pass


class QoderCLIProvider(BaseLLMProvider):
    """LLM Provider backed by QoderCLI ACP long connection.

    Two construction modes:

    * ``QoderCLIProvider()`` — config-driven; honours ``config.qodercli_driver``
      (defaults to ``acp``). Use this from production code.
    * ``QoderCLIProvider(node_path=..., js_path=..., model=...)`` —
      forces the legacy one-shot subprocess path. The arguments are passed
      through to ``subprocess.run`` / ``--version`` checks. Useful for unit
      tests that patch ``subprocess.run`` and for callers that need an
      isolated provider instance.
    """

    def __init__(
        self,
        *,
        node_path: str = "",
        js_path: str = "",
        model: str = "",
    ) -> None:
        self._node_path = node_path
        self._js_path = js_path
        self._model = model
        # Force subprocess when the caller provides all three explicit
        # overrides — backwards-compatible alias for the legacy class.
        self._legacy = bool(node_path and js_path and model)

    @property
    def provider_name(self) -> str:
        return "qodercli"

    def _legacy_health_check(self) -> bool:
        """Probe both binaries via subprocess.run (legacy mode)."""
        node = self._node_path or shutil.which("node")
        script = self._js_path or config.qodercli_js_path
        if not node or not script:
            return False
        try:
            proc = subprocess.run(
                [node, script, "--version"],
                capture_output=True, text=True, timeout=10,
            )
            return proc.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return False

    def health_check(self) -> bool:
        """Probe the configured driver.

        * Legacy mode (per-call overrides): spawn ``qodercli --version``.
        * Subprocess driver (default): same probe — quick and avoids
          false positives if the binary is missing or the model is
          mis-configured.
        * ACP driver: long-lived server probe (kept for back-compat;
          not reachable while ACP is disabled).
        """
        if self._legacy:
            return self._legacy_health_check()
        if config.qodercli_driver == "subprocess":
            return self._legacy_health_check()
        try:
            client = _get_or_bootstrap(Path.cwd())
        except QoderCLIError:
            return False
        return client._proc is not None and client._proc.poll() is None

    def run(
        self,
        *,
        agent: str,
        prompt: str,
        workdir: Path,
        files: list[Path] | None = None,
        timeout: int | None = None,
        tolerant_markdown: bool = False,
    ) -> LLMResult:
        # Legacy / kill-switch path: bypass ACP entirely.
        # 2026-08-04: ACP path hangs on stdin (run 577 验证 7+min 卡死),
        # force subprocess regardless of QODERCLI_DRIVER setting.
        if self._legacy or config.qodercli_driver in ("subprocess", "acp"):
            from reviewagent.llm.qodercli_subprocess import run_subprocess
            return run_subprocess(
                agent=agent, prompt=prompt, workdir=workdir, files=files,
                timeout=timeout or config.qodercli_timeout,
                tolerant_markdown=tolerant_markdown,
                node=self._node_path or None,
                script=self._js_path or None,
                model=self._model or None,
            )

        client = _get_or_bootstrap(workdir)
        meta = loader.load(agent)
        prefixed = f"使用 {agent} subagent 处理以下任务:\n\n{prompt}"
        started = time.monotonic()
        sid = client.session_new(cwd=workdir)
        try:
            client.session_prompt(sid, prefixed, timeout=timeout or config.qodercli_session_timeout)
        except _QoderCLITimeoutErrorACP as e:
            try:
                client.session_cancel(sid)
            except Exception:  # pragma: no cover
                pass
            raise QoderCLITimeoutError(str(e)) from e
        message = client.collect_message(sid)
        duration_ms = int((time.monotonic() - started) * 1000)
        if not message:
            raise QoderCLIOutputError("ACP session produced no agent_message_chunk")
        try:
            data = json.loads(_strip_fence(message))
        except json.JSONDecodeError:
            if tolerant_markdown:
                return LLMResult(
                    data={}, provider="qodercli", raw_output=message,
                    duration_ms=duration_ms, model=config.qodercli_model,
                )
            raise QoderCLIOutputError(f"agent output not JSON: {message[:300]}")
        return LLMResult(
            data=data,
            provider="qodercli",
            duration_ms=duration_ms,
            model=config.qodercli_model,
            raw_output=message,
        )
