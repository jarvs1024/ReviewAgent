"""Unit tests for cohort dedup (list_latest_by_cohort, count_hidden_by_cohort).

Background:
    cohort dedup 让"同问题被多轮 V1/V2/V3 重复发布"只算 1 个状态 — MR 249 实测
    之前出现 22 条 applied 但实际只有 ~13 个独立问题. 此后用 cohort_key + 最新一条
    作为聚合粒度.
"""
from __future__ import annotations

import os
import pathlib
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


def _seed(s, *, note_id, cohort_key, state="open", head_sha="abc"):
    s.record_suggestion(
        project_id=34, mr_iid=263, note_id=note_id,
        file_path="a.py", target_line=10, target_line_end=10,
        existing_code="x\n", improved_code="y\n",
        header="h", label="l",
        fingerprint=f"fp-{note_id}",
        cohort_key=cohort_key,
        severity="medium", severity_source="rule", head_sha=head_sha,
    )
    if state != "open":
        s.update_suggestion_state(note_id, state, actor_username="tester")


def test_list_latest_by_cohort_dedupes_same_cohort(tmp_telemetry):
    """同 cohort_key 多条 → 只返回最新 1 条."""
    from reviewagent.telemetry.store import get_store
    s = get_store()
    # 同 cohort "ck-A" 3 条
    _seed(s, note_id="co-A1", cohort_key="ck-A")
    _seed(s, note_id="co-A2", cohort_key="ck-A")
    _seed(s, note_id="co-A3", cohort_key="ck-A")
    # 不同 cohort "ck-B" 1 条
    _seed(s, note_id="co-B1", cohort_key="ck-B")

    result = s.list_latest_by_cohort(project_id=34, mr_iid=263)

    # 期望 2 条 (ck-A 最新 1 + ck-B 1)
    assert len(result) == 2, f"expected 2 unique cohorts, got {len(result)}: {result}"
    note_ids = {r["note_id"] for r in result}
    assert note_ids == {"co-A3", "co-B1"}  # 最新的 A3 + 唯一 B1


def test_list_latest_by_cohort_excludes_superseded(tmp_telemetry):
    """state=superseded 的不参与聚合."""
    from reviewagent.telemetry.store import get_store
    s = get_store()
    _seed(s, note_id="ck1", cohort_key="ck-X", state="superseded")
    _seed(s, note_id="ck2", cohort_key="ck-Y")

    result = s.list_latest_by_cohort(project_id=34, mr_iid=263)

    assert len(result) == 1
    assert result[0]["note_id"] == "ck2"


def test_list_latest_by_cohort_no_cohort_key_uses_note_id(tmp_telemetry):
    """没 cohort_key 时, 按 note_id 聚合 (fallback)."""
    from reviewagent.telemetry.store import get_store
    s = get_store()
    _seed(s, note_id="n1", cohort_key="")
    _seed(s, note_id="n2", cohort_key="")

    result = s.list_latest_by_cohort(project_id=34, mr_iid=263)
    assert len(result) == 2  # 各算一个


def test_list_latest_by_cohort_picks_latest_state(tmp_telemetry):
    """同 cohort 多条, 最新 1 条 (id DESC) 的状态被采用."""
    from reviewagent.telemetry.store import get_store
    s = get_store()
    _seed(s, note_id="lt-1", cohort_key="ck-LT", state="open")  # 最旧
    _seed(s, note_id="lt-2", cohort_key="ck-LT", state="applied")  # 最新

    result = s.list_latest_by_cohort(project_id=34, mr_iid=263)
    assert len(result) == 1
    assert result[0]["note_id"] == "lt-2"
    assert result[0]["state"] == "applied"


def test_count_hidden_by_cohort(tmp_telemetry):
    """count_hidden_by_cohort 统计被 cohort 折掉的非最新记录数."""
    from reviewagent.telemetry.store import get_store
    s = get_store()
    # ck-Hide 同 cohort 3 条 → 2 条被隐藏 (最新 1 条保留)
    _seed(s, note_id="hd-1", cohort_key="ck-Hide")
    _seed(s, note_id="hd-2", cohort_key="ck-Hide")
    _seed(s, note_id="hd-3", cohort_key="ck-Hide")

    count = s.count_hidden_by_cohort(project_id=34, mr_iid=263)
    # 3 条同 cohort, 1 条保留 (最新), 2 条被隐藏
    assert count == 2


def test_count_hidden_by_cohort_no_dup(tmp_telemetry):
    """没有 cohort 重复时, hidden=0."""
    from reviewagent.telemetry.store import get_store
    s = get_store()
    _seed(s, note_id="nd-1", cohort_key="ck-A")
    _seed(s, note_id="nd-2", cohort_key="ck-B")
    _seed(s, note_id="nd-3", cohort_key="ck-C")

    assert s.count_hidden_by_cohort(project_id=34, mr_iid=263) == 0


def test_count_superseded_in_mr(tmp_telemetry):
    """count_superseded_in_mr 只算显式 superseded 的."""
    from reviewagent.telemetry.store import get_store
    s = get_store()
    _seed(s, note_id="sup-1", cohort_key="ck-S1", state="superseded")
    _seed(s, note_id="sup-2", cohort_key="ck-S2", state="superseded")
    _seed(s, note_id="norm-1", cohort_key="ck-N1", state="open")
    _seed(s, note_id="app-1", cohort_key="ck-A1", state="applied")

    count = s.count_superseded_in_mr(project_id=34, mr_iid=263)
    assert count == 2


