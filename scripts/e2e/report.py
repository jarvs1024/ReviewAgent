"""E2E 结果模型与报告落盘（json + 每 feature 明细日志）."""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class FeatureResult:
    feature: str
    round: int
    mr_iid: int
    passed: bool
    detail: str
    log: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ConfigRow:
    """一条配置生效证据（witness 目录 + 快速档 delta 的组合）."""

    field: str
    kind: str
    status: str  # verified | declared | script | missing
    evidence: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status != "missing"


def stamp_now() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def default_logdir() -> Path:
    return REPO_ROOT / "logs" / "e2e"


def print_result(result: FeatureResult) -> None:
    marker = "✅" if result.passed else "❌"
    print(
        f"  {marker} {result.feature} round={result.round} mr={result.mr_iid} "
        f"passed={result.passed} | {result.detail[:200]}"
    )


def summarize(results: list[FeatureResult]) -> tuple[int, int]:
    return sum(1 for r in results if r.passed), len(results)


def write_report(
    logdir: Path,
    stamp: str,
    results: list[FeatureResult],
    *,
    tier: str,
    config_rows: list[ConfigRow] | None = None,
    preflight: list[tuple[str, bool, str]] | None = None,
    extra: dict[str, Any] | None = None,
) -> Path:
    """写 `report-<stamp>.json` + 每个 feature 的明细日志；返回报告路径."""
    logdir.mkdir(parents=True, exist_ok=True)
    for result in results:
        log_path = logdir / f"run-{result.feature}-r{result.round}-{stamp}.log"
        log_path.write_text("\n".join(result.log), encoding="utf-8")

    passed, total = summarize(results)
    payload: dict[str, Any] = {
        "tier": tier,
        "stamp": stamp,
        "summary": {"passed": passed, "total": total},
        "preflight": [
            {"check": name, "ok": ok, "detail": detail} for name, ok, detail in (preflight or [])
        ],
        "config": [dataclasses.asdict(row) for row in (config_rows or [])],
        "results": [dataclasses.asdict(result) for result in results],
    }
    if extra:
        payload.update(extra)
    path = logdir / f"report-{stamp}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
