"""Regression tests for sync_resolved_from_gitlab (the webhook-triggered path).

Background:
    之前 sync_resolved_from_gitlab 独立实现了扫描+update+publish_overview 全部逻辑.
    现在已抽到 _scan_and_mark_resolved_silent, sync_resolved_from_gitlab 复用之.
    这些测试验证抽出后行为不变.
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


def _seed(s, *, note_id, line=10, severity="medium"):
    s.record_suggestion(
        project_id=34, mr_iid=263, note_id=note_id,
        file_path="a.py", target_line=line, target_line_end=line,
        existing_code="old\n", improved_code="new\n",
        header="h", label="l",
        fingerprint=f"fp-{note_id}", cohort_key=f"cohort-{note_id}",
        severity=severity, severity_source="rule", head_sha="abc",
    )


def test_sync_resolved_uses_silent_helper(tmp_telemetry):
    """sync_resolved_from_gitlab 调用 _scan_and_mark_resolved_silent 拿到结果."""
    from reviewagent.commands.suggestion_actions import sync_resolved_from_gitlab
    from reviewagent.telemetry.store import get_store

    s = get_store()
    _seed(s, note_id="sync-1")
    _seed(s, note_id="sync-2")

    gl = MagicMock()
    gl.is_discussion_resolved.return_value = True
    gl.list_mr_commits.return_value = []
    gl.list_mr_notes.return_value = []  # publish_overview 走 post 路径

    def fake_post(project_id, mr_iid, body):
        return 99999
    gl.post_mr_comment.side_effect = fake_post

    with patch("reviewagent.commands.suggestion_actions.GitLabClient", return_value=gl), \
         patch("reviewagent.commands.suggestion_actions.get_store", return_value=s), \
         patch("reviewagent.commands._common.GitLabClient", return_value=gl):
        result = sync_resolved_from_gitlab(
            project_id=34, mr_iid=263,
            actor_username="webhook-handler",
        )

    assert result["scanned"] == 2
    assert result["updated"] == 2
    assert sorted(result["note_ids"]) == ["sync-1", "sync-2"]
    # DB 状态已更新
    for note_id in ("sync-1", "sync-2"):
        sug = s.get_suggestion_by_note_id(note_id)
        assert sug["state"] == "resolved"
        assert sug["resolution_source"] == "gitlab_resolve"  # 来自 webhook 触发


def test_sync_resolved_uses_correct_adoption_source(tmp_telemetry):
    """sync_resolved_from_gitlab 写入的 adoption_source 必须是 gitlab_resolve,
    区别于 publish_overview_pre_reconcile 用的 publish_overview_reconcile.
    """
    from reviewagent.commands.suggestion_actions import sync_resolved_from_gitlab
    from reviewagent.telemetry.store import get_store

    s = get_store()
    _seed(s, note_id="src-test")

    gl = MagicMock()
    gl.is_discussion_resolved.return_value = True
    gl.list_mr_commits.return_value = []
    gl.list_mr_notes.return_value = []
    gl.post_mr_comment.return_value = 99999

    with patch("reviewagent.commands.suggestion_actions.GitLabClient", return_value=gl), \
         patch("reviewagent.commands.suggestion_actions.get_store", return_value=s), \
         patch("reviewagent.commands._common.GitLabClient", return_value=gl):
        sync_resolved_from_gitlab(
            project_id=34, mr_iid=263,
            actor_username="webhook-handler",
        )

    sug = s.get_suggestion_by_note_id("src-test")
    assert sug["resolution_source"] == "gitlab_resolve"


def test_sync_resolved_no_overview_call_when_no_updates(tmp_telemetry):
    """没有 actual 更新时, sync_resolved_from_gitlab 不调用 publish_overview."""
    from reviewagent.commands.suggestion_actions import sync_resolved_from_gitlab
    from reviewagent.telemetry.store import get_store

    s = get_store()
    _seed(s, note_id="no-update")

    gl = MagicMock()
    gl.is_discussion_resolved.return_value = False  # 都没 resolved

    with patch("reviewagent.commands.suggestion_actions.GitLabClient", return_value=gl), \
         patch("reviewagent.commands.suggestion_actions.get_store", return_value=s), \
         patch("reviewagent.commands.suggestion_actions.publish_overview") as mock_pub:
        result = sync_resolved_from_gitlab(
            project_id=34, mr_iid=263,
            actor_username="webhook-handler",
        )

    assert result["updated"] == 0
    mock_pub.assert_not_called()
