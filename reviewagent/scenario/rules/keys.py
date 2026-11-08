"""规则键模式（spec §6.4）：从 profile `rule_keys` 编译抽取/判定正则.

`builtin` 用于在任意文本中抽取内置规则键（带左边界负向断言，避免把
`SSD-RULE-...` 里的 `RULE` 误抽成内置键）；`repo` 只匹配仓库自定义规则键
（`<repo_prefix>-RULE-...`）。`repo_prefix` 为 `auto`（默认）时不绑定具体
前缀，按通用 `<大写前缀>-RULE-<KEY>` 形状匹配——仓库规则键随被检仓库变化，
不在工具里写死。`free_prefixes` 目前不参与抽取（T6 `rules check`
用它判断"自由前缀"），但仍随 profile 配置保留在 dataclass 上。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# profile 未配置 `rule_keys.builtin_pattern` 时沿用既有抽取形状（兼容包）。
DEFAULT_BUILTIN_PATTERN = (
    r"(?:R-OTHER-IMPACT:[a-z0-9_]+|R-OTHER:[a-z0-9_]+|R-[A-Z]+(?:-[A-Z0-9_]+)*)"
)

# `repo_prefix: auto` 时使用的通用仓库键形状：任意大写前缀 + `-RULE-`。
AUTO_REPO_KEY_SOURCE = r"[A-Z][A-Z0-9]*-RULE-[A-Z0-9-]+"
_AUTO_PREFIXES = frozenset({"", "auto", "*"})


def repo_key_source(prefix: str | None) -> str:
    """返回仓库键正则源码；`auto`/空 表示不绑定具体前缀."""
    value = (prefix or "").strip()
    if value.lower() in _AUTO_PREFIXES:
        return AUTO_REPO_KEY_SOURCE
    return rf"{re.escape(value)}-RULE-[A-Z0-9-]+"


@dataclass(frozen=True)
class KeyPatterns:
    builtin_raw: re.Pattern[str]
    builtin: re.Pattern[str]
    repo: re.Pattern[str]
    free_prefixes: tuple[str, ...]

    def extract(self, text: str) -> list[str]:
        """按出现顺序抽取内置键与仓库规则键（同键只保留首次出现）."""
        text = text or ""
        hits: list[tuple[int, str]] = []
        for pattern in (self.builtin, self.repo):
            for match in pattern.finditer(text):
                hits.append((match.start(), match.group(1)))
        found: list[str] = []
        for _, key in sorted(hits):
            if key not in found:
                found.append(key)
        return found

    def is_builtin(self, key: str) -> bool:
        return bool(self.builtin_raw.fullmatch(key))

    def is_repo(self, key: str) -> bool:
        return bool(self.repo.fullmatch(key))


def build_key_patterns(rule_keys: dict[str, Any]) -> KeyPatterns:
    pattern = str(rule_keys.get("builtin_pattern") or DEFAULT_BUILTIN_PATTERN)
    builtin_raw = re.compile(pattern)
    builtin = re.compile(rf"(?<![\w-])({pattern})")
    repo = re.compile(
        rf"(?<![\w])({repo_key_source(str(rule_keys.get('repo_prefix') or 'auto'))})(?![\w])"
    )
    return KeyPatterns(
        builtin_raw=builtin_raw,
        builtin=builtin,
        repo=repo,
        free_prefixes=tuple(rule_keys.get("free_prefixes") or ()),
    )
