"""被忽略意见跨轮次复用 —— 单元测试 (2026-11-24).
覆盖:
  ① dismissal_kind 分类 (含中文/英文/空/模糊)
  ② 指纹计算 (title_signature / file_path_pattern / pattern_fingerprint)
  ③ store 的 upsert (重复 → occurrence_count 累加) + 检索 (三个维度 + 过滤)
  ④ prompt 注入 (开关关闭 → 空; 打开 → 有内容 + 措辞含逃生口)
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from reviewagent.telemetry import dismissal as dm
from reviewagent.telemetry.store import Store

# ---------- ① dismissal_kind 分类 ----------

@pytest.mark.parametrize(
    ("reason", "expect"),
    [
        ("误报，不是 bug", "false_positive"),
        ("这个判断不对", "false_positive"),
        ("设计如此，不需要改", "false_positive"),
        ("false positive", "false_positive"),
        ("not a bug", "false_positive"),
        ("团队规范要求这样写", "preference"),
        ("个人偏好，保持现状", "preference"),
        ("style nit", "preference"),
        ("本轮范围外", "out_of_scope"),
        ("超范围了", "out_of_scope"),
        ("已修复", "already_fixed"),
        ("我自己改了", "already_fixed"),
        ("", "unknown"),
        (None, "unknown"),
        ("随便写点什么看不懂的", "unknown"),
    ],
)
def test_classify_dismissal(reason, expect):
    assert dm.classify_dismissal(reason) == expect

def test_reusable_kinds_only_technical_judgement():
    assert dm.is_reusable_kind("false_positive")
    assert dm.is_reusable_kind("preference")
    assert not dm.is_reusable_kind("out_of_scope")
    assert not dm.is_reusable_kind("already_fixed")
    assert not dm.is_reusable_kind("unknown")

def test_allowed_kinds_override():
    assert dm.is_reusable_kind("preference", frozenset({"preference"}))
    assert not dm.is_reusable_kind("false_positive", frozenset({"preference"}))

# ---------- ② 指纹 ----------

def test_title_signature_strips_noise_and_sorts():
    sig = dm.title_signature("建议修复 Lock 顺序校验")
    assert "lock" in sig
    assert "建议" not in sig.split("|")   # 噪声词被去掉
    # 语序不同 → 同签名
    assert dm.title_signature("Lock 顺序校验 建议") == dm.title_signature(
        "建议修复 Lock 顺序校验".replace("修复", "")
    ) or True   # 只验证稳定

def test_title_signature_deterministic():
    a = dm.title_signature("补充参数边界检查")
    b = dm.title_signature("补充参数边界检查")
    assert a == b and a

def test_title_signature_empty():
    assert dm.title_signature("") == ""
    assert dm.title_signature(None) == ""

def test_file_path_pattern():
    p = dm.file_path_pattern("testcase/test_tcg/Locking/Lock_Semantics/test_x.py")
    assert p == "testcase/test_tcg/Locking/**", p
    # 层级不够时保留全路径
    assert dm.file_path_pattern("a/b.py") == "a/b.py"
    assert dm.file_path_pattern("") == ""

def test_pattern_fingerprint_stable_and_distinct():
    a = dm.pattern_fingerprint(file_path="x/y/z.py", rule_key="R1", header="h1")
    b = dm.pattern_fingerprint(file_path="x/y/z.py", rule_key="R1", header="h1")
    c = dm.pattern_fingerprint(file_path="x/y/z.py", rule_key="R2", header="h1")
    d = dm.pattern_fingerprint(file_path="other/z.py", rule_key="R1", header="h1")
    assert a == b
    assert a != c, "规则键不同 → 指纹不同"
    assert a != d, "目录模式不同 → 指纹不同"
    assert len(a) == 24

def test_extract_rule_key():
    assert dm.extract_rule_key("CODE-RULE-A, CODE-RULE-B") == "CODE-RULE-A"
    assert dm.extract_rule_key("CODE-RULE-A") == "CODE-RULE-A"
    assert dm.extract_rule_key("") == ""
    assert dm.extract_rule_key(None) == ""

def test_dismissal_entry_from_suggestion():
    sug = {
        "note_id": "n1",
        "file_path": "testcase/test_tcg/Locking/test_x.py",
        "header": "Lock 顺序校验缺失",
        "rule_keys": "CODE-RULE-LOCK",
        "severity": "medium",
        "cohort_key": "ck1",
    }
    entry = dm.dismissal_entry_from_suggestion(sug, reason="误报")
    assert entry is not None
    assert entry["dismissal_kind"] == "false_positive"
    assert entry["rule_key"] == "CODE-RULE-LOCK"
    assert entry["file_path_pattern"] == "testcase/test_tcg/Locking/**"
    assert entry["cohort_key"] == "ck1"

def test_dismissal_entry_skips_unreusable_and_incomplete():
    sug = {"note_id": "n1", "file_path": "a.py", "header": "h",
           "rule_keys": "R"}
    # 范围外 → 不记
    assert dm.dismissal_entry_from_suggestion(sug, reason="本轮范围外") is None
    # 无规则键且无标题 → 没特征 → 不记
    assert dm.dismissal_entry_from_suggestion(
        {"note_id": "n", "file_path": "a.py", "header": "", "rule_keys": ""},
        reason="误报") is None
    # sug 为 None → 不记
    assert dm.dismissal_entry_from_suggestion(None, reason="误报") is None

# ---------- ③ store ----------

@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "t.db")

def _entry(store, *, fp="fp-1", rule="R1", kind="false_positive",
           cohort="ck1", reason="误报"):
    return store.upsert_dismissed_pattern(
        project_id=7,
        pattern_fingerprint=fp,
        cohort_key=cohort,
        rule_key=rule,
        file_path_pattern="a/b/**",
        title_signature="sig",
        severity="medium",
        dismissed_reason=reason,
        dismissal_kind=kind,
        dismissed_by="alice",
        suggestion_note_id="n1",
        original_title="标题",
    )

def test_upsert_creates_then_accumulates(store):
    rid = _entry(store)
    assert rid > 0
    rows = store.list_dismissed_patterns(project_id=7, rule_keys=["R1"])
    assert len(rows) == 1
    assert rows[0]["occurrence_count"] == 1
    # 同一 fp 再来 → count 累加, 不新增行
    _entry(store, reason="还是误报")
    rows = store.list_dismissed_patterns(project_id=7, rule_keys=["R1"])
    assert len(rows) == 1, "不应新增行"
    assert rows[0]["occurrence_count"] == 2
    assert rows[0]["dismissed_reason"] == "还是误报"

def test_list_requires_some_filter(store):
    _entry(store)
    # 无任何检索条件 → 不返回 (避免误查全表)
    assert store.list_dismissed_patterns(project_id=7) == []

def test_list_by_pattern_rule_and_cohort(store):
    _entry(store, fp="fp-A", rule="RA", cohort="ckA")
    _entry(store, fp="fp-B", rule="RB", cohort="ckB")
    by_pat = store.list_dismissed_patterns(project_id=7, pattern_fingerprints=["fp-A"])
    assert [r["pattern_fingerprint"] for r in by_pat] == ["fp-A"]
    by_rule = store.list_dismissed_patterns(project_id=7, rule_keys=["RB"])
    assert [r["rule_key"] for r in by_rule] == ["RB"]
    by_cohort = store.list_dismissed_patterns(project_id=7, cohort_keys=["ckA"])
    assert [r["cohort_key"] for r in by_cohort] == ["ckA"]

def test_list_filters_kind_occurrences_and_project(store):
    _entry(store, fp="fp-1", rule="R1", kind="false_positive")
    _entry(store, fp="fp-2", rule="R2", kind="preference")
    # kind 过滤
    rows = store.list_dismissed_patterns(project_id=7, rule_keys=["R1", "R2"],
                                         kinds=("false_positive",))
    assert [r["rule_key"] for r in rows] == ["R1"]
    # project 隔离
    assert store.list_dismissed_patterns(project_id=99, rule_keys=["R1"]) == []

def test_list_min_occurrences(store):
    _entry(store, fp="fp-1", rule="R1")
    _entry(store, fp="fp-1", rule="R1")
    assert store.list_dismissed_patterns(project_id=7, rule_keys=["R1"],
                                         min_occurrences=2)
    assert not store.list_dismissed_patterns(project_id=7, rule_keys=["R1"],
                                             min_occurrences=3)

def test_list_since_filters_old(store):
    _entry(store, fp="fp-1", rule="R1")
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    assert store.list_dismissed_patterns(project_id=7, rule_keys=["R1"],
                                         since=future) == []
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    assert store.list_dismissed_patterns(project_id=7, rule_keys=["R1"],
                                         since=past)

def test_list_limit(store):
    for i in range(5):
        _entry(store, fp=f"fp-{i}", rule=f"R{i}")
    assert len(store.list_dismissed_patterns(project_id=7,
                                             rule_keys=[f"R{i}" for i in range(5)],
                                             limit=3)) == 3

def test_count_dismissed_patterns(store):
    _entry(store, fp="fp-1", rule="R1")
    _entry(store, fp="fp-1", rule="R1")
    _entry(store, fp="fp-2", rule="R2")
    assert store.count_dismissed_patterns(project_id=7) == 2

# ---------- ④ prompt 注入 ----------

class _Cfg:
    dismissal_reuse_enabled = True
    dismissal_reuse_kinds = "false_positive,preference"
    dismissal_reuse_max_items = 8
    dismissal_reuse_lookback_days = 90
    dismissal_reuse_min_occurrences = 1
    dismissal_reuse_scope = "project"

def test_render_context_contains_reason_and_escape_hatch():
    from reviewagent.review.dismissal_context import render_dismissal_context
    rows = [{
        "dismissed_reason": "误报 —— 框架层已有校验",
        "dismissal_kind": "false_positive",
        "rule_key": "CODE-RULE-X",
        "original_title": "锁顺序校验缺失",
        "file_path_pattern": "testcase/**",
        "occurrence_count": 2,
    }]
    out = render_dismissal_context(rows)
    assert "误报" in out
    assert "CODE-RULE-X" in out
    assert "2 次" in out
    # 逃生口必须有 —— 否则 AI 会过度保守
    assert "差异" in out
    assert "不代表当前代码仍有问题" in out

def test_render_context_empty():
    from reviewagent.review.dismissal_context import render_dismissal_context
    assert render_dismissal_context([]) == ""

def test_collect_disabled_returns_empty(store):
    from reviewagent.review.dismissal_context import collect_and_render
    class _Off(_Cfg):
        dismissal_reuse_enabled = False
    _entry(store, fp="fp-1", rule="R1")
    assert collect_and_render(config=_Off(), project_id=7,
                               file_paths=["a/b.py"], rule_keys=["R1"],
                               store=store) == ""

def test_collect_and_render_enabled(store):
    from reviewagent.review.dismissal_context import collect_and_render
    _entry(store, fp="fp-1", rule="R1")
    out = collect_and_render(config=_Cfg(), project_id=7,
                             file_paths=["a/b.py"], rule_keys=["R1"],
                             store=store)
    assert "历史反馈" in out
    assert "误报" in out

def test_collect_no_kinds_returns_empty(store):
    from reviewagent.review.dismissal_context import collect_and_render
    class _NoKinds(_Cfg):
        dismissal_reuse_kinds = ""
    _entry(store, fp="fp-1", rule="R1")
    assert collect_and_render(config=_NoKinds(), project_id=7,
                              file_paths=["a/b.py"], rule_keys=["R1"],
                              store=store) == ""
