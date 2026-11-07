"""源码块 + 跨文件 section 的装配（语言无关）.

职责边界（spec §7.3）：

* 本模块决定块标题、读写行、±N 合并、5000 行截断、前 5 段上限与最终拼接；
* 语言策略只经 ``policy.render_source_block`` 把已切好的片段渲染成带行号的
  文本（行号格式 / 缩进等语言差异全部在策略侧），不复制分段与预算逻辑；
* prompt 文案全部来自 profile 目录下的模板，本模块只做模板选择与变量填充。

模板每次从 ``profile.root`` 读取（``load_template`` 做路径逃逸校验），不加
进程内缓存 — golden 测试依赖磁盘原文。
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from reviewagent.review.policy import LanguagePolicy
from reviewagent.review.types import FileContext
from reviewagent.scenario.profile import Profile, active_or_load
from reviewagent.scenario.prompts import load_template, render_template

_SOURCE_DIR = "prompts/source"
_DYNAMIC_DIR = "prompts/dynamic"
# 单文件 full 源码上限（超出截断，防 prompt 撑爆参数长度）
_MAX_FULL_LINES = 5000
# patch 模式最多展示的 merged 段数（控 token 成本）
_MAX_PATCH_SEGMENTS = 5
# patch 模式「建议关注行」列表最多展示的行号数
_MAX_VALID_DISPLAY = 20


def _resolve_profile(profile: Profile | None) -> Profile:
    return profile or active_or_load()


def _render(
    rel_path: str,
    values: dict[str, str],
    profile: Profile | None,
    *,
    trim: bool,
) -> str:
    prof = _resolve_profile(profile)
    text = load_template(rel_path, prof, trim_trailing_newline=trim)
    return render_template(
        text, values, template_path=rel_path, profile_name=prof.name
    )


def _fragment(
    policy: LanguagePolicy,
    lines: list[str],
    lo: int,
    strategy: str,
) -> str:
    return policy.render_source_block(
        FileContext(
            file_path="",
            lines=tuple(lines),
            valid_lines=frozenset(),
            start_line=lo,
        ),
        strategy,
        0,
    )


def _merge_ranges(valid_lines: Iterable[int], n: int, total: int) -> list[list[int]]:
    ranges = [(max(1, v - n), min(total, v + n)) for v in valid_lines]
    merged: list[list[int]] = []
    for lo, hi in sorted(ranges):
        if merged and lo <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    return merged


def build_source_block(
    ctx: FileContext,
    strategy: str,
    *,
    cfg: Any,
    policy: LanguagePolicy,
    profile: Profile | None = None,
) -> str:
    """按 strategy 渲染源码块（与旧 ImproveCommand 实现逐字节一致）.

    ``strategy`` 取 ``full`` / ``partial`` / ``patch``；块标题、分段、合并、
    截断与段数上限都在本函数，行号格式由 ``policy`` 决定。
    """
    file_path = ctx.file_path
    lines = list(ctx.lines)

    if strategy == "full":
        if not lines:
            return _render(
                f"{_SOURCE_DIR}/full_empty.md",
                {"file_path": file_path},
                profile,
                trim=True,
            )
        total = len(lines)
        if total > _MAX_FULL_LINES:
            numbered = _fragment(policy, lines[:_MAX_FULL_LINES], 1, "full")
            return _render(
                f"{_SOURCE_DIR}/full_truncated.md",
                {
                    "file_path": file_path,
                    "total": str(total),
                    "limit": str(_MAX_FULL_LINES),
                    "omitted": str(total - _MAX_FULL_LINES),
                    "numbered": numbered,
                },
                profile,
                trim=True,
            )
        numbered = _fragment(policy, lines, 1, "full")
        return _render(
            f"{_SOURCE_DIR}/full.md",
            {
                "file_path": file_path,
                "total": str(total),
                "numbered": numbered,
            },
            profile,
            trim=True,
        )

    if strategy == "partial":
        if not lines:
            return _render(
                f"{_SOURCE_DIR}/partial_empty.md",
                {"file_path": file_path},
                profile,
                trim=True,
            )
        n = cfg.improve_partial_context_lines
        merged = _merge_ranges(sorted(ctx.valid_lines), n, len(lines))
        blocks = [
            _fragment(policy, lines[lo - 1 : hi], lo, "partial")
            for lo, hi in merged
        ]
        return _render(
            f"{_SOURCE_DIR}/partial.md",
            {
                "file_path": file_path,
                "n": str(n),
                "full_files": str(cfg.improve_full_files),
                "segments": str(len(merged)),
                "blocks": "\n...\n".join(blocks),
            },
            profile,
            trim=True,
        )

    n = cfg.improve_patch_context_lines
    vl = sorted(ctx.valid_lines)
    vl_display = vl[:_MAX_VALID_DISPLAY]
    vl_suffix = "..." if len(vl) > _MAX_VALID_DISPLAY else ""
    context_block = ""
    if lines and n > 0:
        merged = _merge_ranges(vl, n, len(lines))
        blocks = [
            _fragment(policy, lines[lo - 1 : hi], lo, "patch")
            for lo, hi in merged[:_MAX_PATCH_SEGMENTS]
        ]
        omitted = len(merged) - _MAX_PATCH_SEGMENTS if len(merged) > _MAX_PATCH_SEGMENTS else 0
        values = {
            "n": str(n),
            "shown": str(len(merged[:_MAX_PATCH_SEGMENTS])),
            "blocks": "\n...\n".join(blocks),
        }
        if omitted:
            rendered = _render(
                f"{_SOURCE_DIR}/patch_context_omitted.md",
                {**values, "omitted": str(omitted)},
                profile,
                trim=False,
            )
        else:
            rendered = _render(
                f"{_SOURCE_DIR}/patch_context.md",
                values,
                profile,
                trim=False,
            )
        context_block = "\n" + rendered
    return _render(
        f"{_SOURCE_DIR}/patch.md",
        {
            "file_path": file_path,
            "n": str(n),
            "valid_display": str(vl_display),
            "valid_suffix": vl_suffix,
            "context_block": context_block,
        },
        profile,
        trim=True,
    )


def render_cross_file_section(
    refs: list[dict],
    *,
    profile: Profile | None = None,
) -> str:
    """渲染跨文件影响分析 section（空引用 / 有引用两条模板）."""
    if not refs:
        return _render(
            f"{_DYNAMIC_DIR}/cross_file_refs_empty.md",
            {},
            profile,
            trim=False,
        )
    refs_block = "".join(
        f"**`{ref['file']}:{ref['line']}`** 引用 `{ref['ident']}`:\n"
        f"```\n{ref['content']}\n```\n\n"
        for ref in refs
    )
    return _render(
        f"{_DYNAMIC_DIR}/cross_file_refs_refs.md",
        {"refs_count": str(len(refs)), "refs_block": refs_block},
        profile,
        trim=False,
    )
