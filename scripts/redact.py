"""脱敏脚本 — 把 IP / 主机名 / MR 编号替换成占位符.

只脱敏文档 / 脚本 / plan 文件，不动源码（reviewagent/* 没真实 IP）。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path("D:/Code/ReviewAgent")

REPLACEMENTS: list[tuple[str, str]] = [
    (r"10\.20\.27\.7", "gitlab.internal"),
    (r"10\.20\.68\.86", "ops-host.internal"),
    (r"10\.20\.68\.126", "ops-gateway.internal"),
    (r"10\.20\.62\.37", "pypi-mirror.internal"),
    (r"ai-test-86", "ops-host"),
    (r"MR 1049", "MR `<id>`"),
    (r"epc/dml_epc_auto", "`<group>/<project>`"),
    (r"workflow@10\.x\.x\.86", "deploy@ops-host.internal"),
    (r"root@10\.20\.68\.86", "deploy@ops-host.internal"),
    (r"\b2268\b", "reviewer"),
]

TARGETS = [
    "docs/DEPLOYMENT.md",
    "docs/STATUS.md",
    "docs/QUICKSTART.md",
    "README.md",
    "deploy.sh",
    "plans/glistening-gathering-perlis.md",
]


def redact(text: str) -> tuple[str, int]:
    n = 0
    for pat, repl in REPLACEMENTS:
        text, k = re.subn(pat, repl, text)
        n += k
    return text, n


def main() -> None:
    total = 0
    for rel in TARGETS:
        p = ROOT / rel
        if not p.exists():
            print(f"  SKIP (not found): {rel}")
            continue
        orig = p.read_text(encoding="utf-8")
        new, n = redact(orig)
        if n > 0:
            p.write_text(new, encoding="utf-8")
            print(f"  REDACTED {n:3d} hits: {rel}")
            total += n
        else:
            print(f"  no change        : {rel}")
    print(f"\nTotal replacements: {total}")


if __name__ == "__main__":
    main()