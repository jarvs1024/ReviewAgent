"""D16: 「收缩建议」(general 分支) 的正文形态与落库信息必须自洽.

真实业务证据 (E2E 全量档, 临时项目 40 / MR !1 / note 8b6d403e):
    建议: 把 5 行手写累加循环换成 2 行 `sum()` —— improved 行数 < existing 行数
    → validation 判定为「收缩」→ 走 **general 分支**（刻意不给 Apply 按钮,
       因为一键 Apply 会直接删掉 3 行有效代码).

    但实测:
      1. 落库 applicability = **replace** —— 与正文形态矛盾
         (replace 的契约是「有可直接 Apply 的 suggestion 块」, 这里没有块);
      2. 正文尾部复用了含「点上方『应用建议』按钮」的页脚 ——
         **根本没有那个按钮**, 用户被引导去找不存在的按钮
         (与 D1「空建议块却渲染 Apply 按钮」同源的反向问题);
      3. E2E 字段级审计因此报红: `replace 正文缺 suggestion 块` +
         `improved_code diverges from posted note` —— 审计报的是真问题.

修复口径: general 分支与 text_only **同构** —— 无 suggestion 块、无 Apply 引导、
落库 applicability=text_only、不落 improved_code（没有可 Apply 的形态就不落，
避免 auto_detect 把它当成可 Apply 目标代码）。
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


def _real_rule_set():
    from reviewagent.scenario.profile import load_profile
    from reviewagent.scenario.rules.resolver import assemble_base

    return assemble_base(load_profile(None))


SHRINK_RAW = {
    "file": "app/service.py",
    "start_line": 11,
    "severity": "low",
    "label": "code quality",
    "header": "用内置 sum",
    "rationale": "R-OTHER:reimplemented_builtin 手写累加循环等价于内置 `sum()`。",
    "existing_code": (
        "def total(items):\n"
        "    result = 0\n"
        "    for item in items:\n"
        "        result += item\n"
        "    return result"
    ),
    "improved_code": "def total(items):\n    return sum(items)",
    "rule_keys": ["R-OTHER:reimplemented_builtin"],
}


def _publish_shrink(*, note_id: str = "note-shrink"):
    """走 general 分支（收缩建议）发布一条建议，返回 (结果, 正文)."""
    from reviewagent.review.publishing import PublishCallbacks, PublishContext, publish

    gitlab = MagicMock()
    gitlab.post_mr_comment.return_value = 9001
    gitlab.post_mr_discussion.return_value = note_id
    callbacks = PublishCallbacks(
        diff_line_map=lambda: {},
        head_sha=lambda: "sha",
        normalise=normalise_suggestion,
        score=lambda sug: 0,
        # 收缩建议 → general 分支 (无 Apply 按钮)
        validate=lambda **kw: {
            "action": "general", "new_line": kw["start_line"],
            "reason": "shrinking suggestion (5 -> 2 lines)",
        },
        detect_apply_risk=lambda **kw: ("ok", []),
        build_summary_placeholder=lambda *a, **kw: "## 改进总览 V1\n\n_加载中…_",
        build_summary_v2=lambda *a, **kw: "## 改进总览 V1\n\nok",
        extract_guardrail_markers=lambda md: [],
        pre_reconcile=lambda **kw: {},
        publish_overview=MagicMock(return_value=None),
        help_footer="\n\n✅ 接受建议\n   • 直接用：点上方「应用建议」按钮\n",
        help_footer_text_only="\n\n✅ 接受建议\n   • 自己改：请先提交修改，再回复 `/adopt [理由]`\n",
    )
    ctx = PublishContext(project_id=31, mr_iid=2, gitlab=gitlab)
    result = publish(
        ctx, {"summary_md": "", "suggestions": [dict(SHRINK_RAW)]},
        rule_set=_real_rule_set(), profile=None, cfg=None, callbacks=callbacks,
    )
    body = gitlab.post_mr_discussion.call_args[0][2]
    return result, body


def test_general_branch_body_has_no_apply_button(tmp_telemetry):
    """收缩建议正文**不得**提示「点上方『应用建议』按钮」—— 那个按钮不存在."""
    _result, body = _publish_shrink()
    assert "```suggestion" not in body, body
    assert "应用建议" not in body, f"收缩建议没有 Apply 按钮, 正文却提示去点它: {body}"
    assert "收缩" in body, "应保留收缩警告, 提醒人工评估"


def test_general_branch_records_text_only_applicability(tmp_telemetry):
    """general 分支无 suggestion 块 → 落库 applicability 必须是 text_only."""
    from reviewagent.telemetry.store import get_store

    _publish_shrink(note_id="note-shrink-2")
    row = get_store().get_suggestion_by_note_id("note-shrink-2")
    assert row is not None, "收缩建议必须落 telemetry"
    assert row["applicability"] == "text_only", (
        f"applicability={row['applicability']!r} 与正文形态(无 suggestion 块)矛盾"
    )


def test_general_branch_does_not_store_improved_code(tmp_telemetry):
    """没有可 Apply 的形态就不落 improved_code（避免 auto_detect 误判为可 Apply 目标）."""
    from reviewagent.telemetry.store import get_store

    _publish_shrink(note_id="note-shrink-3")
    row = get_store().get_suggestion_by_note_id("note-shrink-3")
    assert not (row.get("improved_code") or "").strip(), (
        f"收缩建议不应落 improved_code: {row.get('improved_code')!r}"
    )


def test_general_branch_still_records_suggestion(tmp_telemetry):
    """修复不能把 general 分支的建议弄丢: 它仍要落库、可被 /adopt /dismiss 定位."""
    from reviewagent.telemetry.store import get_store

    result, _body = _publish_shrink(note_id="note-shrink-4")
    assert result["inline_posted"] == 1, result
    row = get_store().get_suggestion_by_note_id("note-shrink-4")
    assert row is not None
    assert row["file_path"] == "app/service.py"
    assert int(row["target_line"]) == 11
    assert row["state"] == "open"
