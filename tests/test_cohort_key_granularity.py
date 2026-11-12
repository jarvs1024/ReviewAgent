"""D14: cohort 粒度 —— 同文件同标题但**不同位置**的不同问题不得被并成一个.

真实业务证据 (E2E 全量档, 临时项目 37 / MR !1):
    3 条建议都落在 testlib/lib_baw/e2e_helper.py, 标题都是「日志补上下文」,
    行号分别 7 / 12 / 17 —— 是 3 个**不同位置**的不同问题.
    但 cohort_key = sha256(file + ":" + header) → 3 条落进同一个 cohort →
    `list_latest_by_cohort` 只保留最新一条 → 图1「总建议数」显示 10, 实际 12.
    被并掉的 2 条在 DB 里仍是 state=open、GitLab 上也仍可见 →
    「图1 待处理 9 条 / GitLab 上 12 条」自相矛盾, 采纳率分母偏小 → 虚高.

覆盖:
* `_cohort_key` 的粒度: 同文件同标题不同内容 → 不同 key; 同文件同标题同内容 → 同 key
* publish 端到端: 3 条同标题不同位置的建议 → 3 个 cohort, 图1 统计不丢
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


def _raw(*, line: int, existing: str, improved: str, header: str = "日志补上下文"):
    return {
        "file": "testlib/lib_baw/e2e_helper.py",
        "start_line": line,
        "severity": "low",
        "label": "code quality",
        "header": header,
        "rationale": "违反 `SSD-RULE-COMMON-NO-LOG-EXC`：日志缺上下文。",
        "existing_code": existing,
        "improved_code": improved,
        "rule_keys": ["R-LOG"],
    }


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
    return result


# ---------- 单元: cohort_key 粒度 ----------

def test_cohort_key_distinguishes_same_header_different_content():
    """同文件 + 同标题 + 不同代码内容 → 不同 cohort (是两个不同问题)."""
    from reviewagent.review.publishing import _cohort_key

    a = _cohort_key("a.py", "日志补上下文", "    logger.info('x')\n")
    b = _cohort_key("a.py", "日志补上下文", "    logger.info('y')\n")
    assert a != b, "同文件同标题但内容不同的两条建议不得并成一个 cohort"


def test_cohort_key_stable_for_same_issue():
    """同文件 + 同标题 + 同内容 → 同 cohort (跨轮次仍能归并)."""
    from reviewagent.review.publishing import _cohort_key

    a = _cohort_key("a.py", "日志补上下文", "    logger.info('x')\n")
    b = _cohort_key("a.py", "日志补上下文", "    logger.info('x')\n")
    assert a == b


def test_cohort_key_distinguishes_different_files():
    from reviewagent.review.publishing import _cohort_key

    a = _cohort_key("a.py", "日志补上下文", "    logger.info('x')\n")
    b = _cohort_key("b.py", "日志补上下文", "    logger.info('x')\n")
    assert a != b


def test_cohort_key_empty_existing_falls_back_to_line():
    """existing_code 为空时内容指纹恒定 —— 必须用行号兜底区分, 否则同文件
    同标题的多个「无锚点」问题又会并成一个 cohort（回到少算/采纳率虚高）."""
    from reviewagent.review.publishing import _cohort_key

    a = _cohort_key("a.py", "缺 license 头", "", 3)
    b = _cohort_key("a.py", "缺 license 头", "", 9)
    assert a != b

    same = _cohort_key("a.py", "缺 license 头", "  \n", 3)
    assert a == same, "空内容同位置应稳定归并"


# ---------- 端到端: publish 后统计不丢 ----------

def test_publish_same_header_different_lines_keeps_all_in_stats(tmp_telemetry):
    """3 条同文件同标题、不同位置的建议 → 3 个 cohort, 最新视图 3 条 (不被并掉)."""
    from reviewagent.telemetry.store import get_store

    raws = [
        _raw(line=7, existing="    logger.info('a')\n", improved="    logger.info('a', extra=1)\n"),
        _raw(line=12, existing="    logger.info('b')\n", improved="    logger.info('b', extra=1)\n"),
        _raw(line=17, existing="    logger.info('c')\n", improved="    logger.info('c', extra=1)\n"),
    ]
    for idx, raw in enumerate(raws):
        _publish_one(raw, note_id=f"note-{idx}")

    store = get_store()
    rows = store.list_latest_by_cohort(project_id=31, mr_iid=2)
    assert len(rows) == 3, f"3 个不同位置的建议应全部计入统计, 实际 {len(rows)}"
    cohorts = {r["cohort_key"] for r in rows}
    assert len(cohorts) == 3, f"应落在 3 个 cohort, 实际 {len(cohorts)}"
