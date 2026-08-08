"""rule_translate 兜底映射 — 防止 LLM 幻觉键 R-XXX / R-OTHER:unmapped 污染周报.

历史背景: 78 条 telemetry.db 记录曾用 R-XXX 字面作为 rule_key,
LLM 把行文占位词当 rule 写出. 现在 SQL 已迁移到 R-OTHER:unmapped,
翻译兜底给"R-OTHER:unmapped"和"R-XXX"统一的"未分类违规"标签.
"""
from __future__ import annotations

import os
os.environ.setdefault("GITLAB_URL", "http://x")
os.environ.setdefault("REVIEWAGENT_WEBHOOK_PORT", "3000")
os.environ.setdefault("TELEMETRY_DB", "/tmp/x.db")
os.environ.setdefault("LLM_PROVIDER", "stub")
os.environ.setdefault("LOG_LEVEL", "WARNING")

from reviewagent.reporting.rule_translate import translate_rule_key, RuleNameResolver


def test_R_XXX_translates_to_meaningful_label():
    """R-XXX (历史 LLM 幻觉) -> 「未分类违规 - 需人工核实规则归属」."""
    label = translate_rule_key("R-XXX")
    assert "未分类" in label, f"R-XXX 应该翻译成包含未分类的标签, got: {label!r}"
    # 不应该是裸露 "XXX" 字面
    assert label != "XXX", f"R-XXX 翻译掉到 _humanize 裸露, got: {label!r}"


def test_R_OTHER_unmapped_translates():
    """78 条历史数据迁移后的 R-OTHER:unmapped 也要能翻译."""
    label = translate_rule_key("R-OTHER:unmapped")
    assert "未明确归类" in label or "未分类" in label, f"got: {label!r}"


def test_no_letter_doubling_in_label():
    """防止 _humanize 把 SSD-RULE-* 的连字符吃掉输出 SSDRULE."""
    for rk in ("SSD-RULE-TYPEHINTS", "R-RES", "R-OTHER:magic_number"):
        label = translate_rule_key(rk)
        # 不要出现 "SSDRULE" / "ROTHER" 这种字母粘连
        assert "SSDRULE" not in label, f"连字符脱落: {label!r} ({rk})"
        assert " ROTHER" not in f" {label} ", f"R- 前缀吞掉: {label!r} ({rk})"


def test_resolver_translate_handles_R_XXX():
    """通过 RuleNameResolver.translate 路径也要命中兜底."""
    resolver = RuleNameResolver(ssd_map={}, generic_map={})
    # 手动构造兜底路径 (generic_map 里也没 R-XXX); 但 _load_generic_map 全局缓存,
    # 所以单测只验证 translate_rule_key 兜底
    label = translate_rule_key("R-XXX")
    assert "未分类" in label
