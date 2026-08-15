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


def test_resolved_with_exact_match_flips_to_applied(tmp_telemetry):
    """exact_match=True → 翻 applied (Batch1 保持)."""
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
    posted = "def f():\n    x = 1\n    return x\n"
    current = "def f():\n    x = 2\n    return x\n"
    gl.get_file_at_sha.side_effect = lambda pid, path, sha: (
        posted if sha.startswith("0a9043b0") else current
    )
    gl.is_discussion_resolved.return_value = True

    with patch("reviewagent.commands.suggestion_actions.GitLabClient", return_value=gl):
        result = auto_detect_applied(project_id=34, mr_iid=247, head_sha=head_sha)

    assert result["late_apply"] == 1, f"exact_match 应翻转, got {result}"
    final = s.get_suggestion_by_note_id("resolved-note-1")
    assert final["state"] == "applied"


def test_resolved_region_only_stays_resolved(tmp_telemetry):
    """Batch1: region_changed 单独不再翻 applied (MR 249 误分类修复)."""
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
    # current 文件目标行有变, 但不是 improved_code 完整内容
    posted = "def f():\n    x = 1\n    return x\n"
    current = "def f():\n    x = 99\n    return x\n"
    gl.get_file_at_sha.side_effect = lambda pid, path, sha: (
        posted if sha.startswith("0a9043b0") else current
    )
    gl.is_discussion_resolved.return_value = True

    with patch("reviewagent.commands.suggestion_actions.GitLabClient", return_value=gl):
        result = auto_detect_applied(project_id=34, mr_iid=247, head_sha=head_sha)

    assert result["late_apply"] == 0, f"region_only 不应翻 applied, got {result}"
    final = s.get_suggestion_by_note_id("resolved-note-1")
    assert final["state"] == "resolved", f"应保持 resolved, got {final['state']}"


def test_resolved_strict_token_match_flips_to_applied(tmp_telemetry):
    """Batch1: 严格 token 匹配 (新 token 占比>=0.8, 旧 token 残余<30%) 翻 applied."""
    from reviewagent.commands.suggestion_actions import auto_detect_applied
    from reviewagent.telemetry.store import get_store

    s = get_store()
    head_sha = "8b7e58a3" + "0" * 32
    _seed_resolved_suggestion(
        s, head_sha=head_sha,
        target_line=2, target_line_end=3,
        existing_code="print(\"diag: " + "{" + "x" + "}\")\n",
        improved_code="logger.info(\"diag: %s\", x)\n",
    )

    gl = MagicMock()
    posted = "def f():\n    print(\"diag: " + "{" + "x" + "}\")\n    return\n"
    current = "def f():\n    logger.info(\"diag: %s\", x)\n    return\n"
    gl.get_file_at_sha.side_effect = lambda pid, path, sha: (
        posted if sha.startswith("0a9043b0") else current
    )
    gl.is_discussion_resolved.return_value = True

    with patch("reviewagent.commands.suggestion_actions.GitLabClient", return_value=gl):
        result = auto_detect_applied(project_id=34, mr_iid=247, head_sha=head_sha)

    assert result["late_apply"] == 1, f"严格 token 匹配应翻 applied, got {result}"
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


