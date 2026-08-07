"""Regression test: auto_detect_applied 应在每次迭代重检 state, 避免覆盖 dismiss.

Race 场景:
1. push webhook 触发 auto_detect_applied
2. auto_detect 拿到 open suggestions 列表 (此时 #X 还是 open)
3. 用户在另一个线程 / 队列处理 dismiss, 把 #X 标 dismissed
4. auto_detect 处理到 #X 时, 不应再覆盖为 applied

未修复版本会 silently 把 dismissed 改成 applied, 数据采集失真.
"""
from __future__ import annotations

import os
import tempfile

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
    except OSError:
        pass


def test_auto_detect_skips_suggestion_dismissed_mid_scan(tmp_telemetry):
    """list_open_suggestions 在循环开始时 fetch, 用户在循环中 dismiss 该条 → 不应覆盖."""
    from unittest.mock import MagicMock, patch
    from reviewagent.commands.suggestion_actions import auto_detect_applied
    from reviewagent.telemetry.store import get_store

    store = get_store()
    head_sha = "01234567" * 5
    store.record_suggestion(
        project_id=34, mr_iid=300,
        note_id="race-note-id",
        file_path="services/sample.py",
        target_line=5, target_line_end=5,
        existing_code="x = 1",
        improved_code="x = 2",
        header="test", label="code quality",
        severity="low", head_sha=head_sha,
        rule_keys=["TEST"],
        fingerprint="fprace", cohort_key="co_race",
        severity_source="rule",
    )

    original_get = store.get_suggestion_by_note_id
    call_count = {"n": 0}

    def racing_get(note_id):
        call_count["n"] += 1
        result = original_get(note_id)
        if call_count["n"] == 1:
            if result and result.get("state") == "open":
                store.update_suggestion_state(
                    note_id, "dismissed",
                    actor_username="racer",
                    dismissed_reason="user dismissed during scan",
                )
                result = original_get(note_id)
        return result

    gl_mock = MagicMock()
    gl_mock.get_file_at_sha.return_value = "x = 2\n"
    gl_mock.is_discussion_resolved.return_value = True
    gl_mock.resolve_discussion.return_value = True

    with patch(
        "reviewagent.commands.suggestion_actions.GitLabClient",
        return_value=gl_mock,
    ), patch.object(store, "get_suggestion_by_note_id", side_effect=racing_get):
        result = auto_detect_applied(
            project_id=34, mr_iid=300, head_sha=head_sha,
        )

    assert result["applied"] == 0, (
        f"dismissed 中途的 suggestion 不应被标记为 applied, got {result}"
    )
    final = original_get("race-note-id")
    assert final["state"] == "dismissed", (
        f"state 应保持 dismissed, 实际 {final['state']}"
    )