def test_cohort_dedup_with_mixed_severities(tmp_telemetry):
    """不同 severity 的同 cohort 仍合并 (按 cohort_key)."""
    from reviewagent.telemetry.store import get_store
    s = get_store()
    _seed(s, note_id="mix-1", cohort_key="ck-mix")
    # 改 severity (sql UPDATE 因为 record_suggestion 默认 medium)
    import sqlite3
    conn = sqlite3.connect(str(pathlib.Path(tmp_telemetry).resolve()))
    conn.execute("UPDATE suggestions SET severity='high' WHERE note_id='mix-1'")
    conn.execute(
        "INSERT INTO suggestions (project_id, mr_iid, note_id, file_path, target_line, "
        "target_line_end, existing_code, improved_code, header, severity, head_sha, "
        "fingerprint, cohort_key, severity_source, posted_at, state, created_at) "
        "VALUES (34, 263, 'mix-2', 'a.py', 10, 10, 'x', 'y', 'h', 'low', 'abc', "
        "'fp-mix-2', 'ck-mix', 'rule', '2026-01-01', 'open', '2026-01-01')"
    )
    conn.commit()
    conn.close()

    result = s.list_latest_by_cohort(project_id=34, mr_iid=263)
    # 同 cohort 2 条 (severity 不影响 cohort_key 聚合) → 只 1 条
    assert len(result) == 1
    # 最新的 mix-2 (severity=low) 被选中
    assert result[0]["note_id"] == "mix-2"


# ============================================================
# Batch6 / MR263 回归: terminal state 冲突时不 dedup
# ============================================================

def test_list_latest_by_cohort_preserves_conflicting_terminal_states(tmp_telemetry):
    """同 cohort 内 applied + dismissed 冲突 → 两个 terminal 都保留.

    Background (MR263):
        用户 /dismiss 特色班 关闭了一条 HIGH, 但同 cohort 里有更新一条被 late_detect
        翻 applied. 修复前 list_latest_by_cohort 按 id DESC 只取最新, dismissed 被隐藏,
        检视汇总 "已忽略" 显示 0 但实际有 1. 修复后 terminal 冲突时全部保留.
    """
    from reviewagent.telemetry.store import get_store
    s = get_store()
    # 同 cohort "ck-Conflict" 3 条, 状态分布:
    _seed(s, note_id="cf-1", cohort_key="ck-Conflict", state="open")          # 最旧, 待处理
    _seed(s, note_id="cf-2", cohort_key="ck-Conflict", state="dismissed")     # 用户 dismiss
    _seed(s, note_id="cf-3", cohort_key="ck-Conflict", state="applied")       # 最新, late_detect

    result = s.list_latest_by_cohort(project_id=34, mr_iid=263)

    # 期望: cf-3 (latest) + cf-2 (terminal conflict 保留) = 2 条
    # cf-1 (open) 被隐藏 (terminal 冲突里 open 不算 terminal, 不保留)
    note_ids = {r["note_id"] for r in result}
    assert note_ids == {"cf-2", "cf-3"}, (
        f"expected dismissed + applied both preserved, got {note_ids}"
    )


def test_list_latest_by_cohort_dedupes_when_same_terminal_state(tmp_telemetry):
    """同 cohort 多条同 terminal state → 仍然只取最新 (普通 dedup 不受影响)."""
    from reviewagent.telemetry.store import get_store
    s = get_store()
    _seed(s, note_id="same-1", cohort_key="ck-Same", state="dismissed")
    _seed(s, note_id="same-2", cohort_key="ck-Same", state="dismissed")
    _seed(s, note_id="same-3", cohort_key="ck-Same", state="dismissed")

    result = s.list_latest_by_cohort(project_id=34, mr_iid=263)
    assert len(result) == 1
    assert result[0]["note_id"] == "same-3"


def test_list_latest_by_cohort_dedupes_when_all_open(tmp_telemetry):
    """同 cohort 多条全部 open → 仍然只取最新."""
    from reviewagent.telemetry.store import get_store
    s = get_store()
    _seed(s, note_id="ao-1", cohort_key="ck-AllOpen", state="open")
    _seed(s, note_id="ao-2", cohort_key="ck-AllOpen", state="open")
    _seed(s, note_id="ao-3", cohort_key="ck-AllOpen", state="open")

    result = s.list_latest_by_cohort(project_id=34, mr_iid=263)
    assert len(result) == 1
    assert result[0]["note_id"] == "ao-3"


def test_list_latest_by_cohort_preserves_three_terminal_conflict(tmp_telemetry):
    """同 cohort 三种 terminal state (applied + dismissed + resolved) → 全保留."""
    from reviewagent.telemetry.store import get_store
    s = get_store()
    _seed(s, note_id="tri-1", cohort_key="ck-Tri", state="dismissed")
    _seed(s, note_id="tri-2", cohort_key="ck-Tri", state="applied")
    _seed(s, note_id="tri-3", cohort_key="ck-Tri", state="resolved")

    result = s.list_latest_by_cohort(project_id=34, mr_iid=263)
    note_ids = {r["note_id"] for r in result}
    # latest (tri-3) + 所有 terminal (3 条全 terminal) = 3 条
    assert note_ids == {"tri-1", "tri-2", "tri-3"}, (
        f"expected all 3 terminal states preserved, got {note_ids}"
    )


def test_list_latest_by_cohort_open_does_not_trigger_conflict(tmp_telemetry):
    """open 不算 terminal, applied + open 不算冲突, 只取最新."""
    from reviewagent.telemetry.store import get_store
    s = get_store()
    _seed(s, note_id="oa-1", cohort_key="ck-OpenApp", state="open")
    _seed(s, note_id="oa-2", cohort_key="ck-OpenApp", state="applied")

    result = s.list_latest_by_cohort(project_id=34, mr_iid=263)
    assert len(result) == 1
    assert result[0]["note_id"] == "oa-2"  # 只取最新, 没有 conflict
