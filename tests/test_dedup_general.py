"""Regression: general-action (shrinking) suggestions must also pass through
the cross-run dedup guard at MR 155 line 12 / line 19.

Before the fix, the dedup block lived inside `if decision["action"] == "post"`,
so `action == "general"` (删行建议) would short-circuit to a second
post_mr_discussion call without checking whether the same (file, line, head_sha)
was already published. This test drives ImproveCommand._publish with a faked
"general" decision and asserts it is skipped when a prior row exists.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import types
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def tmp_telemetry(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    # Config is a frozen dataclass; patch the Store class's sqlite_path
    # resolution directly by swapping the property at the class level.
    from reviewagent.config import config as _cfg
    monkeypatch.setattr(
        type(_cfg), "sqlite_path",
        property(lambda self: __import__("pathlib").Path(path)),
        raising=False,
    )
    from reviewagent.telemetry import store as st
    st._store = None  # reset cached singleton
    yield path
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    st._store = None


def _make_raw():
    return {
        "file": "tests/test_error_handling.py",
        "line": 12,
        "new_line": 12,
        "header": "静默吞异常",
        "label": "exception",
        "rationale": "bare except swallows real bugs",
        "severity": "high",
        "improved_code": "except (ValueError,):\n    raise",
        "existing_code": "except Exception:\n    pass",
        "rule_keys": ["R-ERR"],
    }


def test_general_action_skipped_when_already_published(tmp_telemetry):
    """Same (file, line, head_sha) already published → general suggestion must
    be skipped, NOT posted again."""
    from reviewagent.commands.improve import ImproveCommand
    from reviewagent.telemetry.store import get_store

    head_sha = "deadbeef" * 5

    # Pre-seed a prior suggestion at the same (file, line, head_sha)
    s = get_store()
    s.record_suggestion(
        project_id=34, mr_iid=155, note_id="seed-note",
        file_path="tests/test_error_handling.py", target_line=12,
        target_line_end=13,
        existing_code="except Exception:\n    pass",
        improved_code="except (ValueError,):\n    raise",
        header="静默吞异常", label="exception",
        fingerprint="abc123", cohort_key="cohort1",
        severity_source="rule",
        head_sha=head_sha,
    )
    # head_sha column required; update it directly
    conn = sqlite3.connect(tmp_telemetry)
    conn.execute("UPDATE suggestions SET head_sha=?, state='open' WHERE note_id='seed-note'", (head_sha,))
    conn.commit()
    conn.close()

    # Build a stub command instance
    cmd = ImproveCommand.__new__(ImproveCommand)
    cmd.project_id = 34
    cmd.mr_iid = 155
    cmd.gitlab = MagicMock()
    cmd.gitlab.post_mr_discussion.return_value = "should-not-be-called"
    cmd.HELP_TEXT_FOOTER = ""

    def _normalise(raw):
        return {
            "file": raw["file"], "new_line": raw["line"],
            "header": raw["header"], "label": raw["label"],
            "rationale": raw["rationale"], "severity": raw["severity"],
            "improved_code": raw["improved_code"],
            "body": "**body**",
        }

    cmd._normalise_suggestion = _normalise  # type: ignore
    cmd._validate_suggestion = lambda **kw: {  # type: ignore
        "action": "general",
        "new_line": kw["start_line"],
        "reason": "shrinking suggestion (5 -> 4 lines)",
    }
    cmd._get_mr_head_sha = lambda: head_sha  # type: ignore

    cmd._diff_line_map = lambda: {}  # type: ignore
    with patch("reviewagent.telemetry.store.get_store", return_value=s):
        result = cmd._publish({"summary_md": "", "suggestions": [_make_raw()]})

    assert result["inline_posted"] == 0
    assert result["inline_skipped"] == 1
    cmd.gitlab.post_mr_discussion.assert_not_called()


def test_post_action_still_skipped_at_line(tmp_telemetry):
    """Sanity: the original `post` path still gets deduped by ±2 tolerance."""
    from reviewagent.commands.improve import ImproveCommand
    from reviewagent.telemetry.store import get_store

    head_sha = "cafebabe" * 5
    s = get_store()
    s.record_suggestion(
        project_id=34, mr_iid=155, note_id="seed-post",
        file_path="tests/test_metrics.py", target_line=10,
        target_line_end=10,
        existing_code="x = 1",
        improved_code="x = 2",
        header="h", label="l",
        fingerprint="fp1", cohort_key="c1",
        severity_source="rule",
        head_sha=head_sha,
    )
    conn = sqlite3.connect(tmp_telemetry)
    conn.execute("UPDATE suggestions SET head_sha=?, state='open' WHERE note_id='seed-post'", (head_sha,))
    conn.commit()
    conn.close()

    cmd = ImproveCommand.__new__(ImproveCommand)
    cmd.project_id = 34
    cmd.mr_iid = 155
    cmd.gitlab = MagicMock()
    cmd.gitlab.post_mr_discussion.return_value = "x"
    cmd.HELP_TEXT_FOOTER = ""

    raw = {
        "file": "tests/test_metrics.py", "line": 11,  # ±1 from 10
        "header": "h", "label": "l",
        "rationale": "r", "severity": "medium",
        "improved_code": "x = 2",
        "existing_code": "x = 1",
        "rule_keys": [],
    }
    cmd._normalise_suggestion = lambda r: {
        "file": r["file"], "new_line": r["line"],
        "header": r["header"], "label": r["label"],
        "rationale": r["rationale"], "severity": r["severity"],
        "improved_code": r["improved_code"],
        "body": "**b**",
    }
    cmd._validate_suggestion = lambda **kw: {"action": "post", "new_line": kw["start_line"]}
    cmd._get_mr_head_sha = lambda: head_sha

    cmd._diff_line_map = lambda: {}  # type: ignore
    with patch("reviewagent.telemetry.store.get_store", return_value=s):
        result = cmd._publish({"summary_md": "", "suggestions": [raw]})

    assert result["inline_posted"] == 0
    assert result["inline_skipped"] == 1
    cmd.gitlab.post_mr_discussion.assert_not_called()


def test_summary_placeholder_posted_before_inline(tmp_telemetry):
    """回归: 改进总览 V{N} placeholder 必须在 inline 建议之前发布,
    这样 GitLab UI 按 created_at 排序时, 本次 run 的总览永远在该 run 的
    inline 建议之上.

    之前 V{N} 实现把 placeholder 写在循环后, 导致 GitLab UI 上 summary
    排在 inline 之后, 违反"总览在最上面"的预期.
    """
    from reviewagent.commands.improve import ImproveCommand
    from reviewagent.telemetry.store import get_store

    head_sha = "feedface" * 5
    s = get_store()

    cmd = ImproveCommand.__new__(ImproveCommand)
    cmd.project_id = 34
    cmd.mr_iid = 999
    cmd.gitlab = MagicMock()
    cmd.gitlab.post_mr_discussion.return_value = "discussion-note-id"
    cmd.gitlab.post_mr_comment.return_value = 4242
    cmd.HELP_TEXT_FOOTER = ""

    call_log: list[str] = []

    def _record_post_comment(*args, **kwargs):
        call_log.append("post_mr_comment")
        return 4242

    def _record_post_discussion(*args, **kwargs):
        call_log.append("post_mr_discussion")
        return "d1"

    def _record_update(*args, **kwargs):
        call_log.append("update_mr_comment")
        return True

    cmd.gitlab.post_mr_comment.side_effect = _record_post_comment
    cmd.gitlab.post_mr_discussion.side_effect = _record_post_discussion
    cmd.gitlab.update_mr_comment.side_effect = _record_update

    raw = {
        "file": "tests/test_metrics.py", "line": 11,
        "header": "h", "label": "l",
        "rationale": "r", "severity": "medium",
        "improved_code": "x = 2",
        "existing_code": "x = 1",
        "rule_keys": [],
    }
    cmd._normalise_suggestion = lambda r: {
        "file": r["file"], "new_line": r["line"],
        "header": r["header"], "label": r["label"],
        "rationale": r["rationale"], "severity": r["severity"],
        "improved_code": r["improved_code"],
        "body": "**b**",
    }
    cmd._validate_suggestion = lambda **kw: {"action": "post", "new_line": kw["start_line"]}
    cmd._get_mr_head_sha = lambda: head_sha
    cmd._diff_line_map = lambda: {}
    cmd._build_summary_placeholder = lambda *a, **kw: "## 改进总览 V1\n\n_加载中…_"
    cmd._build_summary_v2 = lambda *a, **kw: "## 改进总览 V1\n\n本次新发现 1 条"

    with patch("reviewagent.telemetry.store.get_store", return_value=s):
        result = cmd._publish({"summary_md": "", "suggestions": [raw]})

    # 验证: 第一次 post_mr_comment (placeholder) 必须在任何
    # post_mr_discussion (inline) 之前, edit (update_mr_comment) 在最后.
    assert result["inline_posted"] == 1
    assert call_log[0] == "post_mr_comment", (
        f"placeholder 必须在 inline 之前发布, 实际 call_log={call_log}"
    )
    assert "post_mr_discussion" in call_log
    assert "update_mr_comment" in call_log
    placeholder_idx = call_log.index("post_mr_comment")
    inline_idx = call_log.index("post_mr_discussion")
    edit_idx = call_log.index("update_mr_comment")
    assert placeholder_idx < inline_idx < edit_idx, (
        f"顺序应为 placeholder→inline→edit, 实际={call_log}"
    )


def test_adopt_skipped_state_applied_replies_and_audits(tmp_telemetry):
    """回归: 当 /adopt 调用时 suggestion.state 已是 applied (被 auto_detect
    或前一次 /adopt 标记过), 旧实现静默 return 不给任何反馈, 用户感知
    'adopt 没生效'. 修复: 即使 skip, 也要:
    1) reply_to_discussion 告诉用户'已采纳, 无需重复 /adopt'
    2) 把用户的 reason 写进 suggestion_actions audit
    3) 触发 re-improve
    """
    from reviewagent.commands.improve import ImproveCommand  # noqa: F401
    from reviewagent.commands.suggestion_actions import process_adopt
    from reviewagent.telemetry.store import get_store

    head_sha = "abc12345" * 5
    store = get_store()
    store.record_suggestion(
        project_id=34, mr_iid=164, note_id="already-applied-1",
        file_path="services/health_check.py", target_line=13,
        target_line_end=13,
        existing_code="from services.notify import *",
        improved_code="from services.notify import dispatch_email, lookup_recipient",
        header="禁止 wildcard import", label="potential bug",
        fingerprint="fpalready1", cohort_key="c1",
        severity_source="rule",
        head_sha=head_sha,
    )
    conn = sqlite3.connect(tmp_telemetry)
    conn.execute(
        "UPDATE suggestions SET head_sha=?, state='applied' WHERE note_id='already-applied-1'",
        (head_sha,),
    )
    conn.commit()
    conn.close()

    fake_gl = MagicMock()
    fake_gl.resolve_discussion.return_value = True
    fake_gl.reply_to_discussion.return_value = 9999

    # enqueue_improve 是 _maybe_enqueue_reimprove 内部从 reviewagent.workers.tasks
    # 延迟 import, patch 必须在原模块位置 (不是 suggestion_actions)
    # cooldown check 也要 patch 掉, 否则跨测试时 Redis 还在冷却
    with patch("reviewagent.commands.suggestion_actions.GitLabClient", return_value=fake_gl), \
         patch("reviewagent.workers.tasks.enqueue_improve", return_value="job-xyz") as mock_enqueue, \
         patch("reviewagent.webhook.locks.locks") as mock_locks:
        mock_locks.should_skip_cooldown.return_value = False
        result = process_adopt(
            project_id=34, mr_iid=164,
            suggestion_note_id="already-applied-1",
            actor_username="root",
            reason="fix",
        )

    # 1. 返回值带 re-improve job
    assert result["action"] == "adopt-skipped"
    assert "reimprove_job" in result, f"应触发 re-improve, 实际 {result}"
    # 2. reply_to_discussion 至少调用一次 (给用户反馈)
    assert fake_gl.reply_to_discussion.called, "应给用户 reply, 实际未调用"
    # 3. 用户的 reason 写进 audit
    conn = sqlite3.connect(tmp_telemetry)
    rows = conn.execute(
        "SELECT actor_username, action, reason, validation_status "
        "FROM suggestion_actions "
        "WHERE suggestion_note_id='already-applied-1' AND validation_status='already-applied'"
    ).fetchall()
    conn.close()
    assert len(rows) == 1, f"audit 应记一条 already-applied, 实际 {rows}"
    assert rows[0][0] == "root"
    assert rows[0][1] == "adopted"
    assert rows[0][2] == "fix"
    # 4. enqueue_improve 被调用
    assert mock_enqueue.called


def test_token_adoption_match_detects_with_block_change():
    """回归: 严格 strip 比对漏判的"格式不同但语义一致"场景, token fallback
    必须识别. 例: 建议 `with open(...) as f: f.read()`, 用户用了 with block
    (但变量名 f → fp, 多了一个变量引入), token 'fp' 出现在目标行附近.
    """
    from reviewagent.commands.suggestion_actions import _token_adoption_match

    posted = "def f():\n    return open(path).read()\n"
    # 用户用 with block 但变量名不同
    current = "def f():\n    with open(path) as fp:\n        return fp.read()\n"
    existing = "return open(path).read()"
    # 引入 token: "with" (keyword, 排除) + "fp" (新变量) → 只剩 {"fp"}
    # current 目标行 ±5 行内出现 "fp" → 算采纳
    assert _token_adoption_match(
        posted, current,
        line=2, line_end=2,
        improved_code="with open(path) as fp:\n    return fp.read()",
        existing_code=existing,
    )


def test_token_adoption_match_rejects_unrelated_change():
    """反例: 用户改了别的行 (target 行附近没出现 improved 的新 token) → 不算采纳."""
    from reviewagent.commands.suggestion_actions import _token_adoption_match

    posted = "def f():\n    return open(path).read()\n\ndef g():\n    pass\n"
    # 用户改了 g 函数, 没动 f
    current = "def f():\n    return open(path).read()\n\ndef g():\n    return 42\n"
    existing = "return open(path).read()"
    # 引入新 token: improved 中减去 existing 已有 = {"with", "as", "fp"} → 排除 keyword
    # 后 = {"fp"}. 目标行附近没出现 "fp" → 不算采纳
    assert not _token_adoption_match(
        posted, current,
        line=2, line_end=2,
        improved_code="with open(path) as fp:\n    return fp.read()",
        existing_code=existing,
    )


def test_token_adoption_match_empty_inputs():
    """边界: improved_code 为空 / current 为空 → 不算采纳."""
    from reviewagent.commands.suggestion_actions import _token_adoption_match

    assert not _token_adoption_match("a", "", line=1, line_end=1, improved_code="x")
    assert not _token_adoption_match("a", "b", line=1, line_end=1, improved_code="")
    assert not _token_adoption_match("a", "b", line=1, line_end=1, improved_code=None)
