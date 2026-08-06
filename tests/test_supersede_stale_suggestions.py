"""A3: head_sha 变化时, state=open 且 head_sha != current 的 suggestions 应被标 superseded.

Why: UI Apply suggestion / 新 push 都改 head_sha, 老 suggestions 的行号 / 上下文
     可能已失效, 留着 state=open 会:
       - 前端 V{N} 列表里看到一堆"仍 open"但实际已 outdated
       - dedup_at_line 把它们当"已发过"而漏掉新 bug
       - /adopt 时跟新建议互相干扰
"""
from __future__ import annotations

import os
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
        property(lambda self: __import__("pathlib").Path(path)),
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


def _seed_suggestion(s, *, note_id: str, head_sha: str):
    """种子一条 suggestion (state=open)."""
    s.record_suggestion(
        project_id=34, mr_iid=211, note_id=note_id,
        file_path="a.py", target_line=3, target_line_end=5,
        existing_code="def foo():\n    pass\n    return 1\n",
        improved_code="def foo():\n    pass\n    return 2\n",
        header="fix", label="L",
        fingerprint=note_id, cohort_key="c",
        severity_source="rule",
        head_sha=head_sha,
    )


def test_supersede_marks_only_stale_open(tmp_telemetry):
    """head_sha 变化的 open suggestions 全部 superseded; 同 SHA / 已 applied / 已 dismissed 不动."""
    from reviewagent.telemetry.store import get_store
    s = get_store()

    # 三个 open: 2 个 stale (head_sha=old) + 1 个 current (head_sha=new)
    _seed_suggestion(s, note_id="stale-1", head_sha="old-sha")
    _seed_suggestion(s, note_id="stale-2", head_sha="old-sha")
    _seed_suggestion(s, note_id="current", head_sha="new-sha")

    # 1 个 applied (即使 head_sha=old 也不动 — applied 的语义是用户已采纳)
    _seed_suggestion(s, note_id="applied-old", head_sha="old-sha")
    s.update_suggestion_state("applied-old", "applied")

    # 1 个 dismissed (同理)
    _seed_suggestion(s, note_id="dismissed-old", head_sha="old-sha")
    s.update_suggestion_state("dismissed-old", "dismissed", actor_username="tester")

    note_ids = s.supersede_stale_open_suggestions(
        project_id=34, mr_iid=211, current_head_sha="new-sha"
    )
    # 只有 stale-1 / stale-2 命中
    assert sorted(note_ids) == ["stale-1", "stale-2"], note_ids

    # 验证 DB state
    rows = s.list_suggestions(project_id=34, mr_iid=211)
    states_by_note = {r["note_id"]: r["state"] for r in rows}
    assert states_by_note["stale-1"] == "superseded"
    assert states_by_note["stale-2"] == "superseded"
    assert states_by_note["current"] == "open"
    assert states_by_note["applied-old"] == "applied"
    assert states_by_note["dismissed-old"] == "dismissed"


def test_supersede_empty_head_sha_is_noop(tmp_telemetry):
    """空 head_sha 不操作 (前置校验失败)."""
    from reviewagent.telemetry.store import get_store
    s = get_store()

    _seed_suggestion(s, note_id="x", head_sha="any")
    res = s.supersede_stale_open_suggestions(
        project_id=34, mr_iid=211, current_head_sha=""
    )
    assert res == []
    # x 仍 open
    rows = s.list_suggestions(project_id=34, mr_iid=211)
    assert rows[0]["state"] == "open"


def test_supersede_other_mr_not_affected(tmp_telemetry):
    """只 supersede 指定 (project_id, mr_iid)."""
    from reviewagent.telemetry.store import get_store
    s = get_store()

    # MR 211 的 stale suggestion
    _seed_suggestion(s, note_id="mr211-stale", head_sha="old")
    # MR 212 的 stale suggestion (不同 MR)
    s.record_suggestion(
        project_id=34, mr_iid=212, note_id="mr212-stale",
        file_path="b.py", target_line=3, target_line_end=5,
        existing_code="x", improved_code="y",
        header="h", label="l",
        fingerprint="fp-mr212", cohort_key="c",
        severity_source="rule", head_sha="old",
    )

    note_ids = s.supersede_stale_open_suggestions(
        project_id=34, mr_iid=211, current_head_sha="new"
    )
    assert note_ids == ["mr211-stale"]

    # MR 212 的仍是 open
    rows = s.list_suggestions(project_id=34, mr_iid=212)
    assert rows[0]["state"] == "open"


def test_supersede_returns_empty_when_no_stale(tmp_telemetry):
    """所有 open suggestions 的 head_sha 都匹配 current → 无操作."""
    from reviewagent.telemetry.store import get_store
    s = get_store()

    _seed_suggestion(s, note_id="a", head_sha="same-sha")
    _seed_suggestion(s, note_id="b", head_sha="same-sha")

    res = s.supersede_stale_open_suggestions(
        project_id=34, mr_iid=211, current_head_sha="same-sha"
    )
    assert res == []

    rows = s.list_suggestions(project_id=34, mr_iid=211)
    for r in rows:
        assert r["state"] == "open"


def test_supersede_only_open_not_applied(tmp_telemetry):
    """applied / dismissed 的不应被改成 superseded (它们的语义是终态)."""
    from reviewagent.telemetry.store import get_store
    s = get_store()

    _seed_suggestion(s, note_id="applied-old", head_sha="old")
    s.update_suggestion_state("applied-old", "applied")
    _seed_suggestion(s, note_id="dismissed-old", head_sha="old")
    s.update_suggestion_state("dismissed-old", "dismissed", actor_username="u")

    res = s.supersede_stale_open_suggestions(
        project_id=34, mr_iid=211, current_head_sha="new"
    )
    assert res == [], f"非 open 不应被 superseded: {res}"

    rows = s.list_suggestions(project_id=34, mr_iid=211)
    states = {r["note_id"]: r["state"] for r in rows}
    assert states["applied-old"] == "applied"
    assert states["dismissed-old"] == "dismissed"


@pytest.mark.asyncio
async def test_update_does_not_supersede_unapplied_open_suggestions(monkeypatch):
    """A new head must not hide still-unresolved suggestions from pending counts."""
    from unittest.mock import MagicMock
    import reviewagent.webhook.router as router

    payload = {
        "object_kind": "merge_request",
        "event_type": "merge_request",
        "project": {"id": 34},
        "user": {"username": "root"},
        "object_attributes": {
            "iid": 234,
            "action": "update",
            "state": "opened",
            "source_branch": "fixture",
            "target_branch": "main",
            "last_commit": {"id": "new-head"},
            "head_sha": "new-head",
        },
    }
    fake_locks = MagicMock()
    fake_locks.is_bot.return_value = False
    fake_locks.check_diff_head_changed.return_value = True
    fake_locks.should_skip_max_review_calls.return_value = (False, 0)
    fake_locks.should_skip_cooldown.return_value = True
    monkeypatch.setattr(router, "locks", fake_locks)

    store = MagicMock()
    monkeypatch.setattr("reviewagent.telemetry.store.get_store", lambda: store)
    monkeypatch.setattr(
        "reviewagent.commands.suggestion_actions.auto_detect_applied",
        lambda **kwargs: {"scanned": 2, "applied": 1, "unchanged": 1, "errors": 0},
    )

    result = await router._handle_code_change(payload, "merge_request", MagicMock())

    assert result["status"] == "skipped"
    store.supersede_stale_open_suggestions.assert_not_called()
