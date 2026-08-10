"""Unit tests for Store dedup logic (suggestion_exists_by_fingerprint, suggestion_exists_at_line).

Background:
    dedup 是 review bot 防刷关键 — 同一问题多次检视 / 多次发布都让用户烦.
    测试覆盖:
    - suggestion_exists_by_fingerprint: 跨次 fingerprint dedup
    - suggestion_exists_at_line: file:line dedup (含已处理永远命中 + open+head_sha 等)
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


def _seed(s, *, note_id, fp, line=10, file_path="a.py", state="open",
          head_sha="abc", rule_keys="R1", cohort_key=None):
    s.record_suggestion(
        project_id=34, mr_iid=263, note_id=note_id,
        file_path=file_path, target_line=line, target_line_end=line,
        existing_code="old\n", improved_code="new\n",
        header="h", label="l",
        fingerprint=fp, rule_keys=rule_keys.split(","),
        cohort_key=cohort_key or f"cohort-{note_id}",
        severity="medium", severity_source="rule", head_sha=head_sha,
    )
    if state != "open":
        s.update_suggestion_state(note_id, state, actor_username="tester")


# ---------- suggestion_exists_by_fingerprint ----------

def test_fingerprint_dedup_hits_applied(tmp_telemetry):
    """state=applied → 永远命中 (不管 head_sha 变没变)."""
    from reviewagent.telemetry.store import get_store
    s = get_store()
    _seed(s, note_id="fp-1", fp="FP-A", state="applied", head_sha="old_sha")

    assert s.suggestion_exists_by_fingerprint(34, 263, "FP-A", head_sha="new_sha") is True


def test_fingerprint_dedup_hits_dismissed(tmp_telemetry):
    """state=dismissed → 永远命中."""
    from reviewagent.telemetry.store import get_store
    s = get_store()
    _seed(s, note_id="fp-2", fp="FP-B", state="dismissed")

    assert s.suggestion_exists_by_fingerprint(34, 263, "FP-B") is True


def test_fingerprint_dedup_open_requires_same_head_sha(tmp_telemetry):
    """state=open 必须 head_sha 匹配 (force-push 后残留 → 放行重新识别)."""
    from reviewagent.telemetry.store import get_store
    s = get_store()
    _seed(s, note_id="fp-3", fp="FP-C", state="open", head_sha="sha1")

    assert s.suggestion_exists_by_fingerprint(34, 263, "FP-C", head_sha="sha1") is True
    # head_sha 变了 → 不命中 (force-push 后残留)
    assert s.suggestion_exists_by_fingerprint(34, 263, "FP-C", head_sha="sha2") is False


def test_fingerprint_dedup_unknown_fingerprint_returns_false(tmp_telemetry):
    """不存在的 fingerprint → False."""
    from reviewagent.telemetry.store import get_store
    s = get_store()

    assert s.suggestion_exists_by_fingerprint(34, 263, "DOES-NOT-EXIST") is False


def test_fingerprint_dedup_empty_fingerprint_returns_false(tmp_telemetry):
    """空 fingerprint → False (避免误判)."""
    from reviewagent.telemetry.store import get_store
    s = get_store()

    assert s.suggestion_exists_by_fingerprint(34, 263, "", head_sha="abc") is False


# ---------- suggestion_exists_at_line ----------

def test_line_dedup_hits_applied_forever(tmp_telemetry):
    """state=applied → 永远命中 (不管 head_sha / rule_keys)."""
    from reviewagent.telemetry.store import get_store
    s = get_store()
    _seed(s, note_id="ln-1", fp="FP1", line=10, state="applied", head_sha="sha_a", rule_keys="R1")

    assert s.suggestion_exists_at_line(
        34, 263, "a.py", 10,
        head_sha="sha_b",  # 不同 head_sha
        rule_keys="R9",  # 不同 rule_keys
    ) is True


def test_line_dedup_open_with_matching_head_sha_hits(tmp_telemetry):
    """state=open + 同 head_sha → 命中."""
    from reviewagent.telemetry.store import get_store
    s = get_store()
    _seed(s, note_id="ln-2", fp="FP2", line=10, state="open", head_sha="sha1", rule_keys="R1")

    assert s.suggestion_exists_at_line(34, 263, "a.py", 10, head_sha="sha1", rule_keys="R1") is True


def test_line_dedup_open_with_different_head_sha_misses(tmp_telemetry):
    """state=open + 不同 head_sha + rule_keys 不重叠 → 不命中 (force-push 残留放行)."""
    from reviewagent.telemetry.store import get_store
    s = get_store()
    _seed(s, note_id="ln-3", fp="FP3", line=10, state="open", head_sha="sha_old", rule_keys="R1")

    # 不同 head_sha + 不同 rule_keys → 不命中 (force-push 真残留, 放行重新识别)
    assert s.suggestion_exists_at_line(34, 263, "a.py", 10, head_sha="sha_new", rule_keys="R9") is False


def test_line_dedup_different_file_misses(tmp_telemetry):
    """不同 file_path → 不命中."""
    from reviewagent.telemetry.store import get_store
    s = get_store()
    _seed(s, note_id="ln-4", fp="FP4", line=10, file_path="a.py", state="applied")

    assert s.suggestion_exists_at_line(34, 263, "b.py", 10) is False


def test_line_dedup_with_line_tolerance(tmp_telemetry):
    """line_tolerance=2 允许 ±2 行范围命中."""
    from reviewagent.telemetry.store import get_store
    s = get_store()
    _seed(s, note_id="ln-5", fp="FP5", line=10, state="applied")

    assert s.suggestion_exists_at_line(34, 263, "a.py", 12, line_tolerance=2) is True
    assert s.suggestion_exists_at_line(34, 263, "a.py", 13, line_tolerance=2) is False


def test_line_dedup_rule_keys_open_with_same_rule_hits(tmp_telemetry):
    """open + 同 head_sha + rule_keys 重叠 → 命中."""
    from reviewagent.telemetry.store import get_store
    s = get_store()
    _seed(s, note_id="ln-6", fp="FP6", line=10, state="open", head_sha="sha1", rule_keys="R1,R2")

    assert s.suggestion_exists_at_line(34, 263, "a.py", 10, head_sha="sha1", rule_keys="R2,R3") is True


def test_line_dedup_rule_keys_open_no_overlap_misses(tmp_telemetry):
    """open + 同 head_sha + rule_keys 完全不重叠 → 不命中."""
    from reviewagent.telemetry.store import get_store
    s = get_store()
    _seed(s, note_id="ln-7", fp="FP7", line=10, state="open", head_sha="sha1", rule_keys="R1,R2")

    assert s.suggestion_exists_at_line(34, 263, "a.py", 10, head_sha="sha1", rule_keys="R8,R9") is False


# ---------- record_suggestion side effects ----------

def test_record_suggestion_touches_last_activity(tmp_telemetry):
    """record_suggestion 同步更新 mr_activity.last_activity_at."""
    from reviewagent.telemetry.store import get_store
    from reviewagent.telemetry.models import MRRecord
    s = get_store()
    s.upsert_mr(MRRecord(
        project_id=34, mr_iid=263,
        title="t", author_username="u",
        source_branch="f", target_branch="main", state="opened",
    ))

    s.record_suggestion(
        project_id=34, mr_iid=263, note_id="act-1",
        file_path="a.py", target_line=10, head_sha="abc",
    )

    mr = s.get_mr(34, 263)
    assert mr is not None
    assert mr["last_activity_at"] is not None


def test_record_suggestion_assigns_open_state(tmp_telemetry):
    """新 record 的 suggestion 默认为 state=open."""
    from reviewagent.telemetry.store import get_store
    s = get_store()
    s.record_suggestion(
        project_id=34, mr_iid=263, note_id="new-1",
        file_path="a.py", target_line=10, head_sha="abc",
    )
    sug = s.get_suggestion_by_note_id("new-1")
    assert sug["state"] == "open"


# ---------- update_suggestion_state ----------

def test_update_state_to_applied_records_audit(tmp_telemetry):
    """update_suggestion_state → applied 必须落库."""
    from reviewagent.telemetry.store import get_store
    s = get_store()
    s.record_suggestion(
        project_id=34, mr_iid=263, note_id="up-1",
        file_path="a.py", target_line=10, head_sha="abc",
    )

    s.update_suggestion_state(
        "up-1", "applied",
        actor_username="tester",
        adoption_source="ui_apply",
        adoption_evidence="exact_match",
    )

    sug = s.get_suggestion_by_note_id("up-1")
    assert sug["state"] == "applied"
    assert sug["adoption_source"] == "ui_apply"
    assert sug["adoption_evidence"] == "exact_match"


def test_update_state_to_dismissed_stores_reason(tmp_telemetry):
    """update_suggestion_state → dismissed 必须存 reason + actor."""
    from reviewagent.telemetry.store import get_store
    s = get_store()
    s.record_suggestion(
        project_id=34, mr_iid=263, note_id="up-2",
        file_path="a.py", target_line=10, head_sha="abc",
    )

    s.update_suggestion_state(
        "up-2", "dismissed",
        actor_username="root",
        dismissed_reason="误报",
    )

    sug = s.get_suggestion_by_note_id("up-2")
    assert sug["state"] == "dismissed"
    assert sug["dismissed_by"] == "root"
    assert sug["dismissed_reason"] == "误报"
