"""Unit tests for periodic reconciler (reviewagent.reconciler.loop).

Background:
    Periodic reconciler 是 publish_overview pre-reconcile 的安全网, 处理
    "用户纯 click-only, 之后没任何后续事件" 这种孤儿状态. 每 60s 扫所有 bot
    跟踪的 open MR, 把 GitLab 端已 resolved 但 DB 还 open 的 suggestion 标 resolved,
    刷新对应 MR 的检视汇总 note.
"""
from __future__ import annotations

import os
import pathlib
import tempfile
from unittest.mock import MagicMock, patch

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


def _seed(s, *, project_id, mr_iid, note_id, line=10, severity="medium"):
    s.record_suggestion(
        project_id=project_id, mr_iid=mr_iid, note_id=note_id,
        file_path="a.py", target_line=line, target_line_end=line,
        existing_code="old\n", improved_code="new\n",
        header="h", label="l",
        fingerprint=f"fp-{note_id}", cohort_key=f"cohort-{note_id}",
        severity=severity, severity_source="rule", head_sha="abc",
    )


def _seed_open_mr(s, *, project_id=34, mr_iid):
    from reviewagent.telemetry.models import MRRecord
    s.upsert_mr(MRRecord(
        project_id=project_id, mr_iid=mr_iid,
        title=f"test mr {mr_iid}", author_username="tester",
        source_branch="feat", target_branch="main",
        state="opened",
    ))


# ---------- reconcile_single_mr ----------

def test_reconcile_single_mr_no_updates(tmp_telemetry):
    """没有 orphan (GitLab 也没 resolved) → updated=0, overview 不刷."""
    from reviewagent.reconciler.loop import reconcile_single_mr

    s = get_store_fixture(tmp_telemetry)
    _seed_open_mr(s, mr_iid=263)
    _seed(s, project_id=34, mr_iid=263, note_id="normal-1")

    gl = MagicMock()
    gl.is_discussion_resolved.return_value = False  # GitLab 也没 resolved

    with patch("reviewagent.commands.suggestion_actions.GitLabClient", return_value=gl), \
         patch("reviewagent.commands.suggestion_actions.get_store", return_value=s):
        result = reconcile_single_mr(project_id=34, mr_iid=263)

    assert result["scanned"] == 1
    assert result["updated"] == 0
    assert result["overview_refreshed"] is False


def test_reconcile_single_mr_catches_orphan(tmp_telemetry):
    """orphan (GitLab resolved, DB open) → updated + overview 刷新."""
    from reviewagent.reconciler.loop import reconcile_single_mr

    s = get_store_fixture(tmp_telemetry)
    _seed_open_mr(s, mr_iid=263)
    _seed(s, project_id=34, mr_iid=263, note_id="orphan-1")
    _seed(s, project_id=34, mr_iid=263, note_id="orphan-2")

    gl = MagicMock()
    def is_resolved(project_id, mr_iid, note_id):
        return note_id == "orphan-1"
    gl.is_discussion_resolved.side_effect = is_resolved
    gl.list_mr_notes.return_value = []
    gl.post_mr_comment.return_value = 99999

    with patch("reviewagent.commands.suggestion_actions.GitLabClient", return_value=gl), \
         patch("reviewagent.commands._common.GitLabClient", return_value=gl), \
         patch("reviewagent.commands.suggestion_actions.get_store", return_value=s):
        result = reconcile_single_mr(project_id=34, mr_iid=263)

    assert result["scanned"] == 2
    assert result["updated"] == 1
    assert result["note_ids"] == ["orphan-1"]
    assert result["overview_refreshed"] is True
    # DB 已同步
    sug1 = s.get_suggestion_by_note_id("orphan-1")
    sug2 = s.get_suggestion_by_note_id("orphan-2")
    assert sug1["state"] == "resolved"
    assert sug1["resolution_source"] == "periodic_reconcile"
    assert sug2["state"] == "open"


def test_reconcile_single_mr_uses_periodic_actor(tmp_telemetry):
    """reconcile_single_mr 默认 actor 是 periodic_reconciler (可审计)."""
    from reviewagent.reconciler.loop import reconcile_single_mr

    s = get_store_fixture(tmp_telemetry)
    _seed_open_mr(s, mr_iid=263)
    _seed(s, project_id=34, mr_iid=263, note_id="actor-test")

    gl = MagicMock()
    gl.is_discussion_resolved.return_value = True
    gl.list_mr_notes.return_value = []
    gl.post_mr_comment.return_value = 99999

    with patch("reviewagent.commands.suggestion_actions.GitLabClient", return_value=gl), \
         patch("reviewagent.commands._common.GitLabClient", return_value=gl), \
         patch("reviewagent.commands.suggestion_actions.get_store", return_value=s):
        reconcile_single_mr(project_id=34, mr_iid=263)

    actions = [a for a in s.list_suggestion_actions(project_id=34, mr_iid=263, limit=100)
               if a.get("suggestion_note_id") == "actor-test"]
    assert len(actions) >= 1
    assert any(a.get("actor_username") == "periodic_reconciler" for a in actions)