# ---------- Batch2: 汇总按 cohort 归并 ----------
def test_list_latest_by_cohort_dedup(tmp_telemetry):
    """MR299 修复: 同 cohort V1 open, V2 applied, V3 open → 都保留 (V2 用户的 applied 
    不能被 V3 新 open 覆盖).
    同 cohort 全 open 仍 dedup, 但 terminal + open 共存时 terminal 也保留."""
    from reviewagent.telemetry.store import get_store
    s = get_store()
    head = "ab" * 32
    # 同 cohort 3 轮发布: V1 open, V2 applied, V3 open
    for i, state in enumerate(["open", "applied", "open"]):
        nid = f"note-v{i+1}"
        s.record_suggestion(
            project_id=34, mr_iid=247, note_id=nid,
            file_path="a.py", target_line=2, target_line_end=2,
            existing_code="x = 1\n", improved_code="x = 2\n",
            header="docstring", label="l",
            fingerprint=f"fp-{i}", cohort_key="c-docstring",
            severity_source="rule", head_sha=head,
        )
        if state != "open":
            s.update_suggestion_state(nid, state, actor_username="t")
    # 独立 cohort
    s.record_suggestion(
        project_id=34, mr_iid=247, note_id="note-print",
        file_path="a.py", target_line=10, target_line_end=10,
        existing_code="print(x)\n", improved_code="logger.info(x)\n",
        header="bare print", label="l",
        fingerprint="fp-print", cohort_key="c-print",
        severity_source="rule", head_sha=head,
    )
    rows = s.list_latest_by_cohort(project_id=34, mr_iid=247)
    # MR299 修复后: terminal V2 (applied) + 最新 V3 (open) + 独立 c-print = 3 条
    assert len(rows) == 3, f"应保留 V3 (open) + V2 (applied) + 独立 c-print, got {len(rows)}"
    # cohort "c-docstring": V3 (open, 最新) + V2 (applied, terminal) → 2 条
    docstring_rows = [r for r in rows if r["cohort_key"] == "c-docstring"]
    assert len(docstring_rows) == 2, f"同 cohort 应保留 2 条 (V3 open + V2 applied), got {len(docstring_rows)}"
    # V3 是最新 (id 最大), V2 是 applied terminal
    note_ids = sorted(r["note_id"] for r in docstring_rows)
    assert note_ids == ["note-v2", "note-v3"], f"应保留 V2(applied) + V3(open), got {note_ids}"
    states = sorted(r["state"] for r in docstring_rows)
    assert states == ["applied", "open"], f"应保留 applied + open, got {states}"
    # 独立 cohort
    print_row = [r for r in rows if r["cohort_key"] == "c-print"][0]
    assert print_row["state"] == "open"


def test_supersede_suggestion(tmp_telemetry):
    """supsersede_suggestion 应把旧记录标 superseded + 写 supersedes_note_id."""
    from reviewagent.telemetry.store import get_store
    s = get_store()
    head = "cd" * 32
    s.record_suggestion(
        project_id=34, mr_iid=247, note_id="old",
        file_path="a.py", target_line=2,
        existing_code="x=1\n", improved_code="x=2\n",
        header="h", label="l",
        fingerprint="fp", cohort_key="c",
        severity_source="rule", head_sha=head,
    )
    s.record_suggestion(
        project_id=34, mr_iid=247, note_id="new",
        file_path="a.py", target_line=2,
        existing_code="x=1\n", improved_code="x=2\n",
        header="h", label="l",
        fingerprint="fp2", cohort_key="c",
        severity_source="rule", head_sha=head,
    )
    s.supersede_suggestion("old", "new", generation=2)
    old = s.get_suggestion_by_note_id("old")
    new = s.get_suggestion_by_note_id("new")
    assert old["state"] == "superseded"
    assert old["supersedes_note_id"] == "new"
    assert new["cohort_generation"] == 2


