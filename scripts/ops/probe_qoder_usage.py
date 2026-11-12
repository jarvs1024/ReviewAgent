"""探针: 直接问 qoder-sdk 上游 usage 里的 token 字段到底是什么。

Why 需要:
§7.3 原结论「上游 Qoder 服务端的 usage 响应**未回填 token 字段**」——
E6 的诊断日志打脸了: usage_keys 里**有** `input_tokens` / `output_tokens`,
但 pt=0 ct=0。所以真实情况是「键存在但值为 0 或 None」，
与「键缺失」是两回事 —— 对应的处置也不同（前者上游改了就能好，
后者要 SDK/协议层适配）。

这个探针把原始 usage 字典打出来, 把结论钉死。
用法: set -a; source config/instance.env; set +a; .venv/bin/python scripts/ops/probe_qoder_usage.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path


async def _probe() -> int:
    from reviewagent.llm.qoder_sdk_provider import _build_options, _sdk

    print("[i] 发一次最小请求以取回原始 usage ...")
    opts = _build_options(
        agent="describe",
        workdir=Path.cwd(),
        pat=os.environ["QODER_SDK_PAT"],
        cli_path=os.environ.get("QODER_SDK_CLI_PATH", ""),
        model=os.environ.get("QODER_SDK_MODEL", ""),
        max_turns=1,
        permission_mode="bypassPermissions",
        disallowed_tools=(),
    )
    seen: list[dict] = []
    async for message in _sdk.query(prompt="Reply with exactly: OK", options=opts):
        for src in (getattr(message, "usage", None),):
            if src:
                try:
                    seen.append({str(k): v for k, v in dict(src).items()})
                except Exception:  # noqa: BLE001
                    seen.append({"_raw": repr(src)[:500]})
        if type(message).__name__ == "ResultMessage":
            rm_usage = getattr(message, "usage", None)
            if rm_usage:
                try:
                    seen.append({str(k): v for k, v in dict(rm_usage).items()})
                except Exception:  # noqa: BLE001
                    seen.append({"_raw": repr(rm_usage)[:500]})
            break

    if not seen:
        print("[!] 没取到任何 usage —— 上游这次压根没回 usage")
        return 1

    print(f"[i] 取到 {len(seen)} 份 usage:")
    for i, u in enumerate(seen):
        print(f"  --- usage[{i}] ---")
        for k in sorted(u):
            v = u[k]
            if isinstance(v, (dict, list)):
                v = json.dumps(v, ensure_ascii=False)[:120]
            print(f"    {k} = {v!r}")

    print()
    print("[*] token 字段判定:")
    for i, u in enumerate(seen):
        it = u.get("input_tokens", "<缺失>")
        ot = u.get("output_tokens", "<缺失>")
        print(f"  usage[{i}]: input_tokens={it!r} output_tokens={ot!r}")
    return 0


def main() -> int:
    for k in ("QODER_SDK_PAT", "QODER_SDK_CLI_PATH"):
        if not os.environ.get(k):
            print(f"[!] missing env var: {k}; source config/instance.env first")
            return 2
    try:
        return asyncio.run(_probe())
    except Exception as e:  # noqa: BLE001
        print(f"[!] 探针失败: {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
