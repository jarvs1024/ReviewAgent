"""Task 18: 发布幂等 / 崩溃窗口 / cohort 代际（spec §7.7.3 / §7.7.5）.

* 发布成功且 telemetry 落库成功 → 下一轮同 fingerprint 被 dedup 拦截（只发一次）；
* 「GitLab 已发帖、telemetry 落库失败」→ 本轮不中断（仍发帖一次，记
  `improve.record_suggestion failed`），下一轮会重复一次：这是 spec 明确接受的
  残余窗口（禁止「先写库再发帖」换取表面幂等）；
* cohort 复现：旧代 applied 且当前 head 的 existing_code 仍在 → supersede 旧记录，
  新记录 generation+1，`list_latest_by_cohort` 只返回新代。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from loguru import logger as loguru_logger

from reviewagent.review.publishing import (
    PublishCallbacks,
    PublishContext,
    _handle_cohort_reoccurrence,
    _suggestion_fingerprint,
    publish,
)
from reviewagent.telemetry.store import Store


@pytest.fixture
def logs():
    """捕获 loguru 消息（不依赖 caplog）."""
    messages: list[str] = []
    handler_id = loguru_logger.add(
        lambda message: messages.append(str(message).rstrip("\n")),
        level="INFO",
        format="{message}",
    )
    try:
        yield messages
    finally:
        loguru_logger.remove(handler_id)


@pytest.fixture
def store(tmp_path):
    """临时 telemetry Store 注入模块级单例（teardown 置 None）."""
    from reviewagent.telemetry import store as store_mod

    instance = Store(tmp_path / "telemetry.db")
    store_mod._store = instance
    try:
        yield instance
    finally:
        store_mod._store = None


@pytest.fixture
def rule_set():
    from reviewagent.scenario.profile import load_profile
    from reviewagent.scenario.rules.resolver import assemble_base

    return assemble_base(load_profile(None))


def _normalise(raw: dict) -> dict:
    return {
        "file": raw["file"],
        "new_line": raw["start_line"],
        "improved_code": raw.get("improved_code") or "",
        "header": raw.get("header") or "",
        "label": raw.get("label") or "",
        "rationale": raw.get("rationale") or "",
        "severity": raw.get("severity") or "medium",
        "body": "**[HIGH]** **可变默认参数** — potential bug\n\n违反规则",
    }


def _callbacks(**overrides) -> PublishCallbacks:
    values = {
        "diff_line_map": lambda: {},
        "head_sha": lambda: "head-1",
        "normalise": _normalise,
        "score": lambda sug: 0,
        "validate": lambda **kw: {
            "action": "post",
            "new_line": kw["start_line"],
            "reason": "ok",
        },
        "detect_apply_risk": lambda **kw: ("ok", []),
        "build_summary_placeholder": lambda *args, **kw: "## 改进总览 V1\n\n_加载中…_",
        "build_summary_v2": lambda *args, **kw: "## 改进总览 V1\n\n✅ 本次未发现新问题.",
        "extract_guardrail_markers": lambda md: [],
        "pre_reconcile": lambda **kw: {},
        "publish_overview": MagicMock(return_value=None),
        "help_footer": "",
    }
    values.update(overrides)
    return PublishCallbacks(**values)


def _gitlab() -> MagicMock:
    gitlab = MagicMock()
    gitlab.post_mr_discussion.return_value = "note-1"
    gitlab.post_mr_comment.return_value = 1
    return gitlab


_SUGGESTION = {
    "file": "pkg/a.py",
    "start_line": 12,
    "existing_code": "    x = []",
    "improved_code": "    x = None",
    "header": "可变默认参数",
    "label": "potential bug",
    "rationale": "违反 SSD-RULE-NO-MUTABLE-DEFAULT: 第 12 行的 `x=[]` 是可变默认实参",
    "severity": "high",
}

_MERGED = {"summary_md": "", "suggestions": [_SUGGESTION]}


# ---------------------------------------------------------------------------
# Step 3a: 落库成功后，下一轮同 suggestion / 同 head_sha 被 dedup
# ---------------------------------------------------------------------------


def test_second_publish_same_suggestion_is_deduped(store, rule_set, monkeypatch):
    """落库成功后，下一轮同 suggestion / 同 head_sha 由 fingerprint 维度拦截.

    屏蔽 line 维度（`suggestion_exists_at_line` 恒 False），让第二轮只能靠
    `duplicate_fingerprint` 拦截 —— 证明落库的 fingerprint 值本身有效，而不是
    被 at-line 兜底掩盖.
    """
    gitlab = _gitlab()
    ctx = PublishContext(project_id=34, mr_iid=7, gitlab=gitlab)
    captured: dict = {}

    def _capture_summary(posted, skipped, total):
        captured["posted"] = posted
        captured["skipped"] = skipped
        return "## 改进总览 V1\n\n_ok_"

    callbacks = _callbacks(build_summary_v2=_capture_summary)
    monkeypatch.setattr(
        Store, "suggestion_exists_at_line", lambda *_args, **_kwargs: False
    )

    first = publish(
        ctx, _MERGED, rule_set=rule_set, profile=None, cfg=None, callbacks=callbacks
    )
    assert first["inline_posted"] == 1
    assert gitlab.post_mr_discussion.call_count == 1

    rows = store.list_suggestions(project_id=34, mr_iid=7)
    assert [row["note_id"] for row in rows] == ["note-1"]
    expected_fingerprint = _suggestion_fingerprint(
        _SUGGESTION["file"], _SUGGESTION["start_line"], _SUGGESTION["header"]
    )
    assert rows[0]["fingerprint"] == expected_fingerprint
    assert (
        store.suggestion_exists_by_fingerprint(
            34, 7, expected_fingerprint, head_sha="head-1"
        )
        is True
    )

    second = publish(
        ctx, _MERGED, rule_set=rule_set, profile=None, cfg=None, callbacks=callbacks
    )
    assert second["inline_posted"] == 0
    assert second["inline_skipped"] == 1
    assert gitlab.post_mr_discussion.call_count == 1, "同 head_sha 不得重复发帖"
    assert captured["posted"] == []
    # line 维度已被屏蔽 → 只有 fingerprint 命中才会 skip，理由必须是 duplicate_fingerprint
    assert captured["skipped"][0]["reason"] == "duplicate_fingerprint"


# ---------------------------------------------------------------------------
# Step 3b: 崩溃窗口（残余）— 发帖成功但 record_suggestion 失败
# ---------------------------------------------------------------------------


def test_residual_window_record_failure_duplicates_once(store, rule_set, logs, monkeypatch):
    """落库失败 → 本轮仍发帖、下一轮重复一次（spec §7.7.3 已知残余窗口）."""
    gitlab = _gitlab()
    ctx = PublishContext(project_id=34, mr_iid=7, gitlab=gitlab)
    callbacks = _callbacks()

    real_record = Store.record_suggestion

    def _boom(self, **_kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(Store, "record_suggestion", _boom)

    first = publish(
        ctx, _MERGED, rule_set=rule_set, profile=None, cfg=None, callbacks=callbacks
    )
    # 发帖成功：telemetry 失败不得回滚已发布内容 / 不得抛出
    assert first["inline_posted"] == 1
    assert gitlab.post_mr_discussion.call_count == 1
    assert any("improve.record_suggestion failed" in message for message in logs)
    assert store.list_suggestions(project_id=34, mr_iid=7) == []

    # 恢复落库后同 suggestion 再发布：因上一轮未落库，dedup 未命中 → 再发一次
    monkeypatch.setattr(Store, "record_suggestion", real_record)
    second = publish(
        ctx, _MERGED, rule_set=rule_set, profile=None, cfg=None, callbacks=callbacks
    )
    assert second["inline_posted"] == 1
    assert gitlab.post_mr_discussion.call_count == 2, "残余窗口：下一轮允许重复一次"
    assert len(store.list_suggestions(project_id=34, mr_iid=7)) == 1


# ---------------------------------------------------------------------------
# Step 3c: cohort 代际（supersede 旧代 + generation+1）
# ---------------------------------------------------------------------------


def test_cohort_reoccurrence_supersedes_old_generation(store):
    cohort_key = "cohort-abc123"
    common = {
        "project_id": 34,
        "mr_iid": 7,
        "file_path": "pkg/a.py",
        "target_line": 12,
        "existing_code": "x = []",
        "improved_code": "x = None",
        "header": "可变默认参数",
        "severity": "high",
        "cohort_key": cohort_key,
    }
    store.record_suggestion(note_id="old-note", head_sha="head-0", **common)
    assert store.update_suggestion_state(
        "old-note", "applied", actor_username="alice", adoption_source="ui_apply"
    ) is True
    store.record_suggestion(note_id="new-note", head_sha="head-1", **common)

    gitlab = MagicMock()
    gitlab.get_file_at_sha.return_value = "def build():\n    x = []\n    return x\n"

    _handle_cohort_reoccurrence(
        store=store,
        project_id=34,
        mr_iid=7,
        cohort_key=cohort_key,
        new_note_id="new-note",
        file_path="pkg/a.py",
        target_line=12,
        target_line_end=12,
        existing="x = []",
        head_sha="head-1",
        gitlab=gitlab,
    )

    old = store.get_suggestion_by_note_id("old-note")
    new = store.get_suggestion_by_note_id("new-note")
    assert old["state"] == "superseded"
    assert new["cohort_generation"] == 2
    assert new["supersedes_note_id"] is None  # 反向链写在旧记录上
    assert old["supersedes_note_id"] == "new-note"

    latest = store.list_latest_by_cohort(project_id=34, mr_iid=7)
    assert [row["note_id"] for row in latest] == ["new-note"]
