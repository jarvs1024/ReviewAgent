"""通用语言策略（GenericPolicy）— 未注册语言的安全兜底.

设计要点（spec §7.3 / §9）：

* 只用 profile 数据 + 通用正则，不假设任何具体语言语法（无 AST、无关键字表）；
* 测试文件判定来自 profile 的 `test_paths`（`Profile` 若提供 `test_paths`
  属性则优先，否则读 `profile.scoring["test_paths"]`）；无数据时保守判为
  SOURCE / OTHER，不臆造 TEST；
* 校验是透传（动态/静态规则由引擎负责），评分只用共享数表（无仓库规则加分）.

C/C++ 首期只拿到 profile / language / policy 的注册接口，走本策略.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from fnmatch import fnmatch
from typing import Any

from reviewagent.review.policy import score_by_table
from reviewagent.review.types import FileContext, FileKind, RiskVerdict, Suggestion

# 通用标识符：3 字符起（过滤短变量噪音），与 Python 策略的 15 条上限对齐.
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_MAX_SYMBOLS = 15


class GenericPolicy:
    """LanguagePolicy 的保守通用实现."""

    language = "generic"

    def __init__(
        self,
        *,
        profile: Any,
        cfg: Any,
        read_lines: Callable[[str], list[str]],
    ) -> None:
        self._profile = profile
        self._cfg = cfg
        self._read_lines = read_lines

    # ---------- profile 数据 ----------

    def _test_paths(self) -> tuple[str, ...]:
        explicit = getattr(self._profile, "test_paths", None)
        if explicit:
            return tuple(str(p) for p in explicit)
        scoring = getattr(self._profile, "scoring", None)
        if isinstance(scoring, dict):
            return tuple(str(p) for p in (scoring.get("test_paths") or ()))
        return ()

    # ---------- 钩子 ----------

    def classify_file(self, path: str) -> FileKind:
        normalized = (path or "").replace("\\", "/")
        if not normalized:
            return FileKind.OTHER
        base = normalized.rsplit("/", 1)[-1]
        for pattern in self._test_paths():
            if pattern.endswith("/"):
                # 目录前缀（如 `tests/`）— 目录名必须完整匹配某一段
                parts = normalized.split("/")[:-1]
                if pattern.rstrip("/") in parts:
                    return FileKind.TEST
            elif fnmatch(normalized, pattern) or fnmatch(base, pattern):
                return FileKind.TEST
        return FileKind.SOURCE

    def test_feature_density(self, path: str, size: int) -> float:
        # 通用策略没有测试特征关键字表 → 不贡献测试风险权重.
        return 0.0

    def extract_symbols(self, diff: str) -> list[str]:
        if not diff:
            return []
        added = "\n".join(
            ln[1:]
            for ln in diff.splitlines()
            if ln.startswith("+") and not ln.startswith("+++")
        )
        return sorted(set(_IDENTIFIER_RE.findall(added)))[:_MAX_SYMBOLS]

    def render_source_block(self, ctx: FileContext, strategy: str, budget: int) -> str:
        return "\n".join(
            f"{ctx.start_line + i:4d}| {line}" for i, line in enumerate(ctx.lines)
        )

    def normalize_suggestion(self, sug: Suggestion, ctx: FileContext) -> Suggestion:
        return sug

    def read_lines(self, path: str) -> list[str]:
        """读文件源码行（复用门面注入的惰性预读回调）."""
        return self._read_lines(path)

    def fix_indent(self, target_line: str, improved_code: str) -> str:
        """通用语言不做缩进猜测：improved_code 原样返回."""
        return improved_code

    def code_first_line_matches(self, target_line: str, improved_first: str) -> bool:
        """无语言语法知识时的保守判定：strip 后非空且相等才算同一行."""
        target = (target_line or "").strip()
        first = (improved_first or "").strip()
        return bool(target) and target == first

    def validate_suggestion(
        self,
        sug: Suggestion,
        ctx: FileContext,
        *,
        rule_set: Any,
        file_sources: dict[str, list[str]],
        line_map: dict[str, set[int]],
    ) -> RiskVerdict:
        return {"action": "post"}

    def score_suggestion(self, sug: Suggestion) -> int:
        return score_by_table(sug)

    def keywords(self) -> set[str]:
        return set()

    def prompt_sections(self) -> dict[str, str]:
        return {}
