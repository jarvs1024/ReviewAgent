"""建议「可 Apply 性」判定 — 区分 replace / delete / text_only 三种语义.

真实业务回归 (p31 / MR !2 / note_1095 / note_1096):
  模型对一条「无法给出可 Apply 代码」的发现输出了 `improved_code=""` + `existing_code` 非空,
  Python 端把空 `improved_code` 一律当成「删除整段」→ 发布成空的 ```suggestion:-0+0``` 块,
  GitLab 渲染出 **Apply suggestion** 按钮.
  reviewer 点一下就会把 `state = gc_ob.gc_get_status()` 这行**有效代码**删掉 —— 但模型
  根本没打算删它, 只是无法给出可直接 Apply 的改法.

覆盖:
* `normalise_suggestion` 对三种语义产出正确的正文 (有无 suggestion 块)
* `normalise_suggestion` 不再因 improved/existing 双空而整条丢弃 (信息丢失)
* `publish` 端到端: text_only 不出现 Apply 按钮, delete 保留 Apply 且带显式删除警告
"""
from __future__ import annotations

import os
import pathlib
import tempfile
from unittest.mock import MagicMock

import pytest

from reviewagent.review.policies.python import normalise_suggestion


@pytest.fixture()
def tmp_telemetry(monkeypatch):
    """隔离 telemetry 库 — 避免 dedup 命中上一条用例的落库记录."""
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


# ---------- 真实业务样本 (取自 p31 MR !2 的两条 inline 建议) ----------

# note_1096 — 「无法给出可直接 Apply 的代码」: 模型只想要文字说明
TEXT_ONLY_RAW = {
    "file": "testcase/test_gc/normal_gc/test_gc_status_query_hima3361_13008.py",
    "start_line": 14,
    "header": "caller 参数不匹配",
    "label": "cross-file impact",
    "severity": "high",
    "existing_code": "    state = gc_ob.gc_get_status()",
    "improved_code": "",
    "rationale": (
        "R-OTHER-IMPACT:caller_param — GcOperation.gc_get_status 在 NVMe VU 层未实现"
        "（testlib/lib_baw/nvme_cli.py:1305 的 GcVu 只有 show_blk/gc_get_thr/gc_set_thr），"
        "而无参调用会抛 TypeError。建议与同目录 test_normal_gc_hima3361_5516.py:18-21 的用法对齐，"
        "或在 NVMe VU 层补齐 gc_get_status 实现后再调用。"
        "因缺少可确定的 blk_id 且需跨文件补实现，不提供可直接 Apply 的代码。"
    ),
}

# note_1095 — 「建议直接删除该 import」: 删代码**就是**这条建议本身
DELETE_RAW = {
    "file": "testlib/lib_caw/serial3361.py",
    "start_line": 7,
    "header": "移除反向 import",
    "label": "cross-file impact",
    "severity": "high",
    "existing_code": "from testlib.lib_baw.gc_operation import GcOperation",
    "improved_code": "",
    "rationale": (
        "违反 `SSD-RULE-LIB-LAYER-ISOLATION`：第 7 行反向 import `testlib/lib_baw/gc_operation.py`。"
        "另外本文件全文未引用 `GcOperation`，属死导入（R-OTHER:dead_code）。建议直接删除该 import；"
        "若确需 GC 能力，应由 BAW/BMAW 或组件层组合 `Serial3361`。"
    ),
}

# 正常可 Apply 的改写 (replace 语义)
REPLACE_RAW = {
    "file": "testlib/lib_baw/gc_status_query.py",
    "start_line": 15,
    "header": "断言补充失败信息",
    "label": "code quality",
    "severity": "medium",
    "existing_code": "    assert state == 0",
    "improved_code": '    assert state == 0, f"GC 状态异常，期望 0，实际: {state}"',
    "rationale": "R-ASSERT：assert 缺少错误描述，失败时无法从报告定位实际返回值。",
}


# ---------- normalise_suggestion ----------

