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


def test_dedup_falls_back_to_rationale_rule_keys_when_raw_empty(tmp_telemetry):
    """Regression: dedup 查询时 raw.rule_keys 为空, 应从 normalised.rationale 抽取.

    MR 239 场景: other.py L4 (R-OTHER:magic_number, severity=medium)
    被 L2 (SSD-RULE-TYPEHINTS, severity=low) 在 line_tolerance=2 范围内
    命中 → skip_at_line → 资源泄漏建议丢失.
    修复: raw.rule_keys 为空时, 从 normalised.rationale 用 _RULE_REF_REGEX
    抽 rule_keys 用于 dedup 查询, 让 L4 的 R-OTHER:magic_number 不与
    L2 的 SSD-RULE-TYPEHINTS 误命中.
    """
    from reviewagent.commands.improve import ImproveCommand
    from reviewagent.telemetry.store import get_store

    head_sha = "abcdef01" * 5
    s = get_store()
    # Pre-seed: SSD-RULE-TYPEHINTS at L2 (其他规则, low)
    s.record_suggestion(
        project_id=34, mr_iid=239, note_id="seed-typehints",
        file_path="fixtures/qodercli_manual_20260806_232415/other.py",
        target_line=2, target_line_end=2,
        existing_code="from __future__ import annotations",
        improved_code="from __future__ import annotations\nfrom collections.abc import Callable",
        header="类型注解", label="code quality",
        severity="low",
        rule_keys=["SSD-RULE-TYPEHINTS"],
        fingerprint="tyhintfp01", cohort_key="tyhintco01",
        severity_source="rule",
        head_sha=head_sha,
    )
    conn = sqlite3.connect(tmp_telemetry)
    conn.execute(
        "UPDATE suggestions SET head_sha=?, state='open' WHERE note_id='seed-typehints'",
        (head_sha,),
    )
    conn.commit()
    conn.close()

    # 新建议: L4 R-OTHER:magic_number (medium), raw.rule_keys 字段缺失
    # rationale 里包含 "R-OTHER:magic_number" → 应被抽出
    new_raw = {
        "file": "fixtures/qodercli_manual_20260806_232415/other.py",
        "line": 4,
        "header": "常量提取",
        "label": "code quality",
        "rationale": "R-OTHER:magic_number: 定义具名常量 POLL_INTERVAL_S = 0.619, "
                     "消除无解释的硬编码间隔",
        "severity": "medium",
        "improved_code": "POLL_INTERVAL_S = 0.619\n\ndef poll_ready(check, attempts: int = 4):",
        "existing_code": "def poll_ready(check, attempts: int = 4):",
        # 注意: 故意不放 rule_keys, 模拟 LLM 不输出 rule_keys 字段
    }

    cmd = ImproveCommand.__new__(ImproveCommand)
    cmd.project_id = 34
    cmd.mr_iid = 239
    cmd.gitlab = MagicMock()
    cmd.gitlab.post_mr_discussion.return_value = "note-new"
    cmd.HELP_TEXT_FOOTER = ""

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
    cmd._read_file_lines = lambda *a, **kw: []

    with patch("reviewagent.telemetry.store.get_store", return_value=s):
        result = cmd._publish({"summary_md": "", "suggestions": [new_raw]})

    # 修复后: dedup 查询从 rationale 抽到 R-OTHER:magic_number,
    # 与 seed 的 SSD-RULE-TYPEHINTS 不重叠 → 不命中 → 应正常发布
    assert result["inline_posted"] == 1, (
        f"修复后, 从 rationale 抽到 R-OTHER:magic_number 不应被 SSD-RULE-TYPEHINTS 命中 dedup. "
        f"实际 posted={result['inline_posted']} skipped={result['inline_skipped']}"
    )
    assert result["inline_skipped"] == 0
    cmd.gitlab.post_mr_discussion.assert_called_once()


