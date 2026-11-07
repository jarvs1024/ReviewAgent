"""性能冒烟门禁：进程内复测装配开销，并校验仓库内基线快照.

* `tests/replay/perf_baseline.json` 由 `scripts/bench_review_assembly.py` 生成，
  字段固定（iterations / assemble_seconds / build_prompt_seconds / total_seconds
  / peak_rss_mb / platform）；
* 用例内用 importlib 原地加载脚本并 `measure()` 复测：总耗时 < 2s、峰值内存
  < 200MB —— 只做数量级保护，不追求机器间绝对可比。
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
    "peak_rss_mb",
    "platform",
}
MAX_TOTAL_SECONDS = 2.0
MAX_PEAK_RSS_MB = 200.0


def _load_script():
    spec = importlib.util.spec_from_file_location("bench_review_assembly", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_committed_baseline_shape_and_gate():
    data = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert set(data) == EXPECTED_KEYS
    assert isinstance(data["iterations"], int) and data["iterations"] >= 1
    assert isinstance(data["platform"], str) and data["platform"]
    for key in ("assemble_seconds", "build_prompt_seconds", "total_seconds"):
        assert isinstance(data[key], (int, float)) and data[key] >= 0
    assert isinstance(data["peak_rss_mb"], (int, float)) and data["peak_rss_mb"] >= 0
    assert data["total_seconds"] < MAX_TOTAL_SECONDS
    assert data["peak_rss_mb"] < MAX_PEAK_RSS_MB


def test_in_process_measure_stays_within_smoke_budget():
    module = _load_script()
    result = module.measure(iterations=3)
    assert set(result) == EXPECTED_KEYS
    assert result["total_seconds"] < MAX_TOTAL_SECONDS
    assert result["peak_rss_mb"] < MAX_PEAK_RSS_MB
