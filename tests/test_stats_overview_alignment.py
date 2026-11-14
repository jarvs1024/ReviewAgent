"""D1 回归: `/mr/{p}/{i}/stats` 必须与「检视汇总」表逐字段对齐.

Why 需要:
    同一个 MR 的「采纳率」此前存在两套口径, 同一份数据能算出两个数:

      - 检视汇总 (build_overview_body): applied / grand_total
        数据源 = list_latest_by_cohort (同 cohort 只算最新一条, 排除 superseded)
      - /stats    (suggestion_stats):  adopted / processed
        数据源 = 原始 suggestions 表 GROUP BY state (不归并)

    实测 (MR !9 真实场景):
      MR !8  → 汇总表 7.1%  vs  /stats 25.0%
      MR !9  → 汇总表 20.0% vs  /stats 50.0%
      p31 项目级 → 周报 5.5% vs MR 级 API 50.0%   (差 9 倍)

    且 /stats 的 total/severity_counts 走原始表, 汇总表走归并后的 ——
    一旦某 MR 出现 cohort 归并, 「总建议数」也会对不上 (D3).

修复:
    suggestion_stats 与汇总表**同源**: 统一走 list_latest_by_cohort 且排除
    superseded; adoption_rate 统一为 applied / total (与汇总表、周报一致).
    processed 字段保留 (向后兼容, 它表达"已处理建议数").
"""
from __future__ import annotations

import os
import pathlib
import re
import tempfile

import pytest