def test_text_only_suggestion_body_has_no_suggestion_block():
    """无法给出可 Apply 代码 → 正文不得含 ```suggestion 块 (否则 GitLab 会渲染 Apply 按钮)."""
    out = normalise_suggestion(dict(TEXT_ONLY_RAW))
    assert "```suggestion" not in out["body"], out["body"]
    # 结论仍要完整保留给 reviewer 看
    assert "caller 参数不匹配" in out["body"]
    assert "不提供可直接 Apply 的代码" in out["body"]


def test_delete_suggestion_body_keeps_suggestion_block():
    """真删除建议 → 仍需保留 Apply 按钮 (删除本身就是这条建议的内容)."""
    out = normalise_suggestion(dict(DELETE_RAW))
    assert "```suggestion:-0+0" in out["body"], out["body"]


def test_delete_suggestion_body_warns_apply_will_delete_code():
    """删除型建议必须在正文显式警告: 点 Apply 会删掉代码行."""
    out = normalise_suggestion(dict(DELETE_RAW))
    assert "删除" in out["body"]
    assert "Apply" in out["body"]


def test_replace_suggestion_body_unchanged_shape():
    """正常改写建议 → 仍有 suggestion 块, 且内容是改进后的代码."""
    out = normalise_suggestion(dict(REPLACE_RAW))
    assert "```suggestion:-0+0" in out["body"], out["body"]
    assert "f\"GC 状态异常" in out["body"]


def test_both_empty_does_not_raise_and_keeps_finding():
    """improved/existing 双空 (提示词要求的「只写 rationale」形态) 不得整条丢弃.

    提示词契约 improve.md:180-183 要求这种情况「让 Python 端降级为普通评论」,
    实际实现却直接 raise → 该发现被静默吞掉, reviewer 完全看不到.
    """
    out = normalise_suggestion({
        "file": "testcase/test_gc/x.py",
        "start_line": 7,
        "header": "缺少 docstring",
        "label": "code quality",
        "severity": "high",
        "existing_code": "",
        "improved_code": "",
        "rationale": "仓库规则键 SSD-RULE-CASE-DESCRIPTION: L14 不在 VALID NEW LINES 内。",
    })
    assert "```suggestion" not in out["body"], out["body"]
    assert "缺少 docstring" in out["body"]


def test_explicit_applicability_field_is_honoured():
    """模型显式声明 applicability=text_only → 即便给了 improved_code 也不给 Apply 按钮."""
    out = normalise_suggestion({**REPLACE_RAW, "applicability": "text_only"})
    assert "```suggestion" not in out["body"], out["body"]


def test_explicit_delete_field_without_intent_words_still_deletes():
    """模型显式声明 applicability=delete → 即便 rationale 没有「删除」字样也按删除处理."""
    out = normalise_suggestion({
        **DELETE_RAW,
        "rationale": "反向依赖 BAW, 违反 SSD-RULE-LIB-LAYER-ISOLATION。",
        "applicability": "delete",
    })
    assert "```suggestion:-0+0" in out["body"], out["body"]


# ---------- publish 端到端 ----------

def _real_rule_set():
    from reviewagent.scenario.profile import load_profile
    from reviewagent.scenario.rules.resolver import assemble_base

    return assemble_base(load_profile(None))


