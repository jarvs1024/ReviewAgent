"""Unit tests for Store.list_latest_by_cohort — cohort 归并逻辑.

回归 MR299: 'terminal + open 共存' 的 cohort, 旧版本 terminal (applied/resolved/dismissed)
不能被新一代 open 覆盖. 用户对老版本做的明确动作必须保留到检视汇总.
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from reviewagent.telemetry.store import Store


@pytest.fixture
def store(tmp_path) -> Store:
    db = tmp_path / "test.db"
    s = Store(db)
    return s


def _insert_suggestion(
    conn, *, id: int, project_id: int = 34, mr_iid: int = 299,
    cohort_key: str = "", file_path: str = "x.py", target_line: int = 1,
    severity: str = "high", state: str = "open", note_id: str | None = None,
) -> int:
    """直接 insert 一条 suggestion, 跳过 record_suggestion 校验."""
    nid = note_id or f"note_{id}"
    conn.execute(
        """
        INSERT INTO suggestions (
            project_id, mr_iid, note_id, file_path, target_line, head_sha,
            severity, state, cohort_key, created_at, header
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (project_id, mr_iid, nid, file_path, target_line, "abc123",
         severity, state, cohort_key, "2026-01-01 00:00:00.000", "header"),
    )
    return id


class TestListLatestByCohort:
    """MR299 回归: terminal + open 共存时, terminal 必须保留."""

    def test_terminal_plus_open_keeps_both(self, store: Store):
        """同 cohort 旧 applied + 新 open → 都保留 (旧 applied 不能被新 open 覆盖).

        这是 MR299 L5 cohort (id 975 applied + id 984 open) 的场景: 用户对 V1
        做了 apply, V2 又发了同位置补强建议. 检视汇总应该看到 1 applied + 1 open.
        """
        with store._conn() as conn:
            _insert_suggestion(conn, id=1, cohort_key="ck1", state="applied")
            _insert_suggestion(conn, id=2, cohort_key="ck1", state="open")
            conn.commit()

        result = store.list_latest_by_cohort(project_id=34, mr_iid=299)
        states = sorted(s["state"] for s in result)
        assert states == ["applied", "open"], (
            f"MR299 回归: applied 不能被 open 覆盖, got {states}"
        )

    def test_resolved_plus_open_keeps_both(self, store: Store):
        """MR299 L63 cohort (id 978 resolved + id 983 open): resolved 必须保留."""
        with store._conn() as conn:
            _insert_suggestion(conn, id=1, cohort_key="ck1", state="resolved")
            _insert_suggestion(conn, id=2, cohort_key="ck1", state="open")
            conn.commit()

        result = store.list_latest_by_cohort(project_id=34, mr_iid=299)
        states = sorted(s["state"] for s in result)
        assert states == ["open", "resolved"], (
            f"resolved 不能被新一代 open 覆盖, got {states}"
        )

    def test_dismissed_plus_open_keeps_both(self, store: Store):
        """MR263 旧场景: dismissed + open 共存, dismissed 必须保留."""
        with store._conn() as conn:
            _insert_suggestion(conn, id=1, cohort_key="ck1", state="dismissed")
            _insert_suggestion(conn, id=2, cohort_key="ck1", state="open")
            conn.commit()

        result = store.list_latest_by_cohort(project_id=34, mr_iid=299)
        states = sorted(s["state"] for s in result)
        assert states == ["dismissed", "open"]

    def test_all_open_dedup_to_latest(self, store: Store):
        """同 cohort 全部 open → 只留最新 (普通重复发布)."""
        with store._conn() as conn:
            _insert_suggestion(conn, id=1, cohort_key="ck1", state="open")
            _insert_suggestion(conn, id=2, cohort_key="ck1", state="open")
            _insert_suggestion(conn, id=3, cohort_key="ck1", state="open")
            conn.commit()

        result = store.list_latest_by_cohort(project_id=34, mr_iid=299)
        # 只留最新一条 (id DESC)
        assert len(result) == 1
        assert result[0]["id"] == 3

    def test_multiple_terminal_states_all_kept(self, store: Store):
        """MR263 旧逻辑保留: applied + dismissed + resolved 同 cohort 全保留."""
        with store._conn() as conn:
            _insert_suggestion(conn, id=1, cohort_key="ck1", state="applied")
            _insert_suggestion(conn, id=2, cohort_key="ck1", state="dismissed")
            _insert_suggestion(conn, id=3, cohort_key="ck1", state="resolved")
            conn.commit()

        result = store.list_latest_by_cohort(project_id=34, mr_iid=299)
        states = sorted(s["state"] for s in result)
        assert states == ["applied", "dismissed", "resolved"]

    def test_severity_upgrade_low_to_medium_keeps_latest(self, store: Store):
        """同 cohort severity 升级 (low→medium), 只留最新 (中等视为包含低)."""
        with store._conn() as conn:
            _insert_suggestion(conn, id=1, cohort_key="ck1", severity="low", state="open")
            _insert_suggestion(conn, id=2, cohort_key="ck1", severity="medium", state="open")
            conn.commit()

        result = store.list_latest_by_cohort(project_id=34, mr_iid=299)
        # row_number=1 + open (不 terminal) → 只留最新 1 条
        assert len(result) == 1
        assert result[0]["severity"] == "medium"

    def test_superseded_excluded(self, store: Store):
        """superseded 状态被 WHERE 排除."""
        with store._conn() as conn:
            _insert_suggestion(conn, id=1, cohort_key="ck1", state="superseded")
            _insert_suggestion(conn, id=2, cohort_key="ck1", state="open")
            conn.commit()

        result = store.list_latest_by_cohort(project_id=34, mr_iid=299)
        assert len(result) == 1
        assert result[0]["state"] == "open"

    def test_empty_cohort_key_fallback_to_note_id(self, store: Store):
        """cohort_key 为空时 fallback 到 note_id, 每条视为独立 cohort."""
        with store._conn() as conn:
            _insert_suggestion(conn, id=1, cohort_key="", note_id="n1", state="open")
            _insert_suggestion(conn, id=2, cohort_key="", note_id="n2", state="open")
            conn.commit()

        result = store.list_latest_by_cohort(project_id=34, mr_iid=299)
        # 不同 note_id = 不同 cohort, 都保留
        assert len(result) == 2

    def test_mr299_full_reproduction(self, store: Store):
        """完整复现 MR299 的 14 条 suggestion, 验证归并后 13 条且计数对得上 user 期望."""
        # 重现 MR299 数据: 11 个 cohort_key, 14 条记录
        # cohort L5:  975 medium applied + 984 medium open  → 2 保留 (修复前只 1)
        # cohort L6:  976 high resolved (单条)            → 1 保留
        # cohort L13: 985 medium open (单条)               → 1 保留
        # cohort L14: 982 low open    + 987 medium open    → 1 保留 (最新 medium, 升级)
        # cohort L17: 986 medium open (单条)               → 1 保留
        # cohort L18: 988 medium open (单条)               → 1 保留
        # cohort L58: 977 high dismissed (单条)            → 1 保留
        # cohort L63: 978 high resolved + 983 high open    → 2 保留 (修复前只 1)
        # cohort L148:979 high applied (单条)              → 1 保留
        # cohort L153:980 high dismissed (单条)            → 1 保留
        # cohort L158:981 high open (单条)                 → 1 保留
        # 期望总数 = 2 + 1 + 1 + 1 + 1 + 1 + 1 + 2 + 1 + 1 + 1 = 13
        with store._conn() as conn:
            _insert_suggestion(conn, id=975, cohort_key="0469", target_line=5,  severity="medium", state="applied")
            _insert_suggestion(conn, id=984, cohort_key="0469", target_line=5,  severity="medium", state="open")
            _insert_suggestion(conn, id=976, cohort_key="2803", target_line=6,  severity="high",   state="resolved")
            _insert_suggestion(conn, id=985, cohort_key="18ab", target_line=13, severity="medium", state="open")
            _insert_suggestion(conn, id=982, cohort_key="826d", target_line=14, severity="low",    state="open")
            _insert_suggestion(conn, id=987, cohort_key="826d", target_line=14, severity="medium", state="open")
            _insert_suggestion(conn, id=986, cohort_key="65dd", target_line=17, severity="medium", state="open")
            _insert_suggestion(conn, id=988, cohort_key="1814", target_line=18, severity="medium", state="open")
            _insert_suggestion(conn, id=977, cohort_key="7c24", target_line=58, severity="high",   state="dismissed")
            _insert_suggestion(conn, id=978, cohort_key="afec", target_line=63, severity="high",   state="resolved")
            _insert_suggestion(conn, id=983, cohort_key="afec", target_line=63, severity="high",   state="open")
            _insert_suggestion(conn, id=979, cohort_key="3335", target_line=148,severity="high",   state="applied")
            _insert_suggestion(conn, id=980, cohort_key="712b", target_line=153,severity="high",   state="dismissed")
            _insert_suggestion(conn, id=981, cohort_key="cdf4", target_line=158,severity="high",   state="open")
            conn.commit()

        result = store.list_latest_by_cohort(project_id=34, mr_iid=299)
        assert len(result) == 13, f"期望 13 条 (修复前 11, 修复后 13), 实际 {len(result)}"

        from collections import Counter
        c = Counter((s["severity"], s["state"]) for s in result)
        # 期望:
        #   high   applied=1 (979)
        #   high   dismissed=2 (977, 980)
        #   high   open=2 (981, 983)
        #   high   resolved=2 (976, 978)   ← 修复前 only 1
        #   medium applied=1 (975)         ← 修复前 0
        #   medium open=5 (984, 985, 987, 986, 988)
        #   low    (none, 升级到 medium)
        assert c[("high", "applied")] == 1
        assert c[("high", "dismissed")] == 2
        assert c[("high", "open")] == 2
        assert c[("high", "resolved")] == 2, (
            "MR299 关键回归: high resolved 应有 2 条 (976, 978)"
        )
        assert c[("medium", "applied")] == 1, (
            "MR299 关键回归: medium applied 应有 1 条 (975)"
        )
        assert c[("medium", "open")] == 5
        assert c[("low", "open")] == 0

        # user 期望: 总 applied=2, 总 resolved=2
        total_applied = sum(n for (sev, st), n in c.items() if st == "applied")
        total_resolved = sum(n for (sev, st), n in c.items() if st == "resolved")
        assert total_applied == 2, f"user 期望 applied=2, 实际 {total_applied}"
        assert total_resolved == 2, f"user 期望 resolved=2, 实际 {total_resolved}"
