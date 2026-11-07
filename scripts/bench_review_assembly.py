"""装配性能基准：`assemble_base(默认 profile)` + `build_chunk_prompt` 均值.

用途（spec 性能门禁）：`tests/replay/test_perf_smoke.py` 在进程内调用
`measure()` 复测，并与仓库内固定的 `tests/replay/perf_baseline.json` 对照，
防止 prompt 模板 / 规则装配在后续重构中退化成数量级的额外开销。

运行方式（生成 / 刷新基线）::

    .venv/bin/python scripts/bench_review_assembly.py

脚本自身不依赖 pytest；独立运行时自动补齐最小 env（不读写真实 GitLab）。
``measure`` 内部惰性 import，模块导入本身无副作用（便于测试用 importlib 加载）。
"""
from __future__ import annotations

import json
import os
import platform
import resource
import sys
from pathlib import Path
from statistics import mean
from time import perf_counter
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = REPO_ROOT / "tests" / "replay" / "perf_baseline.json"

# 独立运行时的最小 env（与 tests/conftest.py 的基线一致，仅用于 import 期校验）。
_BASELINE_ENV = {
    "GITLAB_URL": "https://gitlab.example.test",
    "GITLAB_PERSONAL_ACCESS_TOKEN": "bench-token",
    "GITLAB_WEBHOOK_SECRET": "bench-secret",
    "QODERCLI_NODE_PATH": "/usr/bin/node",
    "QODERCLI_JS_PATH": "/opt/qodercli.js",
}

BASELINE_KEYS = (
    "iterations",
    "assemble_seconds",
    "build_prompt_seconds",
    "total_seconds",
    "peak_rss_mb",
    "platform",
)


def _peak_rss_mb() -> float:
    """进程峰值 RSS（MB）；darwin 的 ru_maxrss 单位是字节，Linux 是 KB."""
    raw = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    if sys.platform == "darwin":
        return raw / (1024 * 1024)
    return raw / 1024


def _chunk_parts() -> Any:
    from reviewagent.review.assembly import ChunkPromptParts

    return ChunkPromptParts(
        cross_file_section="## 跨文件影响分析\n\n- `caller.py:12` 引用 `handler`",
        rules_block="### 通用规则\n\n| 键 | 类别 |\n|---|---|\n| `R-RES` | 资源句柄 |",
        prior_section="",
        file_path="core/main.py",
        file_diff="@@ -1,2 +1,2 @@\n-a = 1\n+a = 2",
        source_block="   1| a = 1\n   2| b = 2",
        valid_lines="core/main.py: [2]",
    )


def measure(iterations: int = 5) -> dict[str, Any]:
    """测 `assemble_base` 与 `build_chunk_prompt` 的平均耗时 + 峰值 RSS."""
    if iterations <= 0:
        raise ValueError(f"iterations must be positive: {iterations}")

    from reviewagent.review.assembly import build_chunk_prompt
    from reviewagent.scenario.profile import load_profile
    from reviewagent.scenario.rules import resolver

    profile = load_profile("ssd-python-autotest")
    parts = _chunk_parts()

    assemble_samples: list[float] = []
    prompt_samples: list[float] = []
    rendered = ""
    for _ in range(iterations):
        started = perf_counter()
        resolver.assemble_base(profile)
        assemble_samples.append(perf_counter() - started)

        started = perf_counter()
        rendered = build_chunk_prompt(parts, profile=profile)
        prompt_samples.append(perf_counter() - started)
    if "core/main.py" not in rendered:
        raise RuntimeError("benchmark prompt lost the file path placeholder")

    assemble_s = mean(assemble_samples)
    prompt_s = mean(prompt_samples)
    return {
        "iterations": iterations,
        "assemble_seconds": round(assemble_s, 6),
        "build_prompt_seconds": round(prompt_s, 6),
        "total_seconds": round(assemble_s + prompt_s, 6),
        "peak_rss_mb": round(_peak_rss_mb(), 2),
        "platform": platform.system().lower(),
    }


def write_baseline(path: Path = BASELINE_PATH, *, iterations: int = 5) -> dict[str, Any]:
    result = measure(iterations=iterations)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return result


def main() -> int:
    for key, value in _BASELINE_ENV.items():
        os.environ.setdefault(key, value)
    result = write_baseline()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    print(f"baseline written: {BASELINE_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
