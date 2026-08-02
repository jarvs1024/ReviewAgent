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
    # 3. resolve_discussion 也必须调用 (用户期望 /adopt 后 thread 自动关闭对勾)
    assert fake_gl.resolve_discussion.called, (
        "skip-state 路径应自动 resolve thread, 实际未调用 resolve_discussion"
    )
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


def test_different_rule_keys_not_deduped_at_close_lines(tmp_telemetry):
    """Regression: rule_keys 不重叠的建议即使 (file, line±2) 重叠也不应 dedup.

    场景: account_helpers.py L10 已发 SSD-RULE-NO-MUTABLE-DEFAULT,
    LLM 又识别出 L12 SSD-RULE-NO-LOG-EXC (snap_to_existing 调整到 12).
    修复前: line_at (L10±2 = 8~12) 命中 → 误 dedup → NO-LOG-EXC 漏发.
    修复后: rule_keys LIKE 不重叠 → 放行 → NO-LOG-EXC 正常发布.
    """
    from reviewagent.commands.improve import ImproveCommand
    from reviewagent.telemetry.store import get_store

    head_sha = "feedface" * 5
    s = get_store()
    # Pre-seed: NO-MUTABLE-DEFAULT at L10
    s.record_suggestion(
        project_id=34, mr_iid=166, note_id="seed-mut",
        file_path="services/account_helpers.py", target_line=10,
        target_line_end=10,
        existing_code="def format_record(rec, tenant_id=None, items=[]):",
        improved_code="def format_record(rec, tenant_id=None, items=None):",
        header="可变默认实参", label="potential bug",
        severity="high",
        rule_keys=["SSD-RULE-NO-MUTABLE-DEFAULT"],
        fingerprint="mutfp001", cohort_key="mutcohort",
        severity_source="rule",
        head_sha=head_sha,
    )
    conn = sqlite3.connect(tmp_telemetry)
    conn.execute(
        "UPDATE suggestions SET head_sha=?, state='open' WHERE note_id='seed-mut'",
        (head_sha,),
    )
    conn.commit()
    conn.close()

    # 新建议: NO-LOG-EXC at L12 (与 seed 不同规则)
    new_raw = {
        "file": "services/account_helpers.py",
        "line": 12,
        "new_line": 12,
        "header": "静默吞异常",
        "label": "potential bug",
        "rationale": "违反 SSD-RULE-NO-LOG-EXC: except Exception: pass 静默吞错",
        "severity": "high",
        "improved_code": "except Exception:\n    logging.exception(...)\n    raise",
        "existing_code": "except Exception:\n    pass",
        "rule_keys": ["SSD-RULE-NO-LOG-EXC"],
    }

    # 验证 dedup 查询不命中
    s2 = get_store()
    exists = s2.suggestion_exists_at_line(
        project_id=34, mr_iid=166,
        file_path="services/account_helpers.py",
        target_line=12, severity="high",
        head_sha=head_sha, line_tolerance=2,
        rule_keys="SSD-RULE-NO-LOG-EXC",
    )
    assert exists is False, "不同 rule_keys 不应被 line_at dedup 误杀"

    # 反向验证: 同 rule_keys 仍会 dedup
    exists_same = s2.suggestion_exists_at_line(
        project_id=34, mr_iid=166,
        file_path="services/account_helpers.py",
        target_line=12, severity="high",
        head_sha=head_sha, line_tolerance=2,
        rule_keys="SSD-RULE-NO-MUTABLE-DEFAULT",
    )
    assert exists_same is True, "同 rule_keys 应被 dedup"

    # 反向验证: 不传 rule_keys 时走旧 (file, line) 兜底
    exists_none = s2.suggestion_exists_at_line(
        project_id=34, mr_iid=166,
        file_path="services/account_helpers.py",
        target_line=12, severity="high",
        head_sha=head_sha, line_tolerance=2,
    )
    assert exists_none is True, "不传 rule_keys 应走旧 (file, line) 兜底"


def test_max_review_calls_limit(tmp_telemetry, monkeypatch):
    """Regression: 同一 MR 的 review 次数超 max_review_calls_per_mr 应被 skip.

    场景: max=3, MR 34/167 已记录 3 个 improve runs, 再次 push → 应被 skip.
    """
    import sqlite3
    from reviewagent.webhook.locks import locks

    # 触发 store init schema (fixture 已把 sqlite_path 切到 tmp_telemetry)
    from reviewagent.telemetry.store import get_store
    store = get_store()  # 建表
    # 用 store._conn() 而非 sqlite3.connect, 保证 store 的连接跟 tmp 一致
    with store._conn() as conn:
        for i in range(3):
            conn.execute(
                "INSERT INTO review_runs (project_id, mr_iid, command, triggered_by, status, started_at, finished_at) "
                "VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))",
                (34, 167, "improve", "test", "success"),
            )

    # max=3, 当前 3 → 应被 skip
    skip, current = locks.should_skip_max_review_calls(34, 167, ("describe", "improve"), max_calls=3)
    assert skip is True, f"应被 skip, got skip={skip} current={current}"
    assert current == 3

    # max=0 → 不限 (快速 return, 不查 DB, 所以 cur0=0)
    skip0, cur0 = locks.should_skip_max_review_calls(34, 167, ("describe", "improve"), max_calls=0)
    assert skip0 is False, "max=0 应永不限"

    # 降到 max=5 → 不超限
    skip5, cur5 = locks.should_skip_max_review_calls(34, 167, ("describe", "improve"), max_calls=5)
    assert skip5 is False
