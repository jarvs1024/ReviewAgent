"""LanguagePolicy 协议（spec §7.3 的 12 个钩子）+ 跨语言共享评分表.

设计要点：

* 协议只描述「语言差异」，引擎（planner / context / assembly / runner /
  validation）走语言无关路径，差异全部经本协议表达；
* `score_suggestion` 的默认数表（severity / label / rationale 长度 / header）
  是历史行为的基线，放在这里供各策略复用，避免每个语言策略复制一份；
* 规则引用加分只认 `{rule_prefix}-RULE-*` 形式（与旧 `_RULE_EXEMPT_REGEX`
  逐字等价），前缀由调用方在**调用时**给出，不做 import-time 绑定.
"""
from __future__ import annotations

import re
from typing import Any, Protocol

from reviewagent.review.types import FileContext, FileKind, RiskVerdict, Suggestion
from reviewagent.scenario.rules.keys import normalize_repo_prefix


class LanguagePolicy(Protocol):
    """单一语言的检视策略（12 个钩子）."""

    def classify_file(self, path: str) -> FileKind: ...

    def test_feature_density(self, path: str, size: int) -> float: ...

    def extract_symbols(self, diff: str) -> list[str]: ...

    def render_source_block(self, ctx: FileContext, strategy: str, budget: int) -> str: ...

    def normalize_suggestion(self, sug: Suggestion, ctx: FileContext) -> Suggestion: ...

    def validate_suggestion(
        self,
        sug: Suggestion,
        ctx: FileContext,
        *,
        rule_set: Any,
        file_sources: dict[str, list[str]],
        line_map: dict[str, set[int]],
    ) -> RiskVerdict: ...

    def score_suggestion(self, sug: Suggestion) -> int: ...

    def keywords(self) -> set[str]: ...

    def prompt_sections(self) -> dict[str, str]: ...

    # ---- 代码形态钩子（validation 的惰性预读 / Apply 修正路径）----

    def read_lines(self, path: str) -> list[str]: ...

    def fix_indent(self, target_line: str, improved_code: str) -> str: ...

    def code_first_line_matches(self, target_line: str, improved_first: str) -> bool: ...


# ---------------------------------------------------------------------------
# 共享评分表（与旧 ImproveCommand._score_suggestion 数表逐字一致）
# ---------------------------------------------------------------------------

SEVERITY_SCORES: dict[str, int] = {"high": 30, "medium": 20, "low": 10}

# cross-file impact 与 potential bug 同级, 体现 P1 优先级
LABEL_SCORES: dict[str, int] = {
    "security": 30,
    "potential bug": 25,
    "cross-file impact": 25,
    "performance": 20,
    "enhancement": 15,
    "code quality": 12,
    "style": 5,
}

_RULE_REF_CACHE: dict[str, re.Pattern[str]] = {}


def rule_ref_pattern(rule_prefix: str) -> re.Pattern[str]:
    """按前缀缓存「仓库规则引用」正则（`{prefix}-RULE-*`）.

    显式前缀时与旧 `_RULE_EXEMPT_REGEX`（`\\b{prefix}-RULE-\\w+\\b`）逐字等价，
    保证评分与严重度豁免的判定与重构前完全一致；`SSD` 与 `SSD-RULE` 两种写法
    归一化后等价；`auto`（默认）时匹配任意大写前缀，仓库规则键随被检仓库变化，
    不在工具里写死.
    """
    value = normalize_repo_prefix(rule_prefix)
    cache_key = value or "auto"
    pattern = _RULE_REF_CACHE.get(cache_key)
    if pattern is None:
        source = rf"{re.escape(value)}-RULE-\w+" if value else r"[A-Z][A-Z0-9]*-RULE-\w+"
        pattern = re.compile(rf"\b{source}\b")
        _RULE_REF_CACHE[cache_key] = pattern
    return pattern


def score_by_table(sug: Suggestion, *, rule_prefix: str | None = None) -> int:
    """默认评分（0~100）：severity + label + rationale 长度 + 规则引用 + header.

    `rule_prefix` 为 None 时不加规则引用分（通用策略没有仓库规则键概念）.
    """
    score = 0
    # severity (0~30)
    score += SEVERITY_SCORES.get((sug.get("severity") or "medium").lower(), 20)
    # label (0~30)
    score += LABEL_SCORES.get((sug.get("label") or "").lower(), 10)
    # rationale 长度 (0~20)
    rationale = sug.get("rationale") or ""
    if len(rationale) > 200:
        score += 20
    elif len(rationale) > 100:
        score += 15
    elif len(rationale) > 50:
        score += 8
    # 规则引用 (+10) — 仅 {prefix}-RULE 形式加分
    if rule_prefix and rule_ref_pattern(rule_prefix).search(rationale):
        score += 10
    # header 质量 (+5)
    header = (sug.get("header") or "").strip()
    if len(header) >= 4 and header != "建议改进":
        score += 5
    return min(100, score)
