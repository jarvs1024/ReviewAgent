"""review/publishing.py — 发布 / cohort / summary 迁移后的回归测试.

覆盖:
- `build_summary_v2` / `build_summary_placeholder` 与门面输出一致 (相同字节)
- `load_rule_actions` 读取 profile 固定路径 `rules/rule_actions.yaml`
- `publish` 的入口守卫 / drop 分支
- dedup / record 的规则键抽取 (仓库规则键 + promotion)
"""
from __future__ import annotations

import os
import tempfile
from unittest.mock import MagicMock

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


# ---------- build_summary_v2 ----------

def _summary_cmd(project_id=34, mr_iid=163):
    from reviewagent.commands.improve import ImproveCommand

    cmd = ImproveCommand.__new__(ImproveCommand)
    cmd.project_id = project_id
    cmd.mr_iid = mr_iid
    return cmd


def test_build_summary_v2_only_duplicate_skipped(tmp_telemetry):
    """全是 dedup skip → 说明性 summary, 与门面输出逐字节一致."""
    from reviewagent.review.publishing import build_summary_v2

    inline_skipped = [
        {"suggestion": {"file": "x.py", "start_line": 1}, "reason": "duplicate_at_line"},
        {"suggestion": {"file": "y.py", "start_line": 5}, "reason": "duplicate_fingerprint"},
    ]
    direct = build_summary_v2(
        project_id=34, mr_iid=163,
        inline_posted=[], inline_skipped=inline_skipped,
        total_agent_suggestions=2,
    )
    facade = _summary_cmd()._build_summary_v2([], inline_skipped, 2)
    assert direct == facade
    assert direct.startswith("## 改进总览 V")
    assert "已发过的 2 条跳过" in direct


def test_build_summary_v2_truly_empty(tmp_telemetry):
    from reviewagent.review.publishing import build_summary_v2

    direct = build_summary_v2(
        project_id=34, mr_iid=163,
        inline_posted=[], inline_skipped=[], total_agent_suggestions=0,
    )
    facade = _summary_cmd()._build_summary_v2([], [], 0)
    assert direct == facade
    assert "未发现新问题" in direct


def test_build_summary_v2_one_posted_item_l_line(tmp_telemetry):
    from reviewagent.review.publishing import build_summary_v2

    inline_posted = [{
        "note_id": "n1",
        "kind": "inline",
        "raw": {"file": "foo.py", "start_line": 10, "header": "可变默认参数", "severity": "high"},
        "normalised": {
            "file": "foo.py", "new_line": 10, "header": "可变默认参数",
            "severity": "high", "label": "potential bug",
            "rationale": "违反 SSD-RULE-NO-MUTABLE-DEFAULT: 第 10 行的 `x=[]` 是可变默认实参",
        },
    }]
    direct = build_summary_v2(
        project_id=34, mr_iid=163,
        inline_posted=inline_posted, inline_skipped=[],
        total_agent_suggestions=1,
    )
    facade = _summary_cmd()._build_summary_v2(inline_posted, [], 1)
    assert direct == facade
    assert "本次新发现 1 条建议" in direct
    assert "`foo.py` `L10`" in direct


# ---------- build_summary_placeholder ----------

def test_build_summary_placeholder_version_numbers(tmp_telemetry):
    from reviewagent.review.publishing import build_summary_placeholder
    from reviewagent.telemetry import events
    from reviewagent.telemetry.models import ReviewRun

    cmd = _summary_cmd(mr_iid=991)
    assert cmd._build_summary_placeholder([], [], 0) == "## 改进总览 V1\n\n_加载中…_"
    assert build_summary_placeholder(project_id=34, mr_iid=991) == "## 改进总览 V1\n\n_加载中…_"

    events.emit_run_started(ReviewRun(
        project_id=34, mr_iid=991, command="improve", triggered_by="test",
    ))
    events.emit_run_started(ReviewRun(
        project_id=34, mr_iid=991, command="improve", triggered_by="test",
    ))
    assert cmd._build_summary_placeholder([], [], 0) == "## 改进总览 V2\n\n_加载中…_"
    assert build_summary_placeholder(project_id=34, mr_iid=991) == "## 改进总览 V2\n\n_加载中…_"


# ---------- load_rule_actions ----------

def test_load_rule_actions_from_profile():
    from reviewagent.reporting.renderer import load_rule_actions
    from reviewagent.scenario.profile import load_profile

    actions = load_rule_actions(load_profile(None))
    # 仓库规则键不在场景包里登记（随被检仓库变化），只保留通用规则动作。
    # 规则包的全部 R-* + R-OTHER-IMPACT / R-OTHER 两个兜底键都要有动作。
    assert len(actions) == 26
    assert not any(key.endswith("-RULE-TYPEHINTS") for key in actions)
    assert actions["R-OTHER-IMPACT"] == "对跨文件函数签名变化在 MR 描述里点出 caller, 拉对应 owner review"
    assert "规则包" in actions["R-OTHER"]
    assert actions["R-CI"] == "对并行用例加临时目录 PID 隔离 / 串行锁, 修 flaky"


