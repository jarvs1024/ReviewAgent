"""`cross_file_refs` provider — 从 ImproveCommand 迁出的 rg 采集实现.

实现自 `reviewagent/commands/improve.py` 的 `_find_cross_file_refs` /
`_collect_cross_file_refs_for_mr` 原样迁移；门面仅保留薄委托，行为逐字节一致。

params（全部可选，缺省即 legacy 运行时常量）：

* `rg_file_type` — rg `-t` 参数；空 / None 时不传 `-t`；
* `max_symbols_per_file` — 每个 diff 文件最多参与 rg 的标识符数（legacy 15）；
* `max_refs_per_file` — 每个文件最多保留的 caller 引用数（legacy 6）；
* `hits_per_symbol` — 每个标识符最多命中数（legacy 2）；
* `timeout_seconds` — 由动态 runner 消费（整体线程超时），本模块不读；
* `limits` — 保留字段，本期不消费。

内层 rg 子进程超时保持 legacy：bulk 30s / 单文件 8s。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from reviewagent.logging_setup import logger
from reviewagent.review.policies.python import extract_identifiers

# Legacy 运行时常量（profiles/*/dynamic_rules.yaml 的 legacy-truth 值）。
DEFAULT_PARAMS: dict[str, Any] = {
    "rg_file_type": "py",
    "max_symbols_per_file": 15,
    "max_refs_per_file": 6,
    "hits_per_symbol": 2,
    "timeout_seconds": 35,
}

# Legacy excludes（顺序与 improve.py 完全一致）。
DEFAULT_EXCLUDES: tuple[str, ...] = (
    "!reviewagent/**",
    "!tests/**",
    "!.venv/**",
    "!**/__pycache__/**",
    "!.git/**",
)

_STOP_IDENTS = {"self", "cls", "None", "True", "False", "args", "kwargs"}
_BULK_TIMEOUT_S = 30
_SINGLE_TIMEOUT_S = 8
_MAX_CONTENT_CHARS = 500


def _int_param(params: dict[str, Any], name: str) -> int:
    """取正整数 param；缺失 / 非法（非数值或 `<= 0`）时回退 legacy 常量.

    零 / 负数上限会让采集静默退化成"每个 ident 只留 1 条"甚至空结果，因此
    一律 clamp 到默认值并记 WARNING（`rule_key` 由 dynamic runner 注入，仅用于
    告警归属；直接调用模块函数时缺省为 `-`）。
    """
    default = int(DEFAULT_PARAMS[name])
    raw = (params or {}).get(name, default)
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError):
        # OverflowError: YAML `.inf` / 超大 float（int(inf) 抛 OverflowError，
        # 不在 ValueError 层级下）— 与非法值同样 clamp 到默认，避免同步门面
        # 路径（improve.py → find_refs）把异常抛到调用方。
        value = default
        invalid = True
    else:
        invalid = value <= 0
        if invalid:
            value = default
    if invalid:
        logger.warning(
            "dynamic.param.invalid rule={} param={} value={} -> {}",
            (params or {}).get("rule_key", "-"), name, raw, value,
        )
    return value


def _rg_type_args(params: dict[str, Any]) -> list[str]:
    file_type = (params or {}).get("rg_file_type", DEFAULT_PARAMS["rg_file_type"])
    return ["-t", str(file_type)] if file_type else []


def _exclude_args(params: dict[str, Any]) -> list[str]:
    excludes = (params or {}).get("excludes") or DEFAULT_EXCLUDES
    args: list[str] = []
    for pattern in excludes:
        args += ["-g", str(pattern)]
    return args


def find_refs(
    file_path: str, diff: str, worktree: Any, *, params: dict[str, Any] | None = None,
) -> list[dict]:
    """在 worktree 找 diff 改动的标识符在其他文件的 caller 引用（单文件回退路径）.

    用 rg 排除当前文件, 每个 ident 限 ``hits_per_symbol`` 条命中, 总数限
    ``max_refs_per_file``. 返回 ``[{"file": rel, "line": N, "content": "...", "ident": ident}]``.
    """
    params = params or {}
    idents = extract_identifiers(diff)[: _int_param(params, "max_symbols_per_file")]
    if not idents or not worktree:
        return []
    workdir = str(Path(worktree))
    hits_per_symbol = _int_param(params, "hits_per_symbol")
    max_refs = _int_param(params, "max_refs_per_file")
    refs: list[dict] = []
    seen: set[tuple[str, int]] = set()
    rel_self = file_path
    for ident in idents:
        if len(ident) < 3:
            continue
        try:
            result = subprocess.run(
                [
                    "rg", "-n", "--no-heading", *_rg_type_args(params),
                    rf"\b{ident}\b", ".",
                    "-g", f"!{rel_self}",
                    *_exclude_args(params),
                ],
                cwd=workdir,
                capture_output=True, text=True, timeout=_SINGLE_TIMEOUT_S,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            # 单文件 rg 超时 / 缺 rg 二进制：静默跳过，整体失败由动态 runner 的
            # 超时 / 降级日志承载，避免每 ident 刷一条 WARNING。
            continue
        except Exception as exc:
            logger.warning(
                "dynamic.collect.failed provider=cross_file_refs error={}",
                type(exc).__name__,
            )
            continue
        hits = 0
        for line in result.stdout.splitlines():
            parts = line.split(":", 2)
            if len(parts) < 3:
                continue
            f, ln_s, content = parts[0], parts[1], parts[2]
            f_rel = f.lstrip("./")
            if f_rel == rel_self or not f_rel:
                continue
            try:
                ln = int(ln_s)
            except ValueError:
                continue
            key = (f_rel, ln)
            if key in seen:
                continue
            seen.add(key)
            refs.append({
                "file": f_rel,
                "line": ln,
                "content": content.strip()[:_MAX_CONTENT_CHARS],
                "ident": ident,
            })
            hits += 1
            if hits >= hits_per_symbol or len(refs) >= max_refs:
                break
        if len(refs) >= max_refs:
            break
    return refs


def collect_refs(
    files: list[str],
    diff_by_file: dict[str, str],
    worktree: Any,
    *,
    params: dict[str, Any] | None = None,
) -> dict[str, list[dict]]:
    """对整个 MR 一次性 rg 找所有 diff 文件的标识符在仓库的 caller 引用.

    Returns: ``{file_path: [{file, line, content, ident}, ...]}``
    - 一次 rg 用所有 ident 的并集作为 pattern, 避免每个文件重启 subprocess
    - 按 ident 分桶, 给每个 diff file 排除自身的 caller
    - 比 per-file 调用更全: file A 的 ident 在 file B 的 caller 也会被发现
    """
    params = params or {}
    if not worktree:
        return {fp: [] for fp in files}
    workdir = str(Path(worktree))
    max_symbols = _int_param(params, "max_symbols_per_file")
    hits_per_symbol = _int_param(params, "hits_per_symbol")
    max_refs_per_file = _int_param(params, "max_refs_per_file")

    # 1. 收集所有 ident (per file)
    idents_by_file: dict[str, set[str]] = {}
    all_idents: set[str] = set()
    for fp in files:
        idents = set(extract_identifiers(diff_by_file.get(fp, ""))[:max_symbols])
        idents_by_file[fp] = idents
        all_idents |= idents
    # 排除短 ident + 常见噪音
    all_idents = {i for i in all_idents if len(i) >= 3} - _STOP_IDENTS
    if not all_idents:
        return {fp: [] for fp in files}

    # 2. 一次 rg 找所有 ident (按 ident 分类)
    idents_sorted = sorted(all_idents)
    pattern = r"\b(?:" + "|".join(re.escape(i) for i in idents_sorted) + r")\b"
    try:
        result = subprocess.run(
            ["rg", "-n", "--no-heading", *_rg_type_args(params), pattern, ".", *_exclude_args(params)],
            cwd=workdir,
            capture_output=True, text=True, timeout=_BULK_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return {fp: [] for fp in files}
    except Exception as exc:
        logger.warning(
            "dynamic.collect.failed provider=cross_file_refs error={}",
            type(exc).__name__,
        )
        return {fp: [] for fp in files}

    # 3. 解析: 按 ident 分桶
    refs_by_ident: dict[str, list[dict]] = {i: [] for i in all_idents}
    for line in result.stdout.splitlines():
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        f, ln_s, content = parts[0], parts[1], parts[2]
        f_rel = f.lstrip("./")
        if not f_rel:
            continue
        try:
            ln = int(ln_s)
        except ValueError:
            continue
        # 一行只算一个 ident 命中 (按 ident 顺序优先)
        for ident in idents_sorted:
            if re.search(rf"\b{re.escape(ident)}\b", content):
                refs_by_ident[ident].append({
                    "file": f_rel,
                    "line": ln,
                    "content": content.strip()[:_MAX_CONTENT_CHARS],
                    "ident": ident,
                })
                break

    # 4. 给每个 diff file 分配 refs (排除自身, 每个 ident 限 hits, 总限 max_refs)
    out: dict[str, list[dict]] = {}
    for fp in files:
        my_idents = idents_by_file.get(fp, set())
        seen: set[tuple[str, int]] = set()
        refs: list[dict] = []
        for ident in my_idents:
            hits = 0
            for r in refs_by_ident.get(ident, []):
                if r["file"] == fp:
                    continue
                key = (r["file"], r["line"])
                if key in seen:
                    continue
                seen.add(key)
                refs.append(r)
                hits += 1
                if hits >= hits_per_symbol or len(refs) >= max_refs_per_file:
                    break
            if len(refs) >= max_refs_per_file:
                break
        out[fp] = refs
    return out


class CrossFileRefsProvider:
    """动态规则 provider：调用注入的 bulk 回调，扁平化并标注 `review_file`."""

    def __init__(self, fetch_callback: Any = None) -> None:
        self._fetch_callback = fetch_callback

    @staticmethod
    def _flatten(grouped: dict[str, list[dict]]) -> list[dict]:
        out: list[dict] = []
        for review_file, refs in grouped.items():
            for ref in refs:
                annotated = dict(ref)
                annotated["review_file"] = review_file
                out.append(annotated)
        return out

    def fetch(
        self,
        *,
        files: Any,
        diff_by_file: Any,
        worktree: Any,
        params: dict[str, Any] | None,
    ) -> list[dict]:
        # 门面注入的 bound method 只接受 3 个位置参数 — 不传 params。
        if self._fetch_callback is None:
            grouped = collect_refs(files, diff_by_file, worktree, params=params)
        else:
            grouped = self._fetch_callback(files, diff_by_file, worktree)
        return self._flatten(grouped)
