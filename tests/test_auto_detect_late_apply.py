"""Regression tests: auto_detect_applied 应在 open 扫完后, 再扫一次
state='resolved' + resolution_source='gitlab_resolve' 的 suggestions, 把那些
「先点 Resolve thread 后 push 代码落地」的误分类翻回 applied.

Why:
    用户先在 GitLab UI 点「解决主题」(resolved=True), 再 push commit 让代码落地.
    当时 auto_detect 跑那一遍 exact_match 没命中 (head_sha 还没变) → 标 resolved.
    后续 push 触发再跑时, 这条 suggestion 已不在 list_open_suggestions 里, 永远
    不会被重检, 数据就停在"已关闭 (未分类)"但实际代码已采纳的状态.

    修复后, late_detect 用同一套 exact_match / region_changed / token_fallback
    重判, 命中就翻 applied + 记一条 action=adopted, adoption_source='late_detect'.
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


def _seed_resolved_suggestion(
    s, *, project_id=34, mr_iid=247, head_sha="0a9043b0" * 4,
    note_id="resolved-note-1", file_path="a.py", target_line=2,
    target_line_end=2,
    existing_code="x = 1\n",
    improved_code="x = 2\n",
    resolution_source="gitlab_resolve",
    record_action=True,
):
    """Seed 1 条已 resolved + gitlab_resolve 的 suggestion.

    record_action=True 时同步写一条 resolved action (跟生产环境 bot 走的一样),
    用于测 "late_detect 翻转后保留原 action 历史" 场景.
    """
    s.record_suggestion(
        project_id=project_id, mr_iid=mr_iid, note_id=note_id,
        file_path=file_path, target_line=target_line,
        target_line_end=target_line_end,
        existing_code=existing_code,
        improved_code=improved_code,
        header="late-detect-test", label="late",
        fingerprint=f"fp-{note_id}", cohort_key=f"c-{note_id}",
        severity_source="rule", head_sha=head_sha,
    )
    s.update_suggestion_state(
        note_id, "resolved", actor_username="tester",
        adoption_source=resolution_source,
    )
    if record_action:
        s.record_suggestion_action(
            project_id=project_id, mr_iid=mr_iid,
            suggestion_note_id=note_id, file_path=file_path,
            target_line=target_line, action="resolved",
            actor_username="tester",
            reason="GitLab 直接解决主题，未检测到建议代码落地",
            validation_status="gitlab-resolve",
            adoption_source=resolution_source,
            head_sha_posted=head_sha, head_sha_current=head_sha,
        )


def test_resolved_with_matching_code_flips_to_applied(tmp_telemetry):
    """exact_match=True → resolved → applied, adoption_source='late_detect'."""
    from reviewagent.commands.suggestion_actions import auto_detect_applied
    from reviewagent.telemetry.store import get_store

    s = get_store()
    head_sha = "8b7e58a3" + "0" * 32
    _seed_resolved_suggestion(s, head_sha=head_sha)

    gl = MagicMock()
    gl.get_file_at_sha.return_value = (
        "def foo():\n"
        "    x = 2\n"          # ← improved_code 完整出现
        "    return x\n"
    )
    gl.is_discussion_resolved.return_value = True
    gl.resolve_discussion.return_value = True

    with patch("reviewagent.commands.suggestion_actions.GitLabClient", return_value=gl):
        result = auto_detect_applied(project_id=34, mr_iid=247, head_sha=head_sha)

    assert result["late_apply"] == 1, f"应翻转 1 条, got {result}"
    final = s.get_suggestion_by_note_id("resolved-note-1")
    assert final["state"] == "applied", f"state 应翻 applied, got {final['state']}"
    assert final["adoption_source"] == "late_detect", \
        f"adoption_source 应是 late_detect, got {final['adoption_source']}"

    actions = s.list_suggestion_actions(project_id=34, mr_iid=247)
    adopted_actions = [a for a in actions if a["action"] == "adopted"]
    assert len(adopted_actions) == 1
    assert adopted_actions[0]["validation_status"] == "late-detect-apply"
    assert adopted_actions[0]["adoption_source"] == "late_detect"


def test_resolved_without_matching_code_stays_resolved(tmp_telemetry):
    """exact_match=False → 保持 resolved, 不翻."""
    from reviewagent.commands.suggestion_actions import auto_detect_applied
    from reviewagent.telemetry.store import get_store

    s = get_store()
    head_sha = "8b7e58a3" + "0" * 32
    _seed_resolved_suggestion(s, head_sha=head_sha)

    gl = MagicMock()
    gl.get_file_at_sha.return_value = (
        "def foo():\n"
        "    x = 999\n"        # ← 完全不同
        "    return x\n"
    )
    gl.is_discussion_resolved.return_value = True

    with patch("reviewagent.commands.suggestion_actions.GitLabClient", return_value=gl):
        result = auto_detect_applied(project_id=34, mr_iid=247, head_sha=head_sha)

    assert result["late_apply"] == 0, f"不应翻转, got {result}"
    final = s.get_suggestion_by_note_id("resolved-note-1")
    assert final["state"] == "resolved", f"state 应保持 resolved, got {final['state']}"


def test_resolved_with_region_changed_flips_to_applied(tmp_telemetry):
    """exact_match=False 但 _target_region_changed=True (用户在目标行附近改了等价代码) → 翻 applied."""
    from reviewagent.commands.suggestion_actions import auto_detect_applied
    from reviewagent.telemetry.store import get_store

    s = get_store()
    head_sha = "8b7e58a3" + "0" * 32
    _seed_resolved_suggestion(
        s, head_sha=head_sha,
        target_line=5, target_line_end=7,
        existing_code="def f():\n    x = 1\n    return x\n",
        improved_code="def f():\n    x = 2\n    return x\n",
    )

    gl = MagicMock()
    # posted 时代 (0a9043b0) vs current 时代 (8b7e58a3) — 目标行 x=1 被改成 x=2
    posted = "def f():\n    x = 1\n    return x\n"
    current = "def f():\n    x = 2\n    return x\n"
    gl.get_file_at_sha.side_effect = lambda pid, path, sha: (
        posted if sha.startswith("0a9043b0") else current
    )
    gl.is_discussion_resolved.return_value = True

    with patch("reviewagent.commands.suggestion_actions.GitLabClient", return_value=gl):
        result = auto_detect_applied(project_id=34, mr_iid=247, head_sha=head_sha)

    assert result["late_apply"] == 1, f"region_changed 应翻转, got {result}"
    final = s.get_suggestion_by_note_id("resolved-note-1")
    assert final["state"] == "applied"


def test_dismissed_or_adopted_not_in_resolved_list(tmp_telemetry):
    """state='dismissed' 或 'applied' 的不进 late_detect 扫, 不被覆盖."""
    from reviewagent.commands.suggestion_actions import auto_detect_applied
    from reviewagent.telemetry.store import get_store

    s = get_store()
    head_sha = "8b7e58a3" + "0" * 32

    # seed 3 条: 1 applied, 1 dismissed, 1 resolved (应只扫 resolved)
    for note_id, state, adoption_source in [
        ("applied-note", "applied", "ui_apply"),
        ("dismissed-note", "dismissed", None),
        ("resolved-note", "resolved", "gitlab_resolve"),
    ]:
        s.record_suggestion(
            project_id=34, mr_iid=247, note_id=note_id,
            file_path="a.py", target_line=2, target_line_end=2,
            existing_code="x = 1\n", improved_code="x = 2\n",
            header=f"h-{note_id}", label="l",
            fingerprint=f"fp-{note_id}", cohort_key=f"c-{note_id}",
            severity_source="rule", head_sha=head_sha,
        )
        s.update_suggestion_state(
            note_id, state, actor_username="tester",
            adoption_source=adoption_source,
            dismissed_reason="test" if state == "dismissed" else None,
        )

    gl = MagicMock()
    gl.get_file_at_sha.return_value = "x = 2\n"
    gl.is_discussion_resolved.return_value = True

    with patch("reviewagent.commands.suggestion_actions.GitLabClient", return_value=gl):
        result = auto_detect_applied(project_id=34, mr_iid=247, head_sha=head_sha)

    # 只有 resolved-note 应被翻, applied/dismissed 不进 list
    assert result["late_apply"] == 1
    assert s.get_suggestion_by_note_id("applied-note")["state"] == "applied"
    assert s.get_suggestion_by_note_id("dismissed-note")["state"] == "dismissed"
    assert s.get_suggestion_by_note_id("resolved-note")["state"] == "applied"


def test_open_suggestions_still_work(tmp_telemetry):
    """回归: late_detect 不应破坏原本的 open 扫逻辑."""
    from reviewagent.commands.suggestion_actions import auto_detect_applied
    from reviewagent.telemetry.store import get_store

    s = get_store()
    head_sha = "8b7e58a3" + "0" * 32

    # 1 条 open + 1 条 resolved
    s.record_suggestion(
        project_id=34, mr_iid=247, note_id="open-note",
        file_path="a.py", target_line=2, target_line_end=2,
        existing_code="x = 1\n", improved_code="x = 2\n",
        header="h", label="l",
        fingerprint="fp-open", cohort_key="c-open",
        severity_source="rule", head_sha=head_sha,
    )
    _seed_resolved_suggestion(s, head_sha=head_sha, note_id="resolved-note-2")

    gl = MagicMock()
    gl.get_file_at_sha.return_value = "x = 2\n"
    gl.is_discussion_resolved.return_value = False  # open 那条还没关
    gl.resolve_discussion.return_value = True

    with patch("reviewagent.commands.suggestion_actions.GitLabClient", return_value=gl):
        result = auto_detect_applied(project_id=34, mr_iid=247, head_sha=head_sha)

    assert result["applied"] == 1, f"open 那条应被 applied, got {result}"
    assert result["late_apply"] == 1, f"resolved 那条应被 late_apply, got {result}"


def test_resolved_via_adopt_command_not_flipped(tmp_telemetry):
    """/adopt 流程走的也是 resolved state 但 adoption_source='adopt_command',
    resolution_source 也是 gitlab_resolve 的话 — Wait, /adopt 状态是 applied 不是
    resolved. 这里验证: 只有 state='resolved' + resolution_source='gitlab_resolve'
    才会被 late_detect 扫. /adopt 走的是 applied, 不会进 list_resolved_suggestions."""
    from reviewagent.commands.suggestion_actions import auto_detect_applied
    from reviewagent.telemetry.store import get_store

    s = get_store()
    head_sha = "8b7e58a3" + "0" * 32

    # state='applied' 不进 list_resolved_suggestions
    s.record_suggestion(
        project_id=34, mr_iid=247, note_id="adopt-cmd-note",
        file_path="a.py", target_line=2, target_line_end=2,
        existing_code="x = 1\n", improved_code="x = 2\n",
        header="h", label="l",
        fingerprint="fp-adopt", cohort_key="c-adopt",
        severity_source="rule", head_sha=head_sha,
    )
    s.update_suggestion_state(
        "adopt-cmd-note", "applied", actor_username="tester",
        adoption_source="adopt_command",
    )

    gl = MagicMock()
    gl.get_file_at_sha.return_value = "x = 2\n"
    gl.is_discussion_resolved.return_value = True

    with patch("reviewagent.commands.suggestion_actions.GitLabClient", return_value=gl):
        result = auto_detect_applied(project_id=34, mr_iid=247, head_sha=head_sha)

    # applied 不在 late_detect 范围, 应 unchanged
    assert result["late_apply"] == 0
    final = s.get_suggestion_by_note_id("adopt-cmd-note")
    assert final["state"] == "applied"
    assert final["adoption_source"] == "adopt_command"  # 不被覆盖


def test_late_detect_records_two_actions(tmp_telemetry):
    """late_detect 翻转后, suggestion_actions 里应同时有 resolved (原) 和 adopted (新) 两条."""
    from reviewagent.commands.suggestion_actions import auto_detect_applied
    from reviewagent.telemetry.store import get_store

    s = get_store()
    head_sha = "8b7e58a3" + "0" * 32
    _seed_resolved_suggestion(s, head_sha=head_sha)

    gl = MagicMock()
    gl.get_file_at_sha.return_value = "x = 2\n"
    gl.is_discussion_resolved.return_value = True

    with patch("reviewagent.commands.suggestion_actions.GitLabClient", return_value=gl):
        auto_detect_applied(project_id=34, mr_iid=247, head_sha=head_sha)

    actions = s.list_suggestion_actions(project_id=34, mr_iid=247)
    by_action = {}
    for a in actions:
        by_action.setdefault(a["action"], []).append(a)
    assert "resolved" in by_action, f"应保留原 resolved action, got {by_action}"
    assert "adopted" in by_action, f"应新增 adopted action, got {by_action}"
    # 原 resolved 的 reason 应是 gitlab_resolve 文案, 新 adopted 是 late_detect 文案
    assert "未检测到" in by_action["resolved"][0]["reason"]
    assert "late_detect" in by_action["adopted"][0]["reason"]