# ---------- publish ----------

def _stub_normalise(raw):
    return {
        "file": raw.get("file"),
        "new_line": raw.get("start_line"),
        "improved_code": raw.get("improved_code") or "",
        "header": raw.get("header") or "",
        "label": raw.get("label") or "",
        "rationale": raw.get("rationale") or "",
        "severity": raw.get("severity") or "medium",
        "body": "**body**",
    }


def _noop_callbacks(**overrides):
    from reviewagent.review.publishing import PublishCallbacks

    values = {
        "diff_line_map": lambda: {},
        "head_sha": lambda: "",
        "normalise": _stub_normalise,
        "score": lambda sug: 0,
        "validate": lambda **kw: {"action": "drop", "new_line": 0, "reason": "drop"},
        "detect_apply_risk": lambda **kw: ("ok", []),
        "build_summary_placeholder": lambda *a, **kw: "## 改进总览 V1\n\n_加载中…_",
        "build_summary_v2": lambda *a, **kw: "## 改进总览 V1\n\n✅ 本次未发现新问题.",
        "extract_guardrail_markers": lambda md: [],
        "pre_reconcile": lambda **kw: {},
        "publish_overview": MagicMock(return_value=None),
        "help_footer": "",
    }
    values.update(overrides)
    return PublishCallbacks(**values)


def test_publish_rejects_non_list_suggestions():
    from reviewagent.llm import OpencodeOutputError
    from reviewagent.review.publishing import PublishContext, publish

    ctx = PublishContext(project_id=34, mr_iid=1, gitlab=MagicMock())
    with pytest.raises(OpencodeOutputError):
        publish(
            ctx, {"summary_md": "", "suggestions": "not-a-list"},
            rule_set=None, profile=None, cfg=None, callbacks=_noop_callbacks(),
        )


def test_publish_drop_action_appends_skipped_without_posting_inline():
    from reviewagent.review.publishing import PublishContext, publish

    gitlab = MagicMock()
    gitlab.post_mr_comment.return_value = None
    ctx = PublishContext(project_id=34, mr_iid=2, gitlab=gitlab)
    raw = {"file": "x.py", "start_line": 3, "header": "h"}
    callbacks = _noop_callbacks(
        validate=lambda **kw: {"action": "drop", "new_line": kw["start_line"], "reason": "no valid line in file"},
    )
    result = publish(
        ctx, {"summary_md": "", "suggestions": [raw]},
        rule_set=None, profile=None, cfg=None, callbacks=callbacks,
    )
    assert result == {
        "top_comment_id": None,
        "suggestions_count": 1,
        "inline_posted": 0,
        "inline_skipped": 1,
        "rule_keys_cited": [],
        "suggestion_count": 0,
    }
    gitlab.post_mr_discussion.assert_not_called()
    gitlab.update_mr_comment.assert_not_called()


def test_publish_returns_posted_rule_keys_and_suggestion_count(tmp_telemetry):
    from reviewagent.review.publishing import PublishContext, publish

    rule_set = _real_rule_set()
    gitlab = MagicMock()
    gitlab.post_mr_comment.return_value = 7001
    gitlab.post_mr_discussion.return_value = "note-42"
    ctx = PublishContext(project_id=34, mr_iid=3, gitlab=gitlab)
    raw = {
        "file": "x.py", "start_line": 3, "header": "静默吞异常",
        "improved_code": "x = 1", "rationale": "违反 R-ERR",
    }
    callbacks = _noop_callbacks(
        score=lambda sug: 77,
        validate=lambda **kw: {
            "action": "post", "new_line": kw["start_line"],
            "reason": "ok", "normalised_code": "x = 1",
        },
    )
    result = publish(
        ctx, {"summary_md": "", "suggestions": [raw]},
        rule_set=rule_set, profile=None, cfg=None, callbacks=callbacks,
    )
    assert result["inline_posted"] == 1
    assert result["suggestion_count"] == 1
    assert result["rule_keys_cited"] == ["R-ERR"]

    from reviewagent.telemetry.store import get_store
    row = get_store().get_suggestion_by_note_id("note-42")
    assert row["score"] == 77
    assert row["severity_source"] == "rule"
    assert row["rule_keys"] == "R-ERR"