def test_overview_summary_format_with_full_data(tmp_telemetry):
    """_build_overview_summary 应生成固定 header + 单表 + 元信息行 (方案 A).

    验证:
    - header 固定 `## 检视汇总` (无 V{N})
    - 单表 5 列: 严重度 × {待处理/已采纳/已忽略/合计}
    - 末行 加粗"总计"
    - 底部单行元信息: 最后新增 + CST 时间 + HEAD sha
    """
    from reviewagent.commands.improve import ImproveCommand
    from reviewagent.telemetry.store import get_store

    head_sha = "01234567" * 5
    s = get_store()

    # 预置: 1 open (HIGH) + 1 applied (MEDIUM) + 1 dismissed (LOW)
    for i, (state, sev, line, header, rule) in enumerate([
        ("open", "high", 10, "可变默认参数", "SSD-RULE-NO-MUTABLE-DEFAULT"),
        ("applied", "medium", 15, "类型注解", "SSD-RULE-TYPEHINTS"),
        ("dismissed", "low", 20, "注释修正", "R-OTHER:stale_comment"),
    ]):
        s.record_suggestion(
            project_id=34, mr_iid=300,
            note_id=f"seed-{i}",
            file_path=f"services/sample_{i}.py",
            target_line=line, target_line_end=line,
            existing_code="x = 1", improved_code="x = 2",
            header=header, label="code quality",
            severity=sev, head_sha=head_sha,
            rule_keys=[rule],
            fingerprint=f"fp{i}", cohort_key=f"co{i}",
            severity_source="rule",
        )
        conn = sqlite3.connect(tmp_telemetry)
        conn.execute(
            "UPDATE suggestions SET state=? WHERE note_id=?",
            (state, f"seed-{i}"),
        )
        conn.commit()
        conn.close()

    cmd = ImproveCommand.__new__(ImproveCommand)
    cmd.project_id = 34
    cmd.mr_iid = 300

    # 模拟本次新增 2 条 (inline_posted)
    inline_posted = [
        {"note_id": "new-1", "raw": {"severity": "high"}, "normalised": {"severity": "high"}, "kind": "inline"},
        {"note_id": "new-2", "raw": {"severity": "medium"}, "normalised": {"severity": "medium"}, "kind": "inline"},
    ]
    out = cmd._build_overview_summary(
        inline_posted, inline_skipped=[], total_agent_suggestions=5,
        head_sha=head_sha,
    )

    # 1. header 固定不带 V{N}
    assert "## 检视汇总" in out, f"应有固定 header '## 检视汇总': {out!r}"
    assert "V0" not in out and "V1" not in out and "V2" not in out, \
        f"不应有 V{{N}} 版本号: {out!r}"

    # 2. 单表 6 列 (严重度 × 状态 × 合计)
    assert "| 严重度 | ⏳ 待处理 | ✅ 已采纳 | ❌ 已忽略 | 🔒 已关闭（未分类） | 合计 |" in out
    # HIGH: 1 open / 0 applied / 0 dismissed / 0 resolved / 1 sum
    assert "| 🔴 HIGH | 1 | 0 | 0 | 0 | 1 |" in out
    # MEDIUM: 0 open / 1 applied / 0 dismissed / 0 resolved / 1 sum
    assert "| 🟡 MEDIUM | 0 | 1 | 0 | 0 | 1 |" in out
    # LOW: 0 open / 0 applied / 1 dismissed / 0 resolved / 1 sum
    assert "| 🟢 LOW | 0 | 0 | 1 | 0 | 1 |" in out

    # 3. 加粗总计行: 1+0+0 / 0+1+0 / 0+0+1 / 3 total
    assert "| **总计** | **1** | **1** | **1** | **0** | **3** |" in out

    # 4. 单行元信息: 最后新增 + CST 时间 + HEAD sha 短码
    assert "🆕 **最后新增 2 条**" in out
    assert "CST" in out, f"时间应为 CST (本地时间), 避免与 UTC 混淆: {out!r}"
    assert "0123456" in out, f"应有 head_sha 短码 (前7位): {out!r}"
    assert "已采纳" in out and "已忽略" in out and "已关闭（未分类）" in out


