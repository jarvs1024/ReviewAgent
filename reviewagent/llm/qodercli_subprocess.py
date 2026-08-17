"""Subprocess driver for QoderCLIProvider.

The only supported path as of 2026-08-05 (the ACP long-lived driver was
removed; see :mod:`reviewagent.llm.qodercli_provider`). One ``qodercli -p``
subprocess is spawned per call. The legacy explicit-arg constructor
``QoderCLIProvider(node_path=..., js_path=..., model=...)`` continues to
work as a config-free entry point and still routes through this module.

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
import re
import os
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from reviewagent.config import config
from reviewagent.llm.base import LLMResult, _strip_fence
from reviewagent.llm.qodercli_errors import (
    QoderCLIError,
    QoderCLIDaemonError,
    QoderCLIOutputError,
    QoderCLITimeoutError,
)
from reviewagent.logging_setup import logger
from reviewagent.prompts import loader

# ---------------------------------------------------------------------------
# Daemon health-check + circuit breaker
# ---------------------------------------------------------------------------
# qodercli 依赖 opencode acp daemon (OPENCODE_URL, 默认 127.0.0.1:4096).
# daemon 挂了时, node 子进程会无限挂起 (connect 无 timeout), 导致每个 chunk
# 占满 QODERCLI_TIMEOUT (600s), 23+ 文件直接打爆 job_timeout.
#
# 防御: 在 spawn node 前做一次 TCP probe (2s), 不通立刻报错.
# 断路器: 失败后 30s 内不再重复探测, 避免 80 个文件各等 2s.
# ---------------------------------------------------------------------------
_DAEMON_COOLDOWN_UNTIL: float = 0.0
_DAEMON_COOLDOWN_SECS: float = 30.0
_DAEMON_PROBE_TIMEOUT: float = 2.0


def _check_daemon_health() -> None:
    """TCP-probe the opencode daemon; raise QoderCLIDaemonError if unreachable.

    Uses a circuit-breaker pattern: after a failure, skip probes for
    ``_DAEMON_COOLDOWN_SECS`` seconds to avoid N * probe_timeout latency
    when processing many files (e.g. 80 files × 2s = 160s wasted).
    """
    global _DAEMON_COOLDOWN_UNTIL

    now = time.monotonic()
    if now < _DAEMON_COOLDOWN_UNTIL:
        # Still in cooldown — daemon was recently unreachable.
        raise QoderCLIDaemonError(
            "opencode daemon unreachable (cached); "
            "daemon was down within last {:.0f}s — skipping probe".format(
                _DAEMON_COOLDOWN_SECS
            )
        )

    daemon_url = os.environ.get("OPENCODE_URL") or config.opencode_url
    parsed = urlparse(daemon_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 4096

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(_DAEMON_PROBE_TIMEOUT)
        s.connect((host, port))
        s.close()
        logger.debug("qodercli daemon health OK {}:{}", host, port)
        return
    except (socket.timeout, socket.error, OSError) as e:
        _DAEMON_COOLDOWN_UNTIL = time.monotonic() + _DAEMON_COOLDOWN_SECS
        raise QoderCLIDaemonError(
            "opencode daemon unreachable at {}:{} ({}). "
            "Is `opencode acp` running? Cooldown {:.0f}s.".format(
                host, port, e, _DAEMON_COOLDOWN_SECS
            )
        ) from e


def _resolve_script_path() -> str:
    """Resolve qodercli.js 路径，优先级: 配置 > $(which qodercli) > 空.

    为什么用 readlink: npm global install 通常在 bin/ 放个 shim wrapper,
    真身在 ../lib/node_modules/@qoder-ai/qodercli/bundle/qodercli.js.
    """
    cfg = config.qodercli_js_path.strip() if config.qodercli_js_path else ""
    if cfg and os.path.isfile(cfg):
        return cfg
    wrapper = shutil.which("qodercli")
    if wrapper:
        return os.path.realpath(wrapper)
    return ""


def _resolve_paths(
    node: str | None, script: str | None, model: str | None
) -> tuple[str, str, str]:
    """解析 node / qodercli.js / model 路径, 全部支持显式参数 > env > PATH fallback."""
    node = node or config.qodercli_node_path or shutil.which("node") or ""
    script = script or _resolve_script_path()
    model = model or config.qodercli_model
    if not node or not script:
        raise QoderCLIError(
            "qodercli subprocess driver requires node + qodercli.js on PATH "
            "(or set QODERCLI_NODE_PATH / QODERCLI_JS_PATH env)"
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
    # 用 uuid 避免同毫秒撞名（并发场景 / 快速重试）
    attachment = workdir / f".__qodercli_attach_{uuid.uuid4().hex[:16]}.diff"
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


_LINE_NUMBER_PREFIX = re.compile(r"^\s*\d+\|\s?")


def _strip_line_number_prefix(text: str) -> str:
    """剥离 qodercli 偶发 stdout 行号前缀 (e.g. ``15| def foo():``).

    DeepSeek-V4-Flash 经 qodercli 输出时, 部分 chunk 会把 stdout 当 code-cat
    展示: 每行前面带 ``<行号>| `` 前缀 (类似 ``15| def collect_name(...)``).
    这种前缀会污染 ``_extract_inner_json``, 整 chunk 解析失败 → 该文件所有
    检视建议丢失 (MR 239 core.py 4 条 AGENTS/通用 bug 全丢即为此场景).

    启发式: 至少 50% 行 + >=2 行匹配才执行剥离, 避免误删 JSON 里
    ``"k": 42`` 这类行首数字字段.

    同时裁掉剥离后 JSON 之前残留的 prose (LLM 偶发把整段文件内容回显后再
    输出 JSON), 让后续 ``raw_decode`` 能从 leading whitespace 后命中 JSON.
    """
    if not text:
        return text
    lines = text.split("\n")
    matched = sum(1 for ln in lines if _LINE_NUMBER_PREFIX.match(ln))
    if matched < 2 or matched < len(lines) * 0.5:
        return text
    stripped = "\n".join(_LINE_NUMBER_PREFIX.sub("", ln) for ln in lines)
    # 找 JSON 起点 (首个未被引号包裹的 ``{`` 之前必有空白或行尾)
    first_brace = stripped.find("{")
    if first_brace <= 0:
        return stripped
    # 裁掉 ``{`` 之前的 non-JSON prose. 保守: 只在前面只含 prose-like 字符时裁
    pre = stripped[:first_brace]
    if not pre.strip():
        return stripped
    # JSON 之前还有 prose 字符 → 截掉 (后续 raw_decode 仍可命中 JSON)
    return stripped[first_brace:]


def _extract_inner_json(text: str) -> object:
    """Parse qodercli inner ``result`` blob tolerating common LLM quirks.

    Real-world DeepSeek-V4-Flash (and similar) frequently emits:

    * Trailing prose after the JSON object (``{"ok": true}\nJSON generated.``).
    * Literal newline characters inside JSON string values
      (``{"title": "x", "description_md": "line 1\nline 2"}``) where
      Python 3.12 ``strict=True`` (the default since 3.12) rejects the
      unescaped control character.

    Strategy (mirrors opencode.client extraction):

      1. ``json.JSONDecoder(strict=False).raw_decode(text)`` — handles both
         trailing prose AND literal control chars inside string values.
      2. Whole-document ``json.loads(text, strict=False)`` as a second
         safety net when the LLM appends whitespace before prose.
      3. Re-raise ``JSONDecodeError`` so the caller can choose their own
         fallback (typically ``tolerant_markdown`` or hard-fail).

    Returns a dict/list when parsing succeeds; raises ``JSONDecodeError``
    otherwise. The result MUST be a JSON object — bare scalars are not
    a useful agent payload — but we keep ``list`` in the contract for
    future-proofing.
    """
    if not text:
        raise json.JSONDecodeError("empty text", text or "", 0)
    decoder = json.JSONDecoder(strict=False)
    try:
        obj, _end = decoder.raw_decode(text)
        if isinstance(obj, (dict, list)):
            return obj
    except json.JSONDecodeError:
        pass
    try:
        obj = json.loads(text, strict=False)
        if isinstance(obj, (dict, list)):
            return obj
    except json.JSONDecodeError:
        pass
    raise json.JSONDecodeError("no json object in inner text", text, 0)


def _unwrap_markdown_wrapper(text: str) -> str:
    """嗅探 text 是否是字面 `{"markdown": "..."}` 包装, 是则剥出 markdown.

    LLM 通过 qodercli 时, result 字段是字符串. 偶尔 LLM 把内层的 JSON
    也当字符串输出, 出现 `{"markdown": "**概述**\n\n..."}` 这种嵌套.
    直接 fallback 会把整个字面 JSON 当 markdown, 钉钉群里看到一坨裸 JSON.
    这里再嗅探一次, 找到就剥开.

    两层:
      1) 严格 JSON parse (正常 LLM 输出)
      2) fallback 正则: LLM 在 markdown value 里嵌入未转义的 `"` (例如
         代码 span 里写 "world" 时漏掉反斜杠) 导致严格 JSON 失败,
         用正则定位 wrapper, 反转义 \\n \\t \" \\\\ 等.

    与 reporting/renderer.py:_maybe_unwrap_llm_markdown 行为对齐 — 两层
    防御, 任意一处成功就剥开, 失败回退原样返回.
    """
    if not text:
        return text
    head = text.lstrip()
    if not head.startswith("{"):
        return text

    # ---- 第 1 层: 严格 JSON parse ----
    try:
        obj = json.loads(head, strict=False)
        if isinstance(obj, dict):
            md = obj.get("markdown")
            if isinstance(md, str):
                return md
        return text
    except json.JSONDecodeError:
        pass

    # ---- 第 2 层: 正则定位 value, 反转义 ----
    m = re.search(r'\{\s*"markdown"\s*:\s*"', head)
    if not m:
        return text
    val_start = m.end()
    last_brace = head.rfind("}")
    if last_brace <= val_start:
        return text
    last_quote = head.rfind('"', val_start, last_brace)
    if last_quote <= val_start:
        return text
    raw = head[val_start:last_quote]
    return re.sub(
        r"\\(.)",
        lambda mo: {
            '"': '"', "\\": "\\", "/": "/",
            "n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f",
        }.get(mo.group(1), mo.group(1)),
        raw,
    )


def _extract_input_tokens(top: dict, usage: dict) -> int:
    """best-effort 提取 input token 数.

    qodercli 不同 model 回写的 token 字段位置不统一:
      - 多数: ``usage.input_tokens``
      - dfmodel (如 DeepSeek-V4-Flash): ``usage.input_tokens`` 恒为 0,
        真实值可能在 ``modelUsage.<provider>.inputTokens`` (同样可能为 0).
    优先取 ``usage.input_tokens``, 非零即用之; 否则遍历 ``modelUsage`` 各
    provider 的 ``inputTokens`` / ``input_tokens`` 兜底. 全部为 0 时返回 0
    (表示该 provider/model 不回写 token 数, 调用方应改用 cost_credits 计量).
    """
    val = int(usage.get("input_tokens", 0) or 0)
    if val > 0:
        return val
    for nested in (top.get("modelUsage") or {}).values():
        if not isinstance(nested, dict):
            continue
        for key in ("inputTokens", "input_tokens"):
            v = int(nested.get(key, 0) or 0)
            if v > 0:
                return v
    return 0


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
        QoderCLIDaemonError: opencode daemon unreachable (health-check failed).
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

    # Fast-fail: TCP-probe daemon before spawning node (2s vs 600s subprocess hang).
    _check_daemon_health()

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
    prompt_tokens = _extract_input_tokens(top, usage)
    completion_tokens = int(usage.get("output_tokens", 0) or 0)
    # dfmodel / DeepSeek-V4-Flash 等模型 qodercli 不回写真实 token 数
    # (usage.input_tokens 恒为 0). 用 total_credits 作为唯一可信的成本信号,
    # 用 context_usage_ratio 作为上下文占用信号. 二者均来自真实返回.
    cost_credits = float(top.get("total_credits") or 0.0) or 0.0
    context_usage_ratio = float(usage.get("context_usage_ratio") or 0.0) or 0.0
    stop_reason = top.get("stop_reason")
    model_id = top.get("modelID") or model_name

    # Log usage for all calls to help diagnose token tracking issues
    logger.info(
        "qodercli.usage agent={} model={} stop_reason={} usage={} pt={} ct={} credits={:.4f} ctx_ratio={:.4f}",
        agent, model_id, stop_reason, usage, prompt_tokens, completion_tokens,
        cost_credits, context_usage_ratio,
    )

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
            cost_credits=cost_credits,
            context_usage_ratio=context_usage_ratio,
            duration_ms=duration_ms,
            model=model_id,
            raw_output=inner_text,
        )

    # `result` is a string — could be a JSON-encoded blob or plain markdown.
    # 先剥离偶发的行号前缀 (qodercli 输出形态), 再按 fence / JSON 解析.
    text = _strip_line_number_prefix(str(inner))
    text = _strip_fence(text)
    try:
        data = _extract_inner_json(text)
        if not isinstance(data, dict):
            # Bare scalar / array are not useful agent payloads — treat as
            # non-JSON so tolerant_markdown can downgrade to raw_text.
            raise json.JSONDecodeError(
                f"inner result is not a JSON object (got {type(data).__name__})",
                text,
                0,
            )
    except json.JSONDecodeError:
        if tolerant_markdown:
            # 嗅探: text 本身可能是字面 JSON 包装 `{"markdown": "..."}`
            # (LLM 在 result 字段里再嵌一层 JSON 时常见), 这种情况应该
            # 递归剥一次, 避免 caller 拿到 `{"markdown": "..."}` 字面字符串.
            stripped = _unwrap_markdown_wrapper(text)
            return LLMResult(
                data={"markdown": stripped},
                provider="qodercli",
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            cost_credits=cost_credits,
            context_usage_ratio=context_usage_ratio,
                duration_ms=duration_ms,
                model=model_id,
                raw_output=text,
            )
        raise QoderCLIOutputError(
            f"agent output result not JSON: {text[:300]}"
        )

    return LLMResult(
        data=data,
        provider="qodercli",
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_credits=cost_credits,
        context_usage_ratio=context_usage_ratio,
        duration_ms=duration_ms,
        model=model_id,
        raw_output=text,
    )