@pytest.fixture
def tmp_telemetry(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    from reviewagent.config import config as _cfg
    monkeypatch.setattr(
        type(_cfg), "sqlite_path",
        property(lambda self: pathlib.Path(path)),
        raising=False,
    )
    from reviewagent.telemetry import store as st
    st._store = None
    yield path
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    st._store = None


def _seed(s, *, note_id, state="open", line=10, severity="medium", cohort_key=None):
    s.record_suggestion(
        project_id=34, mr_iid=263, note_id=note_id,
        file_path="a.py", target_line=line, target_line_end=line,
        existing_code="old\n", improved_code="new\n",
        header="h", label="l",
        fingerprint=f"fp-{note_id}",
        cohort_key=cohort_key or f"cohort-{note_id}",
        severity=severity, severity_source="rule", head_sha="abc",
    )
    if state != "open":
        s.update_suggestion_state(note_id, state, actor_username="tester")


def _overview_rate(body: str) -> float:
    m = re.search(r"采纳率\s*([\d.]+)%", body)
    assert m, f"汇总表正文里没有采纳率: {body!r}"
    return float(m.group(1))


def _overview_total(body: str) -> int:
    m = re.search(r"总建议数\s*(\d+)", body)
    assert m, f"汇总表正文里没有总建议数: {body!r}"
    return int(m.group(1))


# ---------------------------------------------------------------------------
# 采纳率口径
# ---------------------------------------------------------------------------

def test_stats_adoption_rate_equals_overview_rate(tmp_telemetry):
    """同一批数据, /stats 的采纳率必须与检视汇总表一致."""
    from reviewagent.commands._common import build_overview_body
    from reviewagent.telemetry.store import get_store

    s = get_store()
    _seed(s, note_id="a-1", state="open", severity="high")
    _seed(s, note_id="a-2", state="applied", severity="medium")
    _seed(s, note_id="a-3", state="dismissed", severity="low")
    _seed(s, note_id="a-4", state="resolved", severity="medium")
    _seed(s, note_id="a-5", state="open", severity="low")
    _seed(s, note_id="a-6", state="open", severity="medium")

    body = build_overview_body(project_id=34, mr_iid=263, inline_posted_count=0)
    stats = s.suggestion_stats(34, 263)

    assert stats["adoption_rate"] == _overview_rate(body), (
        f"采纳率口径不一致: /stats={stats['adoption_rate']} vs 汇总表={_overview_rate(body)}"
    )


def test_stats_total_equals_overview_total(tmp_telemetry):
    """/stats.total 必须与汇总表「总建议数」一致."""
    from reviewagent.commands._common import build_overview_body
    from reviewagent.telemetry.store import get_store

    s = get_store()
    _seed(s, note_id="t-1", state="open", severity="high")
    _seed(s, note_id="t-2", state="applied", severity="medium")
    _seed(s, note_id="t-3", state="open", severity="low")

    body = build_overview_body(project_id=34, mr_iid=263, inline_posted_count=0)
    stats = s.suggestion_stats(34, 263)

    assert stats["total"] == _overview_total(body)


def test_stats_uses_cohort_dedup(tmp_telemetry):
    """同 cohort 多条只算 1 —— /stats 与汇总表必须同源 (D3)."""
    from reviewagent.commands._common import build_overview_body
    from reviewagent.telemetry.store import get_store

    s = get_store()
    # 同 cohort 两条 (旧 open / 新 applied) → 归并后只剩最新一条 = applied
    _seed(s, note_id="cd-1", state="open", severity="high", cohort_key="ck-same")
    _seed(s, note_id="cd-2", state="applied", severity="high", cohort_key="ck-same")
    # 另一个 cohort
    _seed(s, note_id="cd-3", state="open", severity="medium", cohort_key="ck-other")

    body = build_overview_body(project_id=34, mr_iid=263, inline_posted_count=0)
    stats = s.suggestion_stats(34, 263)

    assert _overview_total(body) == 2, "汇总表应把同 cohort 折成 2 条"
    assert stats["total"] == 2, f"/stats 必须同样按 cohort 归并, got {stats['total']}"
    assert stats["state_counts"] == {"applied": 1, "open": 1}


def test_stats_severity_counts_match_overview_rows(tmp_telemetry):
    """/stats.severity_counts 必须与汇总表 severity 行一致."""
    from reviewagent.commands._common import build_overview_body
    from reviewagent.telemetry.store import get_store

    s = get_store()
    _seed(s, note_id="s-1", state="open", severity="high")
    _seed(s, note_id="s-2", state="open", severity="high")
    _seed(s, note_id="s-3", state="applied", severity="medium")
    _seed(s, note_id="s-4", state="open", severity="low")

    body = build_overview_body(project_id=34, mr_iid=263, inline_posted_count=0)
    stats = s.suggestion_stats(34, 263)

    def _row_total(label: str) -> int:
        line = next(l for l in body.splitlines() if label in l)
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        return int(cells[-1])

    assert stats["severity_counts"].get("high") == _row_total("🔴 HIGH")
    assert stats["severity_counts"].get("medium") == _row_total("🟡 MEDIUM")
    assert stats["severity_counts"].get("low") == _row_total("🟢 LOW")


# ---------------------------------------------------------------------------
# 向后兼容: processed 字段保留
# ---------------------------------------------------------------------------

def test_stats_keeps_processed_field(tmp_telemetry):
    """processed 表达"已处理建议数", 保留不动 (向后兼容)."""
    from reviewagent.telemetry.store import get_store

    s = get_store()
    _seed(s, note_id="p-1", state="open", severity="high")
    _seed(s, note_id="p-2", state="applied", severity="medium")
    _seed(s, note_id="p-3", state="dismissed", severity="low")
    _seed(s, note_id="p-4", state="resolved", severity="low")

    stats = s.suggestion_stats(34, 263)

    assert stats["processed"] == 3, "processed = applied + dismissed + resolved"
    assert stats["adopted"] == 1
    assert stats["dismissed"] == 1
    assert stats["resolved"] == 1
    assert stats["open"] == 1
    assert stats["total"] == 4


def test_stats_zero_division_safe(tmp_telemetry):
    """空 MR → 0.0%, 不抛 ZeroDivisionError."""
    from reviewagent.telemetry.store import get_store

    stats = get_store().suggestion_stats(34, 999)
    assert stats["total"] == 0
    assert stats["adoption_rate"] == 0.0