def test_build_overview_body_dedup(tmp_telemetry):
    """MR299 修复: build_overview_body 多条 terminal state 的同 cohort 全部计入.
    之前 (Batch2 旧语义): 同 cohort 多条 applied 只算 1 → 丢了用户的多轮明确动作.
    现在: 任何 terminal state 都计入 (用户动作不互盖)."""
    from reviewagent.commands._common import build_overview_body
    from reviewagent.telemetry.store import get_store
    s = get_store()
    head = "ef" * 32
    # 同 cohort 3 条, 都是 applied (极端 case, 实际难以触发, 但语义上保留)
    for i in range(3):
        s.record_suggestion(
            project_id=34, mr_iid=247, note_id=f"note-doc{i}",
            file_path="a.py", target_line=2, target_line_end=2,
            existing_code="x=1\n", improved_code="x=2\n",
            header="docstring", label="l",
            fingerprint=f"fp-d{i}", cohort_key="c-doc",
            severity_source="rule", head_sha=head,
        )
        s.update_suggestion_state(f"note-doc{i}", "applied", actor_username="t")
    body = build_overview_body(project_id=34, mr_iid=247)
    # 修复后: 3 条 applied terminal 都保留, MEDIUM 行 applied=3
    # 表格行格式: | 🟡 MEDIUM | 0 | 3 | 0 | 0 | 3 |
    import re
    m = re.search(r"\| 🟡 MEDIUM \| (\d+) \| (\d+) \| (\d+) \| (\d+) \| (\d+) \|", body)
    assert m, f"未找到 MEDIUM 行:\n{body}"
    open_n, applied_n, dismissed_n, resolved_n, sum_n = map(int, m.groups())
    assert applied_n == 3, f"3 条 applied terminal 都应保留, got {applied_n}"
    assert sum_n == 3, f"3 条 cohort 成员都应参与, got {sum_n}"
    # "已被合并" 标注对应 cohort 隐藏数 (row_number > 1): 3 条 cohort 留 3 条,
    # 但 hidden by cohort 只算 row_number > 1 = 2. 这里验证 '已被合并' 行存在且数字对.
    # 修复前 (Batch2 旧语义): 只留 1, hidden=2 标注有; 修复后: 留 3, hidden=2 (同样).
    assert "已被合并" in body, f"应显示 cohort 隐藏数标注:\n{body}"
    assert "2 条已被合并" in body, f"cohort c-doc 3 条, hidden by cohort 应为 2:\n{body}"


# ---------- Batch3: occurrence/generation ----------
def test_get_latest_in_cohort_excluding(tmp_telemetry):
    """get_latest_in_cohort_excluding 返回同 cohort 排除自己后的最新一条."""
    from reviewagent.telemetry.store import get_store
    s = get_store()
    head = "aa" * 32
    for i in range(3):
        s.record_suggestion(
            project_id=34, mr_iid=247, note_id=f"n{i}",
            file_path="a.py", target_line=2, target_line_end=2,
            existing_code="x=1\n", improved_code="x=2\n",
            header="h", label="l",
            fingerprint=f"fp{i}", cohort_key="c-X",
            severity_source="rule", head_sha=head,
        )
    s.update_suggestion_state("n0", "dismissed", actor_username="t")
    s.update_suggestion_state("n1", "applied", actor_username="t")
    latest = s.get_latest_in_cohort_excluding(
        project_id=34, mr_iid=247, cohort_key="c-X",
        exclude_note_id="n2",  # 最新
    )
    assert latest is not None
    assert latest["note_id"] == "n1"
    assert latest["state"] == "applied"


def test_handle_cohort_reoccurrence_supersedes(tmp_telemetry):
    """Batch3: 同 cohort 旧 applied + 当前 existing 仍出现 → supersede."""
    from unittest.mock import MagicMock
    from reviewagent.commands.improve import _handle_cohort_reoccurrence
    from reviewagent.telemetry.store import get_store
    s = get_store()
    head = "bb" * 32
    s.record_suggestion(
        project_id=34, mr_iid=247, note_id="n-applied",
        file_path="a.py", target_line=2, target_line_end=2,
        existing_code="print(x)\n", improved_code="logger.info(x)\n",
        header="h", label="l",
        fingerprint="fp1", cohort_key="c-print",
        severity_source="rule", head_sha=head,
    )
    s.update_suggestion_state("n-applied", "applied", actor_username="t")
    # 当前 head_sha 文件里 existing 仍出现
    gl = MagicMock()
    gl.get_file_at_sha.return_value = "def f():\n    print(x)\n    return\n"
    _handle_cohort_reoccurrence(
        store=s, project_id=34, mr_iid=247,
        cohort_key="c-print", new_note_id="n-new",
        file_path="a.py", target_line=2, target_line_end=2,
        existing="print(x)\n", head_sha=head, gitlab=gl,
    )
    old = s.get_suggestion_by_note_id("n-applied")
    assert old["state"] == "superseded"
    assert old["supersedes_note_id"] == "n-new"