def test_overview_summary_works_with_empty_state(tmp_telemetry):
    """MR 第一次检视前 telemetry 为空, 表格应仍可生成."""
    from reviewagent.commands.improve import ImproveCommand

    cmd = ImproveCommand.__new__(ImproveCommand)
    cmd.project_id = 34
    cmd.mr_iid = 999  # 没数据
    out = cmd._build_overview_summary(
        inline_posted=[], inline_skipped=[], total_agent_suggestions=0,
        head_sha="abcdef00",
    )

    assert "## 检视汇总" in out
    # 单表 + 总计行 (新格式: 严重度 × 状态 × 合计)
    assert "| 🔴 HIGH | 0 | 0 | 0 | 0 |" in out
    assert "| 🟡 MEDIUM | 0 | 0 | 0 | 0 |" in out
    assert "| 🟢 LOW | 0 | 0 | 0 | 0 |" in out
    assert "| **总计** | **0** | **0** | **0** | **0** |" in out
    # 没 inline_posted 时显示 "最后新增 0 条"
    assert "🆕 **最后新增 0 条**" in out
    # CST 时间与 head_sha 短码
    assert "CST" in out
    assert "abcdef0" in out


# ---------- A: fingerprint 算法 (file:line:header_normalized) ----------

def test_fingerprint_stable_for_same_position_and_header(tmp_telemetry):
    """A: 同 file:line:header 永远同 fp, 即使 existing_code 行数不同.

    之前 fingerprint = sha256(existing_code), 同一位置 existing_code 边界
    略变 (LLM 给 1 行 vs 整段) → fp 不同 → dedup 漏.
    修复后: (file:line:header_normalized) 三元组永远稳定.
    """
    from reviewagent.commands.improve import _suggestion_fingerprint

    # 1. 同一 (file, line, header) → fp 应一致
    fp_a = _suggestion_fingerprint("foo.py", 10, "docstring")
    fp_b = _suggestion_fingerprint("foo.py", 10, "docstring")
    assert fp_a == fp_b
    # 2. 不同 header → fp 不同 (说明同位置不同类问题仍可独立 dedup)
    fp_c = _suggestion_fingerprint("foo.py", 10, "\u7c7b\u578b\u63d0\u793a")
    assert fp_a != fp_c
    # 3. 不同 line → fp 不同
    fp_d = _suggestion_fingerprint("foo.py", 11, "docstring")
    assert fp_a != fp_d
    # 4. 同一三元组大小写不敏感 (header normalize lower)
    fp_upper = _suggestion_fingerprint("foo.py", 10, "DOCSTRING")
    fp_lower = _suggestion_fingerprint("foo.py", 10, "docstring")
    assert fp_upper == fp_lower, "header \u5927\u5c0f\u5199\u5dee\u5f02\u4e0d\u5e94\u5f71\u54cd fingerprint"
    # 5. header 前后空格不敏感
    fp_ws = _suggestion_fingerprint("foo.py", 10, "  docstring  ")
    assert fp_ws == fp_a, "header \u524d\u540e\u7a7a\u683c\u4e0d\u5e94\u5f71\u54cd fingerprint"
    # 6. fp 长度固定 24
    assert len(fp_a) == 24


