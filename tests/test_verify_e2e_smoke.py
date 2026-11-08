"""统一 E2E harness 结构守卫 + 快速档可执行性.

- 模块拆分（stack/fixtures/flows/config_audit/report/run_e2e）保持可导入；
- witness 证据引用必须能解析；锁清理绝不碰 `reviewagent:lock:*`；
- `--tier fast` 真跑一遍并落报告（无外部依赖）。
"""
from __future__ import annotations

import dataclasses
import fnmatch
import json

import pytest

from reviewagent.config import current
from scripts.e2e import config_audit, flows, run_e2e, stack, verify_e2e

EXPECTED_FEATURES = {
    "describe",
    "improve",
    "auto_chain",
    "adopt",
    "dismiss",
    "ui_apply",
    "telemetry_api",
    "weekly_report",
}


def test_flow_registry_covers_all_features():
    assert set(flows.FLOWS) == EXPECTED_FEATURES


def test_shim_delegates_to_single_entry():
    assert verify_e2e.main is run_e2e.main


def test_witness_evidence_refs_resolve():
    rows = config_audit.static_rows()
    missing = [(row.field, row.detail) for row in rows if not row.ok]
    assert not missing, missing


def test_evidence_ref_parsing():
    assert config_audit.evidence_ref("reporting/runner.py:_resolve_tz；说明") == (
        "reporting/runner.py",
        "_resolve_tz",
    )
    assert config_audit.evidence_ref("gitlab/client.py:24；说明") == ("gitlab/client.py", "24")
    assert config_audit.evidence_ref("没有路径") is None


class _FakeRedis:
    def __init__(self, keys: list[str]) -> None:
        self.store = {key: "1" for key in keys}
        self.seen_patterns: list[str] = []

    def keys(self, pattern: str) -> list[str]:
        self.seen_patterns.append(pattern)
        return [key for key in self.store if fnmatch.fnmatch(key, pattern)]

    def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if self.store.pop(key, None) is not None:
                removed += 1
        return removed


def test_clear_ephemeral_locks_never_touches_chain_locks(monkeypatch):
    fake = _FakeRedis(
        [
            "reviewagent:cooldown:34:181",
            "reviewagent:max_review_calls:34:181",
            "reviewagent:diff_head:34:181",
            "reviewagent:lock:34:181",
        ]
    )
    monkeypatch.setattr(stack, "redis_client", lambda: fake)

    removed = stack.clear_ephemeral_locks([], pid=34)

    assert removed == 3
    assert "reviewagent:lock:34:181" in fake.store
    assert all("reviewagent:lock:*" not in pattern for pattern in fake.seen_patterns)


def _cfg(**overrides):
    return dataclasses.replace(current(), **overrides)


def test_observable_checks_enforce_limits_and_filters():
    checks = config_audit.observable_checks(
        [
            {"file": "app/service.py", "severity": "high", "rationale": "见 SSD-RULE-NAMING"},
            {"file": "app/extra.py", "severity": "medium", "rationale": ""},
        ],
        _cfg(improve_max_suggestions=2, improve_min_severity="high"),
    )
    by_name = {name: (ok, detail) for name, ok, detail in checks}
    assert by_name["suggestions_within_limit"][0]
    assert not by_name["no_below_min_severity"][0], "medium 应判为低于 high"
    assert by_name["conftest_not_reviewed"][0]
    assert by_name["rule_key_hits"][0]


def test_observable_checks_flags_conftest_and_missing_rule_keys():
    checks = config_audit.observable_checks(
        [{"file": "conftest.py", "severity": "high", "rationale": ""}],
        _cfg(improve_max_suggestions=0, improve_min_severity="", rule_key_prefix="SSD"),
    )
    by_name = {name: ok for name, ok, _ in checks}
    assert not by_name["conftest_not_reviewed"]
    assert not by_name["rule_key_hits"]
    assert "suggestions_within_limit" not in by_name, "0 = 不限，不应检查上限"


def test_fast_tier_runs_and_writes_report(tmp_path):
    code = run_e2e.main(["--tier", "fast", "--logdir", str(tmp_path)])
    assert code == 0
    reports = sorted(tmp_path.glob("report-*.json"))
    assert len(reports) == 1
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["tier"] == "fast"
    assert payload["summary"]["passed"] == payload["summary"]["total"]
    assert {row["status"] for row in payload["config"]} <= {"verified", "declared"}


def test_unknown_feature_is_rejected():
    with pytest.raises(SystemExit):
        run_e2e._parse_features("nope")