def test_handle_cohort_reoccurrence_no_op_when_problem_fixed(tmp_telemetry):
    """Batch3: existing 已不出现 → 不 supersede (走正常 dedup 路径)."""
    from unittest.mock import MagicMock
    from reviewagent.commands.improve import _handle_cohort_reoccurrence
    from reviewagent.telemetry.store import get_store
    s = get_store()
    head = "cc" * 32
    s.record_suggestion(
        project_id=34, mr_iid=247, note_id="n-applied",
        file_path="a.py", target_line=2, target_line_end=2,
        existing_code="print(x)\n", improved_code="logger.info(x)\n",
        header="h", label="l",
        fingerprint="fp1", cohort_key="c-print",
        severity_source="rule", head_sha=head,
    )
    s.update_suggestion_state("n-applied", "applied", actor_username="t")
    # current 文件里 print(x) 已被 logger.info 替换
    gl = MagicMock()
    gl.get_file_at_sha.return_value = "def f():\n    logger.info(x)\n    return\n"
    _handle_cohort_reoccurrence(
        store=s, project_id=34, mr_iid=247,
        cohort_key="c-print", new_note_id="n-new",
        file_path="a.py", target_line=2, target_line_end=2,
        existing="print(x)\n", head_sha=head, gitlab=gl,
    )
    old = s.get_suggestion_by_note_id("n-applied")
    assert old["state"] == "applied"  # 未被 supersede


# ---------- Batch4: Apply commit 关联 + adoption_evidence ----------
def test_find_latest_apply_commit():
    """Batch4: 在 commits 列表里找 Apply ... 的 commit."""
    from reviewagent.commands.suggestion_actions import _find_latest_apply_commit
    commits = [
        {"id": "abc12345", "short_id": "abc12345", "title": "feat: add thing"},
        {"id": "def67890", "short_id": "def67890", "title": "Apply 1 suggestion(s) to 1 file(s)"},
    ]
    # head_sha 不匹配 → fallback 到第一个 Apply commit
    assert _find_latest_apply_commit(commits, head_sha="99999999") == "def67890"
    # head_sha 匹配 → 返回对应 commit
    assert _find_latest_apply_commit(commits, head_sha="def67890abc") == "def67890"
    # 没有 Apply commit
    assert _find_latest_apply_commit(
        [{"id": "x", "short_id": "x", "title": "fix: stuff"}], head_sha="x",
    ) == ""
    # 空 / 非法输入
    assert _find_latest_apply_commit([], head_sha="x") == ""
    assert _find_latest_apply_commit(None, head_sha="x") == ""
    assert _find_latest_apply_commit("not list", head_sha="x") == ""


def test_auto_detect_records_adoption_evidence(tmp_telemetry):
    """exact_match 路径应写 adoption_evidence='exact_match' 到 suggestion 行."""
    from reviewagent.commands.suggestion_actions import auto_detect_applied
    from reviewagent.telemetry.store import get_store
    s = get_store()
    head = "head01" + "0" * 32
    s.record_suggestion(
        project_id=34, mr_iid=247, note_id="n-evidence",
        file_path="a.py", target_line=2, target_line_end=2,
        existing_code="x = 1\n", improved_code="x = 2\n",
        header="h", label="l",
        fingerprint="fp-evi", cohort_key="c-evi",
        severity_source="rule", head_sha=head,
    )
    from unittest.mock import MagicMock, patch
    gl = MagicMock()
    gl.get_file_at_sha.side_effect = ["x = 2\n", "x = 1\n"]
    gl.is_discussion_resolved.return_value = True
    gl.resolve_discussion.return_value = True
    gl.list_mr_commits.return_value = [
        {"id": "head01" + "0"*32, "short_id": "head01", "title": "Apply 1 suggestion(s) to 1 file(s)"},
    ]
    with patch("reviewagent.commands.suggestion_actions.GitLabClient", return_value=gl):
        result = auto_detect_applied(project_id=34, mr_iid=247, head_sha=head)
    assert result["applied"] == 1
    sug = s.get_suggestion_by_note_id("n-evidence")
    assert sug["state"] == "applied"
    assert sug["adoption_evidence"] == "exact_match"
    assert sug["applied_commit_sha"] == "head01"


