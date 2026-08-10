"""Unit tests for build_overview_body (检视汇总 markdown 生成).

Background:
    build_overview_body 是 publish_overview 的核心 — 把 DB 状态聚合生成固定表格.
    这是用户在 GitLab MR 顶部看到的检视汇总 note 的内容来源.
"""
from __future__ import annotations

import os
import pathlib
import tempfile
from unittest.mock import patch

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


def test_empty_mr_produces_zero_summary(tmp_telemetry):
    """没任何 suggestion → 总计=0, 采纳率=0.0%."""
    from reviewagent.commands._common import build_overview_body

    body = build_overview_body(project_id=34, mr_iid=263, inline_posted_count=0)
    assert "总建议数 0" in body
    assert "采纳率 0.0%" in body
    assert "HIGH" in body
    assert "MEDIUM" in body
    assert "LOW" in body
    # 总计行
    assert "**总计**" in body


def test_counts_by_severity_and_state(tmp_telemetry):
    """HIGH/MEDIUM/LOW × {open/applied/dismissed/resolved} 都被正确计数."""
    from reviewagent.commands._common import build_overview_body

    s = _get_store()
    # HIGH: 2 open, 1 applied
    _seed(s, note_id="h1", severity="high", state="open", cohort_key="ck-h1")
    _seed(s, note_id="h2", severity="high", state="open", cohort_key="ck-h2")
    _seed(s, note_id="h3", severity="high", state="applied", cohort_key="ck-h3")
    # MEDIUM: 1 open, 1 dismissed, 1 resolved
    _seed(s, note_id="m1", severity="medium", state="open", cohort_key="ck-m1")
    _seed(s, note_id="m2", severity="medium", state="dismissed", cohort_key="ck-m2")
    _seed(s, note_id="m3", severity="medium", state="resolved", cohort_key="ck-m3")
    # LOW: 1 open
    _seed(s, note_id="l1", severity="low", state="open", cohort_key="ck-l1")

    body = build_overview_body(project_id=34, mr_iid=263, inline_posted_count=0)
    # 总计 7
    assert "总建议数 7" in body
    # 采纳率 = 1/7 = 14.3%
    assert "采纳率 14.3%" in body
    # HIGH 行: 2/1/0/0 = 3
    assert "🔴 HIGH" in body
    # 表格行严格匹配: open | applied | dismissed | resolved | sum
    import re
    high_line = next(l for l in body.splitlines() if "🔴 HIGH" in l)
    assert re.search(r"\|\s*🔴 HIGH\s*\|\s*2\s*\|\s*1\s*\|\s*0\s*\|\s*0\s*\|\s*3\s*\|", high_line), high_line
    medium_line = next(l for l in body.splitlines() if "🟡 MEDIUM" in l)
    assert re.search(r"\|\s*🟡 MEDIUM\s*\|\s*1\s*\|\s*0\s*\|\s*1\s*\|\s*1\s*\|\s*3\s*\|", medium_line)
    low_line = next(l for l in body.splitlines() if "🟢 LOW" in l)
    assert re.search(r"\|\s*🟢 LOW\s*\|\s*1\s*\|\s*0\s*\|\s*0\s*\|\s*0\s*\|\s*1\s*\|", low_line)
    # 总计行: 4/1/1/1 = 7
    total_line = next(l for l in body.splitlines() if "总计" in l)
    assert re.search(r"\*\*总计\*\*\s*\|\s*\*\*4\*\*\s*\|\s*\*\*1\*\*\s*\|\s*\*\*1\*\*\s*\|\s*\*\*1\*\*\s*\|\s*\*\*7\*\*", total_line)