# ---------- reconcile_open_mrs ----------

def test_reconcile_open_mrs_only_scans_open_state(tmp_telemetry):
    """list_mrs(state='opened') 只返回 state='opened' 的 MR. closed/merged 跳过."""
    from reviewagent.reconciler.loop import reconcile_open_mrs

    s = get_store_fixture(tmp_telemetry)
    # 3 个 MR, 1 个 open, 2 个 closed
    from reviewagent.telemetry.models import MRRecord
    _seed_open_mr(s, mr_iid=100)
    s.upsert_mr(MRRecord(project_id=34, mr_iid=200, title="closed mr",
                author_username="t", source_branch="a", target_branch="main", state="closed"))
    s.upsert_mr(MRRecord(project_id=34, mr_iid=300, title="merged mr",
                author_username="t", source_branch="a", target_branch="main", state="merged"))

    gl = MagicMock()
    gl.is_discussion_resolved.return_value = False

    with patch("reviewagent.commands.suggestion_actions.GitLabClient", return_value=gl), \
         patch("reviewagent.commands.suggestion_actions.get_store", return_value=s):
        result = reconcile_open_mrs(project_id=34)

    assert result["total_mrs"] == 1  # 只 mr 100


def test_reconcile_open_mrs_handles_per_mr_failures(tmp_telemetry):
    """某个 MR 扫描抛异常不影响其他 MR (故障隔离)."""
    from reviewagent.reconciler.loop import reconcile_open_mrs

    s = get_store_fixture(tmp_telemetry)
    _seed_open_mr(s, mr_iid=100)
    _seed_open_mr(s, mr_iid=200)
    _seed(s, project_id=34, mr_iid=200, note_id="m200-note")

    gl = MagicMock()
    # mr 200 走通, 其它全 fail (实际不会发生, 但模拟失败路径)
    def selective_resolve(project_id, mr_iid, note_id):
        if mr_iid == 200:
            return True
        raise RuntimeError("network glitch")
    gl.is_discussion_resolved.side_effect = selective_resolve
    gl.list_mr_notes.return_value = []
    gl.post_mr_comment.return_value = 99999

    with patch("reviewagent.commands.suggestion_actions.GitLabClient", return_value=gl), \
         patch("reviewagent.commands._common.GitLabClient", return_value=gl), \
         patch("reviewagent.commands.suggestion_actions.get_store", return_value=s):
        result = reconcile_open_mrs(project_id=34)

    # mr 100 失败 (被 catch 跳过), mr 200 成功 → mrs_updated 只有 mr 200
    assert result["total_updated"] == 1
    assert len(result["mrs_updated"]) == 1
    assert result["mrs_updated"][0]["mr_iid"] == 200


def test_reconcile_open_mrs_no_orphans(tmp_telemetry):
    """没有任何 orphan 时, total_updated=0, duration 仍记录."""
    from reviewagent.reconciler.loop import reconcile_open_mrs

    s = get_store_fixture(tmp_telemetry)
    _seed_open_mr(s, mr_iid=100)
    _seed(s, project_id=34, mr_iid=100, note_id="clean-1")

    gl = MagicMock()
    gl.is_discussion_resolved.return_value = False

    with patch("reviewagent.commands.suggestion_actions.GitLabClient", return_value=gl), \
         patch("reviewagent.commands.suggestion_actions.get_store", return_value=s):
        result = reconcile_open_mrs(project_id=34)

    assert result["total_mrs"] == 1
    assert result["total_updated"] == 0
    assert result["mrs_updated"] == []
    assert "duration_s" in result


def test_reconcile_open_mrs_idempotent(tmp_telemetry):
    """连跑两次, 第二次 updated=0 (DB 已 sync, 不会被重复标 resolved)."""
    from reviewagent.reconciler.loop import reconcile_open_mrs

    s = get_store_fixture(tmp_telemetry)
    _seed_open_mr(s, mr_iid=100)
    _seed(s, project_id=34, mr_iid=100, note_id="once-1")

    gl = MagicMock()
    gl.is_discussion_resolved.return_value = True
    gl.list_mr_notes.return_value = []
    gl.post_mr_comment.return_value = 99999

    with patch("reviewagent.commands.suggestion_actions.GitLabClient", return_value=gl), \
         patch("reviewagent.commands._common.GitLabClient", return_value=gl), \
         patch("reviewagent.commands.suggestion_actions.get_store", return_value=s):
        result1 = reconcile_open_mrs(project_id=34)
        result2 = reconcile_open_mrs(project_id=34)

    assert result1["total_updated"] == 1
    # 第二次: 已经被标 resolved 了, 不再出现在 list_open_suggestions
    assert result2["total_updated"] == 0


def get_store_fixture(path):
    """Return get_store() result (reset between tests)."""
    from reviewagent.telemetry.store import get_store
    return get_store()


# Override the _fixture helper to avoid name conflict with pytest fixture