def test_fingerprint_reused_via_record_and_dedup_check(tmp_telemetry):
    """A: dedup_check 与 record_suggestion 用同一 helper 算 fp.

    Why: improve.py 两处 (dedup_check, record_suggestion) 都调 _suggestion_fingerprint,
    保证同一位置同类问题第二轮 improve 时被 dedup 拦下. 此测试模拟 V1 record + V2 dedup
    两个调用, 验证 fp 一致.
    """
    from reviewagent.commands.improve import _suggestion_fingerprint
    from reviewagent.telemetry.store import get_store

    s = get_store()
    fp = _suggestion_fingerprint("foo.py", 10, "docstring")
    # V1: record (用 helper 算的 fp)
    s.record_suggestion(
        project_id=34, mr_iid=200, note_id="fp-test",
        file_path="foo.py", target_line=10, target_line_end=10,
        existing_code="def f():",
        improved_code="def f(): docstring",
        header="docstring", label="code quality",
        severity="low",
        rule_keys=["SSD-RULE-DOCSTRING-REQUIRED"],
        fingerprint=fp, cohort_key="ck",
        severity_source="rule",
        head_sha="feedface" * 5,
    )
    # V2: 调 dedup_check 应命中 (同 (file, line, header))
    assert s.suggestion_exists_by_fingerprint(34, 200, fp), (
        f"record + dedup_check 同 helper 还不命中: fp={fp}"
    )
    # 不同 header → fp 不同 → 不命中 (同位置不同类问题仍可独立 dedup)
    fp_diff = _suggestion_fingerprint("foo.py", 10, "typehints")
    assert not s.suggestion_exists_by_fingerprint(34, 200, fp_diff), (
        f"不同 header 不应被误命中: fp_diff={fp_diff}"
    )
def test_dedup_at_line_hits_applied_state(tmp_telemetry):
    """B: 已 applied 的位置再次识别同规则 → dedup 命中 (跳过).

    场景: V1 L22 已 applied print→logger+docstring, V6 LLM 又识别 docstring.
    修复前: state='open' 过滤 → V1 那条已 applied 不在 open 集合 → 放行 → 重复发.
    修复后: state IN ('open', 'applied', 'dismissed') → 命中 → 跳过.
    """
    from reviewagent.telemetry.store import get_store
    head_sha = "deadbeef" * 5
    s = get_store()
    s.record_suggestion(
        project_id=34, mr_iid=201, note_id="applied-seed",
        file_path="audit/foo.py", target_line=22, target_line_end=22,
        existing_code="def f():", improved_code="def f():\n    logger.info()",
        header="print\u2192logger + docstring", label="code quality",
        severity="medium", rule_keys=["R-LOG"],
        fingerprint="seed_fp", cohort_key="seed_ck",
        severity_source="rule", head_sha=head_sha,
    )
    # 标记为 applied (模拟用户已采纳)
    conn_path = tmp_telemetry
    import sqlite3
    conn = sqlite3.connect(conn_path)
    conn.execute(
        "UPDATE suggestions SET state='applied' WHERE note_id='applied-seed'"
    )
    conn.commit()
    conn.close()

    # 模拟 V6 LLM 又识别 L22 同位置 docstring, 传同 head_sha
    exists = s.suggestion_exists_at_line(
        project_id=34, mr_iid=201,
        file_path="audit/foo.py", target_line=22, severity="medium",
        head_sha=head_sha, line_tolerance=2,
        rule_keys="R-LOG",
    )
    assert exists is True, (
        f"\u5df2 applied \u7684\u4f4d\u7f6e\u518d\u6b21\u8bc6\u522b\u5e94\u88ab dedup \u547d\u4e2d, \u800c\u4e0d\u662f\u6f0f\u53d1: {exists!r}"
    )


def test_dedup_at_line_hits_dismissed_state(tmp_telemetry):
    """B: 已 dismissed 的位置再次识别 → dedup 命中 (用户已拒绝, 不应骚扰)."""
    from reviewagent.telemetry.store import get_store
    head_sha = "abcdef00" * 5
    s = get_store()
    s.record_suggestion(
        project_id=34, mr_iid=202, note_id="dismissed-seed",
        file_path="bar.py", target_line=5, target_line_end=5,
        existing_code="x = 1", improved_code="x = 2",
        header="magic number", label="code quality",
        severity="low", rule_keys=["R-CONST"],
        fingerprint="d_fp", cohort_key="d_ck",
        severity_source="rule", head_sha=head_sha,
    )
    import sqlite3
    conn = sqlite3.connect(tmp_telemetry)
    conn.execute(
        "UPDATE suggestions SET state='dismissed' WHERE note_id='dismissed-seed'"
    )
    conn.commit()
    conn.close()

    exists = s.suggestion_exists_at_line(
        project_id=34, mr_iid=202,
        file_path="bar.py", target_line=5, severity="low",
        head_sha=head_sha, line_tolerance=2,
        rule_keys="R-CONST",
    )
    assert exists is True, "\u5df2 dismissed \u7684\u4f4d\u7f6e\u518d\u6b21\u8bc6\u522b\u5e94\u88ab dedup \u547d\u4e2d"


