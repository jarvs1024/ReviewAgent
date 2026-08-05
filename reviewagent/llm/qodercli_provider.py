"""QoderCLIProvider — wires ReviewAgent commands onto the subprocess driver.

As of 2026-08-05 the **subprocess** driver is the only supported path. It
spawns one ``qodercli -p`` per call and consumes its JSON stdout.

The previous ``qodercli_acp`` driver (long-lived ``qodercli --acp`` server)
was removed on 2026-08-05: the upstream ACP session/prompt response stream
hung in production (verified on MR 176 run 577 — 7+ min no response, CPU
0%, killed via SIGTERM) and the dead-code was kept around only as a future
re-enable hook. If/when Qoder ships a fixed ACP server, a fresh module
should be scaffolded separately rather than resurrecting the old one.

Two construction modes:

* ``QoderCLIProvider()`` — config-driven; reads node / script / model from
  ``config.qodercli_*`` and dispatches to the subprocess driver.
* ``QoderCLIProvider(node_path=..., js_path=..., model=...)`` — legacy compat
  constructor; forces the subprocess path with explicit overrides. Useful
  for unit tests that patch ``subprocess.run``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from reviewagent.config import config
from reviewagent.llm.base import BaseLLMProvider, LLMResult, _strip_fence
from reviewagent.llm.qodercli_errors import QoderCLIOutputError
from reviewagent.llm.qodercli_subprocess import run_subprocess


class QoderCLIProvider(BaseLLMProvider):
    """LLM Provider backed by QoderCLI subprocess (``qodercli -p`` per call)."""

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
        # 三个 override 字段用于测试（tests/test_llm_adapter.py 显式传值），
        # 实际运行路径与 config 模式一致 — 都是 subprocess driver。

    @property
    def provider_name(self) -> str:
        return "qodercli"

    def health_check(self) -> bool:
        """Probe the configured qodercli binary via subprocess."""
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
        # Always the subprocess driver — ACP path removed 2026-08-05.
        return run_subprocess(
            agent=agent, prompt=prompt, workdir=workdir, files=files,
            timeout=timeout or config.qodercli_timeout,
            tolerant_markdown=tolerant_markdown,
            node=self._node_path or None,
            script=self._js_path or None,
            model=self._model or None,
        )


__all__ = ["QoderCLIProvider"]