def test_cohort_dedup_in_overview(tmp_telemetry):
    """同 cohort 多条 suggestion 只算 1 (避免重复计算同一问题)."""
    from reviewagent.commands._common import build_overview_body

    s = _get_store()
    # 同 cohort (ck-same) 两条 suggestion, 不同 state
    _seed(s, note_id="cd-1", severity="high", state="open", cohort_key="ck-same")
    _seed(s, note_id="cd-2", severity="high", state="applied", cohort_key="ck-same")
    # 不同 cohort 各 1
    _seed(s, note_id="cd-3", severity="medium", state="open", cohort_key="ck-other")

    body = build_overview_body(project_id=34, mr_iid=263, inline_posted_count=0)
    # list_latest_by_cohort 应该只返回每个 cohort 的最新 1 条
    # 实际: ck-same → 1 条 (latest by created_at), ck-other → 1 条 = 2 total
    # 简化断言: 总数 <= 3 (3 条都被录入), 应 = 2 (cohort 去重)
    import re
    total_match = re.search(r"总建议数 (\d+)", body)
    assert total_match, f"no total in body: {body}"
    total = int(total_match.group(1))
    # ck-same 的两条会被 list_latest_by_cohort 折成 1 条 (取最新 = cd-2 applied)
    # 所以 HIGH: 1 applied, MEDIUM: 1 open, 总计 = 2
    assert total == 2, f"expected 2 (cohort dedup), got {total}; body: {body}"


def test_adoption_rate_zero_division_safe(tmp_telemetry):
    """没有任何 suggestion 时, 采纳率 = 0.0% (避免除零)."""
    from reviewagent.commands._common import build_overview_body

    body = build_overview_body(project_id=34, mr_iid=999)  # 没 seed
    assert "采纳率 0.0%" in body


def test_inline_posted_count_appears_in_footer(tmp_telemetry):
    """inline_posted_count 出现在底部"最后新增 N 条" 行."""
    from reviewagent.commands._common import build_overview_body

    body = build_overview_body(project_id=34, mr_iid=263, inline_posted_count=5)
    assert "最后新增 5 条" in body


def test_head_sha_appears_when_provided(tmp_telemetry):
    """head_sha 非空时, 底部显示 HEAD 短码 (前 7 字符)."""
    from reviewagent.commands._common import build_overview_body

    body = build_overview_body(
        project_id=34, mr_iid=263, inline_posted_count=0,
        head_sha="abcdef1234567",
    )
    assert "HEAD abcdef1" in body


def test_head_sha_absent_when_empty(tmp_telemetry):
    """head_sha 空字符串时, 不显示 HEAD 行."""
    from reviewagent.commands._common import build_overview_body

    body = build_overview_body(
        project_id=34, mr_iid=263, inline_posted_count=0,
        head_sha="",
    )
    assert "HEAD" not in body


def test_legend_explanations_present(tmp_telemetry):
    """底部 3 段状态说明 (✅/❌/🔒) 必须出现."""
    from reviewagent.commands._common import build_overview_body

    body = build_overview_body(project_id=34, mr_iid=263, inline_posted_count=0)
    assert "✅" in body and "已采纳" in body
    assert "❌" in body and "已忽略" in body
    assert "🔒" in body and "已关闭" in body


def test_cst_timestamp_appears(tmp_telemetry):
    """时间戳必须用 Asia/Shanghai (CST) 时区."""
    from reviewagent.commands._common import build_overview_body

    body = build_overview_body(project_id=34, mr_iid=263, inline_posted_count=0)
    # 形如 "⏱ 2026-08-11 09:00:00 CST"
    import re
    assert re.search(r"⏱ \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} CST", body), body


def test_superseded_count_appears(tmp_telemetry):
    """superseded_n > 0 时显示 ♻️ 同问题被多轮重复发布行."""
    from reviewagent.commands._common import build_overview_body

    # mark some as superseded
    s = _get_store()
    _seed(s, note_id="sup-1", cohort_key="ck-sup1")
    _seed(s, note_id="sup-2", cohort_key="ck-sup2")
    # mark one as superseded
    import sqlite3
    conn = sqlite3.connect(str(pathlib.Path(tmp_telemetry).resolve()))
    conn.execute("UPDATE suggestions SET state='superseded' WHERE note_id='sup-1'")
    conn.commit()
    conn.close()

    body = build_overview_body(project_id=34, mr_iid=263, inline_posted_count=0)
    # superseded 不计入 active, 但底部应该有 ♻️ 行
    assert "♻️" in body or "同问题被多轮" in body


def _get_store():
    from reviewagent.telemetry.store import get_store
    return get_store()