def test_publish_marks_severity_source_llm_without_rule_keys(tmp_telemetry):
    from reviewagent.review.publishing import PublishContext, publish

    rule_set = _real_rule_set()
    gitlab = MagicMock()
    gitlab.post_mr_comment.return_value = 7002
    gitlab.post_mr_discussion.return_value = "note-43"
    ctx = PublishContext(project_id=34, mr_iid=4, gitlab=gitlab)
    raw = {
        "file": "x.py", "start_line": 5, "header": "命名建议",
        "improved_code": "y = 2", "rationale": "变量名不够直观",
    }
    callbacks = _noop_callbacks(
        score=lambda sug: 41,
        validate=lambda **kw: {
            "action": "post", "new_line": kw["start_line"],
            "reason": "ok", "normalised_code": "y = 2",
        },
    )
    result = publish(
        ctx, {"summary_md": "", "suggestions": [raw]},
        rule_set=rule_set, profile=None, cfg=None, callbacks=callbacks,
    )
    assert result["rule_keys_cited"] == []

    from reviewagent.telemetry.store import get_store
    row = get_store().get_suggestion_by_note_id("note-43")
    assert row["score"] == 41
    assert row["severity_source"] == "llm"


# ---------- rule keys (dedup / record) ----------

def _real_rule_set():
    from reviewagent.scenario.profile import load_profile
    from reviewagent.scenario.rules.resolver import assemble_base

    return assemble_base(load_profile(None))


def test_dedup_rule_keys_uses_key_patterns_extract_only():
    from reviewagent.review.publishing import dedup_rule_keys

    rule_set = _real_rule_set()
    normalised = {"rationale": "违反 R-ERR, 同时 SSD-RULE-NO-BARE-PRINT", "header": "h"}
    keys = dedup_rule_keys({}, normalised, rule_set=rule_set)
    assert keys == "R-ERR,SSD-RULE-NO-BARE-PRINT"


def test_record_rule_keys_prefers_raw_and_appends_promotions():
    import dataclasses

    from reviewagent.review.publishing import record_rule_keys

    rule_set = dataclasses.replace(
        _real_rule_set(),
        promotions=({"to_key": "ACME-RULE-WILDCARD", "when_any": ["from foo import *", "通配符"]},),
    )
    normalised = {"rationale": "from foo import *", "header": "通配符 import"}
    raw = {"rule_keys": ["R-OTHER-IMPACT:wildcard_import"]}
    keys = record_rule_keys(raw, normalised, rule_set=rule_set)
    assert keys == ["R-OTHER-IMPACT:wildcard_import", "ACME-RULE-WILDCARD"]


def test_record_rule_keys_uses_normalize_when_raw_empty():
    from reviewagent.review.publishing import record_rule_keys

    rule_set = _real_rule_set()
    normalised = {"rationale": "违反 R-ERR", "header": "静默吞异常"}
    keys = record_rule_keys({"rule_keys": []}, normalised, rule_set=rule_set)
    assert keys == ["R-ERR"]


def test_dedup_rule_keys_ignores_non_list_rule_keys():
    from reviewagent.review.publishing import dedup_rule_keys

    rule_set = _real_rule_set()
    normalised = {"rationale": "", "header": ""}
    keys = dedup_rule_keys({"rule_keys": "R-ERR"}, normalised, rule_set=rule_set)
    assert keys == ""


def test_record_rule_keys_ignores_header_only_keys():
    from reviewagent.review.publishing import record_rule_keys

    rule_set = _real_rule_set()
    normalised = {"rationale": "", "header": "SSD-RULE-TYPEHINTS"}
    keys = record_rule_keys({}, normalised, rule_set=rule_set)
    assert keys == []


def test_record_rule_keys_promotes_from_header_only():
    """header 命中 promotion（rationale 为空）时仍追加目标键——提升由 profile 配置驱动."""
    import dataclasses

    from reviewagent.review.publishing import record_rule_keys

    rule_set = dataclasses.replace(
        _real_rule_set(),
        promotions=({"to_key": "ACME-RULE-WILDCARD", "when_any": ["wildcard"]},),
    )
    normalised = {"rationale": "", "header": "Wildcard import detected"}
    keys = record_rule_keys({}, normalised, rule_set=rule_set)
    assert keys == ["ACME-RULE-WILDCARD"]


def test_record_rule_keys_extracts_from_rationale_only():
    from reviewagent.review.publishing import record_rule_keys

    rule_set = _real_rule_set()
    normalised = {"rationale": "违反 R-ERR", "header": ""}
    keys = record_rule_keys({}, normalised, rule_set=rule_set)
    assert keys == ["R-ERR"]


def test_record_rule_keys_appends_promotion_without_duplicating():
    import dataclasses

    from reviewagent.review.publishing import record_rule_keys

    rule_set = dataclasses.replace(
        _real_rule_set(),
        promotions=({"to_key": "ACME-RULE-WILDCARD", "when_any": ["wildcard"]},),
    )
    normalised = {"rationale": "wildcard import detected", "header": ""}
    keys = record_rule_keys({"rule_keys": ["R-ERR"]}, normalised, rule_set=rule_set)
    assert keys == ["R-ERR", "ACME-RULE-WILDCARD"]

    already = record_rule_keys(
        {"rule_keys": ["ACME-RULE-WILDCARD"]}, normalised, rule_set=rule_set
    )
    assert already == ["ACME-RULE-WILDCARD"]