# ---------- MR289 根因 #2 回归测试: publish_overview_reconcile 路径 ----------
# Bug: pre_reconcile (commands/_common.py:_scan_and_mark_resolved_silent) 标
# resolution_source='publish_overview_reconcile', 旧 list_resolved_suggestions SQL
# 只查 'gitlab_resolve', late_detect 永远扫不到, webhook 漏发时永远停在 resolved.
# 修复: SQL IN ('gitlab_resolve', 'publish_overview_reconcile').
# 验证:
#   1. exact_match 命中 → 翻 applied (不能漏掉已 apply 的)
#   2. exact_match 未命中 → 保持 resolved (false positive 保护)
#   3. /adopt 路径 (adopt_command) 不被 late_detect 覆盖 → 保持 adopt 语义

def test_publish_overview_reconcile_flips_to_applied(tmp_telemetry):
    """resolution_source='publish_overview_reconcile' + exact_match → 翻 applied (MR289 Fix #2)."""
    from reviewagent.commands.suggestion_actions import auto_detect_applied
    from reviewagent.telemetry.store import get_store

    s = get_store()
    head_sha = "8b7e58a3" + "0" * 32
    _seed_resolved_suggestion(
        s, head_sha=head_sha,
        note_id="reconcile-note-1",
        resolution_source="publish_overview_reconcile",
        record_action=True,
    )

    gl = MagicMock()
    gl.get_file_at_sha.return_value = (
        "def foo():\n"
        "    x = 2\n"          # ← improved_code 完整出现
        "    return x\n"
    )
    gl.is_discussion_resolved.return_value = True

    with patch("reviewagent.commands.suggestion_actions.GitLabClient", return_value=gl):
        result = auto_detect_applied(project_id=34, mr_iid=247, head_sha=head_sha)

    assert result["late_apply"] == 1, (
        f"publish_overview_reconcile + exact_match 应翻 1 条, got {result}"
    )
    final = s.get_suggestion_by_note_id("reconcile-note-1")
    assert final["state"] == "applied", (
        f"state 应翻 applied, got {final['state']}"
    )
    assert final["adoption_source"] == "late_detect", (
        f"adoption_source 应是 late_detect, got {final['adoption_source']}"
    )

    # 原 action 历史保留 (resolved action 不被删除)
    actions = s.list_suggestion_actions(project_id=34, mr_iid=247)
    assert len(actions) == 2, f"应有 2 条 action (resolved + adopted), got {len(actions)}"
    action_kinds = sorted(a["action"] for a in actions)
    assert action_kinds == ["adopted", "resolved"], f"got {action_kinds}"


def test_publish_overview_reconcile_unchanged_stays_resolved(tmp_telemetry):
    """resolution_source='publish_overview_reconcile' + !exact_match → 保持 resolved."""
    from reviewagent.commands.suggestion_actions import auto_detect_applied
    from reviewagent.telemetry.store import get_store

    s = get_store()
    head_sha = "8b7e58a3" + "0" * 32
    _seed_resolved_suggestion(
        s, head_sha=head_sha,
        note_id="reconcile-note-2",
        resolution_source="publish_overview_reconcile",
    )

    gl = MagicMock()
    gl.get_file_at_sha.return_value = (
        "def foo():\n"
        "    x = 999\n"        # ← 完全不同, exact_match=False
        "    return x\n"
    )
    gl.is_discussion_resolved.return_value = True

    with patch("reviewagent.commands.suggestion_actions.GitLabClient", return_value=gl):
        result = auto_detect_applied(project_id=34, mr_iid=247, head_sha=head_sha)

    assert result["late_apply"] == 0, f"不应翻转, got {result}"
    final = s.get_suggestion_by_note_id("reconcile-note-2")
    assert final["state"] == "resolved", (
        f"state 应保持 resolved, got {final['state']}"
    )


