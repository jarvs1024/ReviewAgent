"""Subprocess fallback for QoderCLIProvider.

When `QODERCLI_DRIVER=subprocess` (or the legacy constructor
`QoderCLIProvider(node_path=..., js_path=..., model=...)` is used) we
revert to the pre-ACP one-shot invocation model. The implementation
mirrors the original `QoderCLIProvider.run` so the public contract
(`run -> LLMResult`) and the parsed JSON shape are unchanged.

CLI invocation:
    node {qodercli.js} -p \
        --model {model} \
        --no-session-persistence \
        -o json \
        -w {workdir} \
        --append-system-prompt {agent_meta} \
        --disallowed-tools write,edit,bash,webfetch,websearch \
        [--attachment {tmp_diff}] \
        {prompt}

stdout JSON shape (top-level wrapper produced by qodercli):
    {
        "type": "result", "subtype": "success",
        "result": "<inner JSON string OR plain markdown>",
        "stop_reason": "end_turn" | "max_tokens" | ...,
        "duration_ms": int,
        "usage": {"input_tokens": int, "output_tokens": int, ...},
        "modelID": "DeepSeek-V4-Flash",
    }
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

from reviewagent.config import config
from reviewagent.llm.base import LLMResult, _strip_fence
from reviewagent.llm.qodercli_errors import (
    QoderCLIError,
    QoderCLIOutputError,
    QoderCLITimeoutError,
)
from reviewagent.logging_setup import logger
from reviewagent.prompts import loader


def _resolve_paths(
    node: str | None, script: str | None, model: str | None
) -> tuple[str, str, str]:
    node = node or config.qodercli_node_path or shutil.which("node") or ""
    script = script or config.qodercli_js_path
    model = model or config.qodercli_model
    if not node or not script:
        raise QoderCLIError(
            "QODERCLI_NODE_PATH / QODERCLI_JS_PATH not configured for subprocess driver"
        )
    return node, script, model


def _build_attachment(workdir: Path, files: list[Path] | None) -> Path | None:
    """Materialise a single attachment file under workdir; return None on failure.

    Concatenates all provided files with a header separator. qodercli's
    ``--attachment`` flag expects a single path, so multiple inputs are
    merged into one temp file.
    """
    if not files:
        return None
    attachment = workdir / f".__qodercli_attach_{int(time.time() * 1000)}.diff"
    try:
        chunks: list[str] = []
        for p in files:
            try:
                chunks.append(p.read_text(encoding="utf-8"))
            except OSError as e:
                logger.warning("qodercli: failed to read attachment {}: {}", p, e)
                return None
        attachment.write_text("\n".join(chunks), encoding="utf-8")
        return attachment
    except OSError as e:
        logger.warning("qodercli: failed to write attachment file: {}", e)
        return None


def _cleanup_attachment(attachment: Path | None) -> None:
    if attachment is None:
        return
    try:
        attachment.unlink()
    except OSError:
        pass



def _build_cmd(
    *,
    node_path: str,
    script_path: str,
    model_name: str,
    workdir: Path,
    meta_prompt: str,
    attachment: "Path | None",
    prompt: str,
    permission_mode: str,
    max_turns: int,
) -> list[str]:
    """Pure-function form of the qodercli subprocess command — extracted for unit tests."""
    cmd = [
        node_path, script_path, "-p",
        "--model", model_name,
        "--no-session-persistence",
        "-o", "json",
        "-w", str(workdir),
        "--append-system-prompt", meta_prompt,
        "--disallowed-tools", "write,edit,bash,webfetch,websearch",
    ]
    if permission_mode:
        cmd += ["--permission-mode", permission_mode]
    if max_turns > 0:
        cmd += ["--max-turns", str(max_turns)]
    if attachment is not None:
        cmd += ["--attachment", str(attachment)]
    cmd.append(prompt)
    return cmd


def _build_cmd_for_test(
    *,
    node: str,
    script: str,
    model: str,
    meta_prompt: str,
    workdir: str,
    prompt: str,
    permission_mode: str,
    max_turns: int,
) -> list[str]:
    """Test shim mirroring _build_cmd with positional args."""
    return _build_cmd(
        node_path=node,
        script_path=script,
        model_name=model,
        workdir=Path(workdir),
        meta_prompt=meta_prompt,
        attachment=None,
        prompt=prompt,
        permission_mode=permission_mode,
        max_turns=max_turns,
    )


def run_subprocess(
    *,
    agent: str,
    prompt: str,
    workdir: Path,
    files: list[Path] | None,
    timeout: int,
    tolerant_markdown: bool,
    node: str | None = None,
    script: str | None = None,
    model: str | None = None,
) -> LLMResult:
    """One-shot `qodercli -p` invocation. See module docstring for the JSON shape.

    Args:
        agent: agent name (matches a key in `reviewagent/prompts/`).
        prompt: user prompt text; appended as the trailing positional arg.
        workdir: working directory passed via `-w`.
        files: optional list of files to attach as a single `--attachment` blob.
        timeout: per-call timeout in seconds.
        tolerant_markdown: when True, non-JSON stdout falls back to `data["markdown"]`.
        node / script / model: per-call overrides; empty falls back to `config`.

    Raises:
        QoderCLITimeoutError: subprocess.TimeoutExpired or `timeout` exceeded.
        QoderCLIError: non-zero exit code, missing binary, or unexpected failure.
        QoderCLIOutputError: stdout empty, malformed JSON, or agent JSON missing.
    """
    node_path, script_path, model_name = _resolve_paths(node, script, model)
    meta = loader.load(agent)
    attachment = _build_attachment(workdir, files)

    cmd = _build_cmd(
        node_path=node_path,
        script_path=script_path,
        model_name=model_name,
        workdir=workdir,
        meta_prompt=meta["prompt"],
        attachment=attachment,
        prompt=prompt,
        permission_mode=config.qodercli_permission_mode,
        max_turns=config.qodercli_max_turns,
    )

    started = time.monotonic()
    actual_timeout = timeout or config.qodercli_timeout
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=actual_timeout,
        )
    except subprocess.TimeoutExpired as e:
        _cleanup_attachment(attachment)
        raise QoderCLITimeoutError(
            f"qodercli timeout after {actual_timeout}s"
        ) from e
    except FileNotFoundError as e:
        _cleanup_attachment(attachment)
        raise QoderCLIError(f"qodercli binary not found: {e}") from e
    _cleanup_attachment(attachment)

    if proc.returncode != 0:
        stderr = (proc.stderr or "")[:300]
        raise QoderCLIError(
            f"qodercli exit={proc.returncode} stderr={stderr}"
        )

    raw = proc.stdout.strip() if proc.stdout else ""
    duration_ms = int((time.monotonic() - started) * 1000)

    if not raw:
        if tolerant_markdown:
            return LLMResult(
                data={}, provider="qodercli", raw_output="",
                duration_ms=duration_ms, model=model_name,
            )
        raise QoderCLIOutputError(
            f"qodercli empty stdout; stderr={(proc.stderr or '')[:500]}"
        )

    try:
        top = json.loads(raw)
    except json.JSONDecodeError:
        if tolerant_markdown:
            return LLMResult(
                data={}, provider="qodercli", raw_output=raw,
                duration_ms=duration_ms, model=model_name,
            )
        raise QoderCLIOutputError(
            f"qodercli top-level JSON parse failed; stdout[:500]={raw[:500]}"
        )

    if not isinstance(top, dict):
        # CLI wrapper is required to emit a JSON object; any other shape is malformed.
        if tolerant_markdown:
            return LLMResult(
                data={}, provider="qodercli", raw_output=raw,
                duration_ms=duration_ms, model=model_name,
            )
        raise QoderCLIOutputError(
            f"qodercli top-level wrapper not a dict; got {type(top).__name__}"
        )

    usage = top.get("usage", {}) or {}
    prompt_tokens = int(usage.get("input_tokens", 0) or 0)
    completion_tokens = int(usage.get("output_tokens", 0) or 0)
    stop_reason = top.get("stop_reason")
    model_id = top.get("modelID") or model_name

    if stop_reason == "max_tokens":
        logger.warning(
            "qodercli output truncated (stop_reason=max_tokens); "
            "agent={} usage={}",
            agent, usage,
        )

    inner = top.get("result", "")
    if isinstance(inner, dict):
        # Inner JSON already deserialised — return as-is.
        inner_text = json.dumps(inner, ensure_ascii=False)
        return LLMResult(
            data=inner,
            provider="qodercli",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            duration_ms=duration_ms,
            model=model_id,
            raw_output=inner_text,
        )

    # `result` is a string — could be a JSON-encoded blob or plain markdown.
    text = _strip_fence(str(inner))
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        if tolerant_markdown:
            return LLMResult(
                data={"markdown": text},
                provider="qodercli",
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                duration_ms=duration_ms,
                model=model_id,
                raw_output=text,
            )
        raise QoderCLIOutputError(f"agent output result not JSON: {text[:300]}")

    return LLMResult(
        data=data,
        provider="qodercli",
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        duration_ms=duration_ms,
        model=model_id,
        raw_output=text,
    )