def _publish_one(raw: dict, *, note_id: str = "note-x"):
    from reviewagent.review.publishing import PublishCallbacks, PublishContext, publish

    gitlab = MagicMock()
    gitlab.post_mr_comment.return_value = 9001
    gitlab.post_mr_discussion.return_value = note_id
    callbacks = PublishCallbacks(
        diff_line_map=lambda: {},
        head_sha=lambda: "sha",
        normalise=normalise_suggestion,
        score=lambda sug: 0,
        validate=lambda **kw: {
            "action": "post", "new_line": kw["start_line"], "reason": "ok",
            "normalised_code": kw["improved_code"],
        },
        detect_apply_risk=lambda **kw: ("ok", []),
        build_summary_placeholder=lambda *a, **kw: "## 改进总览 V1\n\n_加载中…_",
        build_summary_v2=lambda *a, **kw: "## 改进总览 V1\n\nok",
        extract_guardrail_markers=lambda md: [],
        pre_reconcile=lambda **kw: {},
        publish_overview=MagicMock(return_value=None),
        help_footer="",
    )
    ctx = PublishContext(project_id=31, mr_iid=2, gitlab=gitlab)
    result = publish(
        ctx, {"summary_md": "", "suggestions": [raw]},
        rule_set=_real_rule_set(), profile=None, cfg=None, callbacks=callbacks,
    )
    body = gitlab.post_mr_discussion.call_args[0][2]
    return result, body


def test_publish_text_only_has_no_apply_button(tmp_telemetry):
    """端到端: text_only 建议发出去后, GitLab 侧不会出现 Apply suggestion 按钮."""
    result, body = _publish_one(dict(TEXT_ONLY_RAW))
    assert "```suggestion" not in body, body
    assert result["inline_posted"] == 1


def test_publish_delete_keeps_apply_button(tmp_telemetry):
    """端到端: 真删除建议仍带 Apply 按钮 (保留一键删除 import 的能力)."""
    result, body = _publish_one(dict(DELETE_RAW))
    assert "```suggestion:-0+0" in body, body
    assert result["inline_posted"] == 1


def test_publish_records_applicability_in_telemetry(tmp_telemetry):
    """落库要带上 applicability —— 审计要能区分「可 Apply」/「删除」/「纯文字」."""
    from reviewagent.telemetry.store import get_store

    _publish_one(dict(TEXT_ONLY_RAW), note_id="note-text-only")
    row = get_store().get_suggestion_by_note_id("note-text-only")
    assert row is not None
    assert row["applicability"] == "text_only"
    # text_only 不得落 improved_code (占位锚点不能被当成可 Apply 目标)
    assert not (row["improved_code"] or "").strip()


def test_publish_general_action_records_telemetry(tmp_telemetry):
    """general 分支 (收缩建议) 也必须落库.

    实测证据: project=3 mr=3 的 improve.ok 报 inline_posted=9, telemetry 只落 8 行 ——
    general 那条在 GitLab 上看得见, 顶部汇总表却不计数, 也没法 /adopt //dismiss.
    """
    from reviewagent.review.publishing import PublishCallbacks, PublishContext, publish
    from reviewagent.telemetry.store import get_store

    gitlab = MagicMock()
    gitlab.post_mr_comment.return_value = 9002
    gitlab.post_mr_discussion.return_value = "note-general"
    callbacks = PublishCallbacks(
        diff_line_map=lambda: {},
        head_sha=lambda: "sha",
        normalise=normalise_suggestion,
        score=lambda sug: 0,
        validate=lambda **kw: {
            "action": "general", "new_line": kw["start_line"],
            "reason": "shrinking suggestion (3 -> 2 lines)",
        },
        detect_apply_risk=lambda **kw: ("ok", []),
        build_summary_placeholder=lambda *a, **kw: "## 改进总览 V1\n\n_加载中…_",
        build_summary_v2=lambda *a, **kw: "## 改进总览 V1\n\nok",
        extract_guardrail_markers=lambda md: [],
        pre_reconcile=lambda **kw: {},
        publish_overview=MagicMock(return_value=None),
        help_footer="",
    )
    ctx = PublishContext(project_id=31, mr_iid=2, gitlab=gitlab)
    result = publish(
        ctx, {"summary_md": "", "suggestions": [dict(REPLACE_RAW)]},
        rule_set=_real_rule_set(), profile=None, cfg=None, callbacks=callbacks,
    )
    assert result["inline_posted"] == 1
    row = get_store().get_suggestion_by_note_id("note-general")
    assert row is not None, "general 建议必须落 telemetry, 否则汇总表漏计"
    assert row["file_path"] == REPLACE_RAW["file"]