def test_publish_overview_reconcile_dismissed_not_flipped(tmp_telemetry):
    """resolution_source='adopt_command' (用户主动 /adopt) 不被 late_detect 覆盖.

    Why: /adopt 路径走的是 adoption_source='adopt_command', SQL 白名单不含
    这条, 防止 bot 误把"用户已主动采纳"重新判定. 这条测试覆盖 SQL 边界.
    """
    from reviewagent.commands.suggestion_actions import auto_detect_applied
    from reviewagent.telemetry.store import get_store

    s = get_store()
    head_sha = "8b7e58a3" + "0" * 32
    # /adopt 路径: adopt_command (在 SQL 白名单之外, 不被 late_detect 翻)
    _seed_resolved_suggestion(
        s, head_sha=head_sha,
        note_id="adopt-note-1",
        resolution_source="adopt_command",
    )

    gl = MagicMock()
    # 即使代码精确匹配, /adopt 已有的 resolved 也不被覆盖
    gl.get_file_at_sha.return_value = (
        "def foo():\n"
        "    x = 2\n"          # exact_match=True
        "    return x\n"
    )
    gl.is_discussion_resolved.return_value = True

    with patch("reviewagent.commands.suggestion_actions.GitLabClient", return_value=gl):
        result = auto_detect_applied(project_id=34, mr_iid=247, head_sha=head_sha)

    assert result["late_apply"] == 0, (
        f"adopt_command 不在 SQL 白名单, 不应翻转, got {result}"
    )
    final = s.get_suggestion_by_note_id("adopt-note-1")
    assert final["state"] == "resolved", (
        f"state 应保持 resolved (adopt_command 不被覆盖), got {final['state']}"
    )


# ---------- MR289 根因 #1 回归测试: _handle_push 的 auto_detect_applied 不被 cooldown skip ----------
# Bug: _handle_push 中 cooldown skip 时 continue, 跳过了 auto_detect_applied.
# 修复: auto_detect_applied 调用移到 cooldown check 之前.
# 验证: locks.should_skip_cooldown 返回 True 时, auto_detect_applied 仍被调用.
# 策略: 直接测 _handle_push 的副作用 — mockside_effect 让 locks.should_skip_cooldown 返回 True,
# 然后断言 auto_detect_applied 被调用了.

def test_handle_push_calls_auto_detect_even_under_cooldown():
    """MR289 根因 #1: _handle_push 在 cooldown skip 时也要调用 auto_detect_applied."""
    import asyncio
    from unittest.mock import patch, MagicMock, AsyncMock

    payload = {
        "ref": "refs/heads/codex/test-branch",
        "project": {"id": 34},
        "user_username": "tester",
        "user_name": "Test User",
    }

    # 关键: locks.should_skip_cooldown 返回 True (模拟 cooldown 期内 push)
    with patch(
        "reviewagent.webhook.router.locks.should_skip_cooldown",
        return_value=True,
    ), patch(
        "reviewagent.webhook.router.locks.is_bot",
        return_value=False,
    ), patch(
        "reviewagent.commands.suggestion_actions.auto_detect_applied",
    ) as mock_ad, patch(
        "reviewagent.gitlab.client.GitLabClient.list_project_mrs",
    ) as mock_mrs:
        # _handle_push 对 ("opened", "merged") 两个 state 各调一次
        # 只在 "opened" 返回 MR, "merged" 返回空
        def _mrs_side_effect(*args, **kwargs):
            if kwargs.get("state") == "opened":
                return [{"iid": 247, "sha": "abc123" + "0" * 32}]
            return []
        mock_mrs.side_effect = _mrs_side_effect
        mock_ad.return_value = {"scanned": 0, "applied": 0, "late_apply": 0}

        # Mock enqueue_mr_chain 不实际入队
        enqueue = MagicMock()
        enqueue.return_value = ["fake-job-id"]

        from reviewagent.webhook.router import _handle_push
        result = asyncio.run(_handle_push(payload, enqueue))

    # 关键断言: auto_detect_applied 必须被调用了 (即使 cooldown skip)
    assert mock_ad.called, (
        f"auto_detect_applied 应在 cooldown skip 之前调用, got calls={mock_ad.call_args_list}"
    )
    # enqueue_mr_chain 不应被调用 (cooldown skip 时)
    enqueue.assert_not_called(), (
        f"enqueue_mr_chain 在 cooldown skip 时不应被调用, got calls={enqueue.call_args_list}"
    )
    # mock_mrs 必须被调用 (说明 _handle_push 走到了查 MR 的代码段)
    assert mock_mrs.called, (
        f"list_project_mrs 应被调用, got calls={mock_mrs.call_args_list}"
    )
