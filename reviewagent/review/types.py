"""语言抽象层共享类型（spec §7.1）.

`Suggestion` / `RiskVerdict` 沿用 improve 的 dict 载荷形态（与 GitLab
suggestion body 构造保持零转换）；`FileContext` 是文件级上下文的最小快照，
由 `review/context.py`（Task 9）构造，策略只读。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

Suggestion = dict[str, Any]
RiskVerdict = dict[str, Any]


class FileKind(StrEnum):
    """文件类别 — 决定 planning / prompt 组装 / 测试抽样策略."""

    TEST = "test"
    SOURCE = "source"
    FIXTURE = "fixture"
    OTHER = "other"


@dataclass(frozen=True)
class FileContext:
    """单文件上下文快照（策略钩子的输入）.

    Attributes:
        file_path: 仓库相对路径（与 diff / telemetry 的 file 字段一致）.
        lines: 已切好的源码片段（策略只做片段内格式化，不做分段）.
        valid_lines: diff 中可发布 inline suggestion 的 1-based 行号集合.
        diff: 该文件的 unified diff 文本（空串 = 无 diff / 非 diff 文件）.
        worktree: 文件所在 worktree 根（None = 无 worktree）.
        start_line: `lines[0]` 在文件中的 1-based 行号（片段渲染用）.
    """

    file_path: str
    lines: tuple[str, ...]
    valid_lines: frozenset[int]
    diff: str = ""
    worktree: Path | None = None
    start_line: int = 1