def test_dedup_at_line_hits_resolved_state(tmp_telemetry):
    """B+: state=resolved (用户手动点 GitLab 「解决主题」) → dedup 命中.

    Why: 用户主动 close thread 后, 不应再被同位置建议骚扰.
    之前 resolved 放行 (担心 force-push 后需重新检视), 但这个职责已由
    supersede_stale_open_suggestions (force-push 时 head_sha 变化把 open
    标 superseded) 独立覆盖, resolved 放行只会让随手 close 的用户被重复推.
    """
    from reviewagent.telemetry.store import get_store
    head_sha = "12345678" * 5
    s = get_store()
    s.record_suggestion(
        project_id=34, mr_iid=203, note_id="resolved-seed",
        file_path="baz.py", target_line=30, target_line_end=30,
        existing_code="", improved_code="",
        header="bare open", label="potential bug",
        severity="high", rule_keys=["SSD-RULE-RESOURCE-CONTEXT-MANAGER"],
        fingerprint="r_fp", cohort_key="r_ck",
        severity_source="rule", head_sha=head_sha,
    )
    import sqlite3
    conn = sqlite3.connect(tmp_telemetry)
    conn.execute(
        "UPDATE suggestions SET state='resolved' WHERE note_id='resolved-seed'"
    )
    conn.commit()
    conn.close()

    exists = s.suggestion_exists_at_line(
        project_id=34, mr_iid=203,
        file_path="baz.py", target_line=30, severity="high",
        head_sha=head_sha, line_tolerance=2,
        rule_keys="SSD-RULE-RESOURCE-CONTEXT-MANAGER",
    )
    assert exists is True, (
        f"resolved (用户主动 close thread) 应被 dedup 命中, 避免重复骚扰: got {exists!r}"
    )


def test_dedup_at_line_misses_superseded_state(tmp_telemetry):
    """B+: state=superseded (force-push 后由 supersede_stale_open_suggestions 设置) → 放行.

    Why: superseded = 该 suggestion 因 head_sha 变化已过时, force-push 后允许
    LLM 重新识别该位置. 这是 force-push 重新检视的预期行为, 不能误 dedup.
    """
    from reviewagent.telemetry.store import get_store
    head_sha = "87654321" * 5
    s = get_store()
    s.record_suggestion(
        project_id=34, mr_iid=204, note_id="superseded-seed",
        file_path="qux.py", target_line=42, target_line_end=42,
        existing_code="", improved_code="",
        header="x", label="code quality",
        severity="low", rule_keys=["R-CONST"],
        fingerprint="s_fp", cohort_key="s_ck",
        severity_source="rule", head_sha=head_sha,
    )
    import sqlite3
    conn = sqlite3.connect(tmp_telemetry)
    conn.execute(
        "UPDATE suggestions SET state='superseded' WHERE note_id='superseded-seed'"
    )
    conn.commit()
    conn.close()

    exists = s.suggestion_exists_at_line(
        project_id=34, mr_iid=204,
        file_path="qux.py", target_line=42, severity="low",
        head_sha=head_sha, line_tolerance=2,
        rule_keys="R-CONST",
    )
    assert exists is False, (
        f"superseded (force-push 后过时) 应放行, 允许 force-push 后重新检视: got {exists!r}"
    )