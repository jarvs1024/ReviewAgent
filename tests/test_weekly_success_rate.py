"""周报运行统计: 成功率口径 (跳过算成功) —— 2026-11-25.

Why: skipped 不是失败 (它是「本次不跑」: 锁超时/去重/无变化),
     算失败会让成功率虚低, 且与 TestMate 主页的
     (success + skipped) / total 口径不一致 (同一份数据两个数).
"""
from __future__ import annotations

from reviewagent.reporting.collectors.telemetry import TelemetryCollector


def _row(status: str, **kw):
    base = {
        "command": "improve",
        "status": status,
        "duration_ms": 1000,
        "project_id": 7,
        "mr_iid": 1,
        "started_at": "2026-11-24T00:00:00+00:00",
        "error": "",
        "mr_title": "t",
        "mr_author": "a",
    }
    base.update(kw)
    return base


class TestSuccessRate:
    def test_skipped_counts_as_success(self):
        """88 成功 + 60 跳过 + 0 失败 = 148 总 → 成功率 100%."""
        rows = [_row("success") for _ in range(88)]
        rows += [_row("skipped") for _ in range(60)]
        agg = TelemetryCollector._aggregate(rows)
        assert agg["total"] == 148
        assert agg["success"] == 88
        assert agg["skipped"] == 60
        assert agg["failed"] == 0
        # 跳过算成功 → (88 + 60) / 148 = 100%
        assert agg["success_rate"] == 100.0

    def test_old_would_have_been_59_5(self):
        """回归: 旧口径 (success / total) 是 59.5% —— 正是用户看到的那个数."""
        rows = [_row("success") for _ in range(88)]
        rows += [_row("skipped") for _ in range(60)]
        agg = TelemetryCollector._aggregate(rows)
        strict = agg["success_rate_strict"]
        assert strict == 59.5, strict
        # 新口径更大
        assert agg["success_rate"] > strict

    def test_failed_still_counts_as_failure(self):
        rows = [_row("success") for _ in range(8)]
        rows += [_row("skipped") for _ in range(1)]
        rows += [_row("failed") for _ in range(1)]
        agg = TelemetryCollector._aggregate(rows)
        # (8 + 1) / 10 = 90%
        assert agg["success_rate"] == 90.0
        assert agg["success_rate_strict"] == 80.0

    def test_timeout_counts_as_failed(self):
        rows = [_row("success"), _row("timeout")]
        agg = TelemetryCollector._aggregate(rows)
        assert agg["failed"] == 1
        assert agg["success_rate"] == 50.0

    def test_running_in_denominator_not_numerator(self):
        """running 不算成功也不算失败, 但留在分母里 (不被高估)."""
        rows = [_row("success"), _row("running")]
        agg = TelemetryCollector._aggregate(rows)
        assert agg["running"] == 1
        assert agg["success_rate"] == 50.0

    def test_empty_rows(self):
        agg = TelemetryCollector._aggregate([])
        assert agg["total"] == 0
        assert agg["success_rate"] == 0.0
        assert agg["success_rate_strict"] == 0.0

    def test_all_skipped(self):
        rows = [_row("skipped") for _ in range(5)]
        agg = TelemetryCollector._aggregate(rows)
        assert agg["success_rate"] == 100.0
        assert agg["success_rate_strict"] == 0.0

    def test_by_command_counts_unchanged(self):
        """分桶计数不受口径变化影响."""
        rows = [_row("success", command="improve"),
                _row("skipped", command="improve"),
                _row("failed", command="describe")]
        agg = TelemetryCollector._aggregate(rows)
        assert agg["by_command"]["improve"]["success"] == 1
        assert agg["by_command"]["improve"]["skipped"] == 1
        assert agg["by_command"]["describe"]["failed"] == 1
