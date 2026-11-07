"""性能冒烟门禁：进程内复测装配开销，并校验仓库内基线快照.

* `tests/replay/perf_baseline.json` 由 `scripts/bench_review_assembly.py` 生成，
  字段固定（iterations / assemble_seconds / build_prompt_seconds / total_seconds
  / peak_tracemalloc_kb / peak_rss_mb / platform）；
* 用例内用 importlib 原地加载脚本并 `measure()` 复测，先与提交基线比较
  （总耗时 ≤ 基线 10 倍且 ≥ 10ms 下限；tracemalloc 峰值 ≤ 基线 5 倍且
  ≥ 4MiB 下限，跨机器噪声大，只挡数量级退化），再保留绝对冒烟上限
  （总耗时 < 2s、峰值 RSS < 200MB）。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "bench_review_assembly.py"
BASELINE = REPO_ROOT / "tests" / "replay" / "perf_baseline.json"

EXPECTED_KEYS = {
    "iterations",
    "assemble_seconds",
    "build_prompt_seconds",
    "total_seconds",
    "peak_tracemalloc_kb",
    "peak_rss_mb",
    "platform",
}
MAX_TOTAL_SECONDS = 2.0
MAX_PEAK_RSS_MB = 200.0
# 相对提交基线的允许倍数（跨机器/CI 噪声）与绝对下限（基线过小时避免 0 门槛）。
TOTAL_SECONDS_FACTOR = 10.0
TOTAL_SECONDS_FLOOR = 0.01
PEAK_TRACEMALLOC_FACTOR = 5.0
PEAK_TRACEMALLOC_FLOOR_KB = 4096.0


def _load_script():
    spec = importlib.util.spec_from_file_location("bench_review_assembly", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _baseline() -> dict:
    return json.loads(BASELINE.read_text(encoding="utf-8"))


def test_committed_baseline_shape_and_gate():
    data = _baseline()
    assert set(data) == EXPECTED_KEYS
    assert isinstance(data["iterations"], int) and data["iterations"] >= 1
    assert isinstance(data["platform"], str) and data["platform"]
    for key in ("assemble_seconds", "build_prompt_seconds", "total_seconds"):
        assert isinstance(data[key], (int, float)) and data[key] >= 0
    assert (
        isinstance(data["peak_tracemalloc_kb"], (int, float))
        and data["peak_tracemalloc_kb"] >= 0
    )
    assert isinstance(data["peak_rss_mb"], (int, float)) and data["peak_rss_mb"] >= 0
    assert data["total_seconds"] < MAX_TOTAL_SECONDS
    assert data["peak_rss_mb"] < MAX_PEAK_RSS_MB


def test_in_process_measure_stays_within_baseline_and_smoke_budget():
    module = _load_script()
    result = module.measure(iterations=3)
    baseline = _baseline()

    assert set(result) == EXPECTED_KEYS
    # 相对提交基线：挡数量级退化（新机器更快/更慢不影响结论）.
    assert result["total_seconds"] <= max(
        baseline["total_seconds"] * TOTAL_SECONDS_FACTOR, TOTAL_SECONDS_FLOOR
    )
    assert result["peak_tracemalloc_kb"] <= max(
        baseline["peak_tracemalloc_kb"] * PEAK_TRACEMALLOC_FACTOR,
        PEAK_TRACEMALLOC_FLOOR_KB,
    )
    # 绝对冒烟上限：与机器无关的数量级保护.
    assert result["total_seconds"] < MAX_TOTAL_SECONDS
    assert result["peak_rss_mb"] < MAX_PEAK_RSS_MB
