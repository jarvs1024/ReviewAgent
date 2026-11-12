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
from scripts.e2e import config_audit, flows, report, run_e2e, stack, verify_e2e

EXPECTED_FEATURES = {
    "describe",
    "improve",
    "auto_chain",
    "adopt",
    "dismiss",
    "ui_apply",
    "telemetry_api",
    "weekly_report",
    "weekly_notify_gate",
    "applicability",
    "cooldown",
    "rate_limit",
    "dedup",
    "push_dispatch",
    "apply_closure",
    "reconcile_resolved",
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
            {"file": "app/extra.py", "severity": "medium", "rationale": "R-ERR: 裸 except"},
        ],
        _cfg(improve_max_suggestions=2, improve_min_severity="high"),
    )
    by_name = {name: (ok, detail) for name, ok, detail in checks}
    assert by_name["suggestions_present"][0]
    assert by_name["suggestions_within_limit"][0]
    assert not by_name["no_below_min_severity"][0], "medium 应判为低于 high"
    assert by_name["conftest_skip_policy_respected"][0]
    assert by_name["rule_key_cited"][0]


def test_observable_checks_flags_conftest_and_missing_rule_keys():
    """默认策略把 conftest.py 列入 skip_paths → conftest 却出了建议 = 违规."""
    checks = config_audit.observable_checks(
        [{"file": "conftest.py", "severity": "high", "rationale": "no key"}],
        _cfg(improve_max_suggestions=0, improve_min_severity="", rule_key_prefix="SSD"),
    )
    by_name = {name: ok for name, ok, _ in checks}
    assert not by_name["conftest_skip_policy_respected"], "默认 skip_paths 含 conftest.py，不该有建议"
    assert not by_name["rule_key_cited"]
    assert "suggestions_within_limit" not in by_name, "0 = 不限，不应检查上限"


def test_observable_checks_allows_conftest_when_policy_does_not_skip_it():
    """conftest.py 是否参与检视由 skip_paths 决定 —— 不写死「一定不检视」.

    Why: 场景包策略已改为「conftest.py 不跳过 —— fixture 是测试业务的真实契约面」,
    旧断言把「conftest 一律不检视」写死, 策略变更后变成假阳性.
    """
    checks = config_audit.observable_checks(
        [{"file": "conftest.py", "severity": "medium", "rationale": "R-ERR"}],
        _cfg(improve_skip_test_paths=("tests/", "docs/")),
    )
    by_name = {name: ok for name, ok, _ in checks}
    assert by_name["conftest_skip_policy_respected"], "未被 skip_paths 跳过 → 有 conftest 建议是正常的"


def test_observable_checks_flags_conftest_when_policy_skips_it():
    """conftest 命中 skip_paths 却仍有建议 → 必须报错."""
    checks = config_audit.observable_checks(
        [{"file": "tests/conftest.py", "severity": "medium", "rationale": "R-ERR"}],
        _cfg(improve_skip_test_paths=("tests/",)),
    )
    by_name = {name: ok for name, ok, _ in checks}
    assert not by_name["conftest_skip_policy_respected"], "被 skip_paths 跳过的文件不该有建议"


def test_observable_checks_require_published_suggestions():
    checks = config_audit.observable_checks([], _cfg(improve_max_suggestions=0))
    by_name = {name: ok for name, ok, _ in checks}
    assert not by_name["suggestions_present"]


def test_fast_tier_runs_and_writes_report(tmp_path):
    code = run_e2e.main(["--tier", "fast", "--logdir", str(tmp_path)])
    assert code == 0
    reports = sorted(tmp_path.glob("report-*.json"))
    assert len(reports) == 1
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["tier"] == "fast"
    assert payload["summary"]["passed"] == payload["summary"]["total"]
    assert {row["status"] for row in payload["config"]} <= {"verified", "declared"}


def test_report_log_name_sanitises_slashes(tmp_path):
    result = report.FeatureResult("/describe", 1, 1, True, "ok", ["line"])
    path = report.write_report(tmp_path, "S", [result], tier="fast")
    assert path.exists()
    assert (tmp_path / "run-describe-r1-S.log").exists()


def test_unknown_feature_is_rejected():
    with pytest.raises(SystemExit):
        run_e2e._parse_features("nope")


# ---------------------------------------------------------------------------
# E2E 安全闸: 默认绝不能真发钉钉
# ---------------------------------------------------------------------------


def test_real_push_is_opt_in_only(monkeypatch):
    """不带显式 opt-in 时, E2E 绝不允许真发钉钉.

    Why: 本地 config/instance.env 里 REVIEWAGENT_WEEKLY_DINGTALK_DRY_RUN=false
    且带真实 webhook URL —— E2E 带 --push 跑周报会把报告推到团队群。
    """
    monkeypatch.delenv(flows.ALLOW_REAL_PUSH_ENV, raising=False)
    assert flows._e2e_real_push_allowed() is False
    for value in ("0", "false", "no", "", "  "):
        monkeypatch.setenv(flows.ALLOW_REAL_PUSH_ENV, value)
        assert flows._e2e_real_push_allowed() is False
    for value in ("1", "true", "yes", "ON"):
        monkeypatch.setenv(flows.ALLOW_REAL_PUSH_ENV, value)
        assert flows._e2e_real_push_allowed() is True


def test_delivery_dry_run_detection():
    """CLI 的 delivery 输出格式变了, 门控断言必须跟着失效（否则静默放过真发）."""
    assert flows._delivery_is_dry_run("[ok] delivery     : dry_run, chunks=1")
    assert flows._delivery_is_dry_run("[ok] delivery : dry_run, chunks=2")
    # 真发的输出格式 —— 必须识别为「不是 dry_run」
    assert not flows._delivery_is_dry_run(
        "[ok] delivery     : notifier=dingtalk success=True sent=1/1"
    )
    assert not flows._delivery_is_dry_run("")
