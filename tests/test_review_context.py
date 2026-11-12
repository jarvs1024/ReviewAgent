"""review/context.py 边界用例：与重构前三段源码块实现逐字节比对.

旧实现（ImproveCommand._build_full_source_block / _build_partial_context_block /
_build_patch_only_block）在本文件内重新实现为 `_legacy_*`，仅作测试对照；
覆盖空文件、5000 行截断、±N 合并、patch 段数上限（>5）、patch n=0、
valid_lines > 20 等边界。跨文件 section 校验空 / 有引用两种模板与结尾换行。
"""
from __future__ import annotations

import dataclasses

import pytest

from reviewagent.review import context
from reviewagent.review.policies.python import PythonPolicy
from reviewagent.review.types import FileContext
from reviewagent.scenario.profile import load_profile


def _legacy_full(file_path: str, lines: list[str]) -> str:
    _max = 5000
    if not lines:
        return f"### 完整源码\n(无法读取 {file_path})"
    total = len(lines)
    if total > _max:
        kept = lines[:_max]
        numbered = "\n".join(f"{i+1:4d}| {ln}" for i, ln in enumerate(kept))
        return (
            f"### 完整源码: `{file_path}` (共 {total} 行, **已截断到前 {_max} 行**)\n"
            f"⚠️ 末尾 {total - _max} 行未加载, 如需检视请缩小 diff 范围或拆分 MR\n```\n{numbered}\n```"
        )
    numbered = "\n".join(f"{i+1:4d}| {ln}" for i, ln in enumerate(lines))
    return f"### 完整源码: `{file_path}` (共 {total} 行)\n```\n{numbered}\n```"


def _legacy_partial(
    file_path: str, valid_lines: set[int], lines: list[str], n: int, full_files: int,
) -> str:
    if not lines:
        return f"### 上下文 (partial)\n(无法读取 {file_path})"
    ranges: list[tuple[int, int]] = []
    for vl in sorted(valid_lines):
        lo, hi = max(1, vl - n), min(len(lines), vl + n)
        ranges.append((lo, hi))
    merged: list[list[int]] = []
    for lo, hi in sorted(ranges):
        if merged and lo <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    blocks: list[str] = []
    for lo, hi in merged:
        chunk = lines[lo - 1:hi]
        blocks.append("\n".join(f"{lo+i:4d}| {ln}" for i, ln in enumerate(chunk)))
    return (
        f"### diff 上下文 (partial, ±{n} 行): `{file_path}`\n"
        f"⚠️ 此文件超出 IMPROVE_FULL_FILES={full_files} 配额, 已降级为 partial 模式\n"
        f"⚠️ 仅显示 diff 附近 {len(merged)} 段 (±{n} 行), 跨段语义识别能力下降\n"
        f"```\n" + "\n...\n".join(blocks) + "\n```"
    )


def _legacy_patch(file_path: str, valid_lines: set[int], lines: list[str], n: int) -> str:
    vl = sorted(valid_lines)
    vl_display = vl[:20]
    vl_suffix = "..." if len(vl) > 20 else ""
    context_block = ""
    if lines and n > 0:
        ranges: list[tuple[int, int]] = []
        for v in vl:
            lo, hi = max(1, v - n), min(len(lines), v + n)
            ranges.append((lo, hi))
        merged: list[list[int]] = []
        for lo, hi in sorted(ranges):
            if merged and lo <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], hi)
            else:
                merged.append([lo, hi])
        blocks: list[str] = []
        for lo, hi in merged[:5]:
            chunk = lines[lo - 1:hi]
            blocks.append("\n".join(f"{lo+i:4d}| {ln}" for i, ln in enumerate(chunk)))
        omitted = len(merged) - 5 if len(merged) > 5 else 0
        context_block = (
            f"\n### 最小上下文 (patch, ±{n} 行, 前 {len(merged[:5])} 段)\n"
            f"```\n" + "\n...\n".join(blocks) + "\n```\n"
            + (f"(另有 {omitted} 段未展示)\n" if omitted else "")
        )
    return (
        f"### patch-only: `{file_path}`\n"
        f"⚠️ 已降级为 patch-only (overflow 或低优先级), 仅 diff + ±{n} 行最小 context\n"
        f"⚠️ 跨行/跨段语义识别能力有限, 优先识别行内问题 (裸 except / 可变默认参数 / 硬编码等)\n"
        f"建议关注行: {vl_display}{vl_suffix}\n"
        f"{context_block}"
    )


@pytest.fixture(scope="module")
def profile():
    return load_profile("ssd-python-autotest")


def _cfg(*, partial: int = 20, patch: int = 3, full_files: int = 20):
    from reviewagent.config import current

    return dataclasses.replace(
        current(),
        improve_partial_context_lines=partial,
        improve_patch_context_lines=patch,
        improve_full_files=full_files,
    )


def _build(ctx, strategy, cfg, profile):
    policy = PythonPolicy(profile=profile, cfg=cfg, read_lines=lambda _p: [])
    return context.build_source_block(
        ctx, strategy, cfg=cfg, policy=policy, profile=profile
    )


def _lines(count: int) -> list[str]:
    return [f"line {i}" for i in range(1, count + 1)]


FULL_CASES = {
    "empty": [],
    "small": _lines(4),
    "truncated": _lines(5001),
}

PARTIAL_CASES = {
    "empty": ([], frozenset()),
    "one_range": (_lines(100), frozenset({10, 12})),
    "two_ranges": (_lines(100), frozenset({10, 60})),
    "no_valid": (_lines(40), frozenset()),
}

PATCH_CASES = {
    "empty_file": ([], frozenset({1}), 3),
    "n_zero": (_lines(40), frozenset({5, 15}), 0),
    "two_ranges": (_lines(40), frozenset({5, 15}), 3),
    "segment_cap": (_lines(200), frozenset({10, 40, 70, 100, 130, 160}), 3),
    "display_cap": (_lines(60), frozenset(range(1, 26)), 3),
}


@pytest.mark.parametrize("name", sorted(FULL_CASES))
def test_full_matches_legacy(name, profile):
    lines = FULL_CASES[name]
    cfg = _cfg()
    ctx = FileContext(
        file_path="a.py", lines=tuple(lines), valid_lines=frozenset({1})
    )
    assert _build(ctx, "full", cfg, profile) == _legacy_full("a.py", lines)


@pytest.mark.parametrize("name", sorted(PARTIAL_CASES))
def test_partial_matches_legacy(name, profile):
    lines, valid = PARTIAL_CASES[name]
    cfg = _cfg()
    ctx = FileContext(file_path="a.py", lines=tuple(lines), valid_lines=valid)
    assert _build(ctx, "partial", cfg, profile) == _legacy_partial(
        "a.py", set(valid), lines, 20, 20
    )


@pytest.mark.parametrize("name", sorted(PATCH_CASES))
def test_patch_matches_legacy(name, profile):
    lines, valid, n = PATCH_CASES[name]
    cfg = _cfg(patch=n)
    ctx = FileContext(file_path="a.py", lines=tuple(lines), valid_lines=valid)
    assert _build(ctx, "patch", cfg, profile) == _legacy_patch(
        "a.py", set(valid), lines, n
    )


def test_patch_segment_cap_reports_omitted(profile):
    lines = _lines(200)
    valid = frozenset({10, 40, 70, 100, 130, 160})
    out = _build(
        FileContext(file_path="a.py", lines=tuple(lines), valid_lines=valid),
        "patch",
        _cfg(patch=3),
        profile,
    )
    assert "前 5 段" in out
    assert "(另有 1 段未展示)" in out
    assert " 160| line 160" not in out


def test_patch_display_cap_suffix(profile):
    lines = _lines(60)
    out = _build(
        FileContext(
            file_path="a.py", lines=tuple(lines), valid_lines=frozenset(range(1, 26))
        ),
        "patch",
        _cfg(patch=3),
        profile,
    )
    assert "[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]..." in out


def test_full_truncation_marker(profile):
    out = _build(
        FileContext(
            file_path="a.py",
            lines=tuple(_lines(5001)),
            valid_lines=frozenset({5001}),
        ),
        "full",
        _cfg(),
        profile,
    )
    assert "共 5001 行, **已截断到前 5000 行**" in out
    assert "末尾 1 行未加载" in out
    assert "5000| line 5000" in out
    assert "5001|" not in out


def test_partial_merges_adjacent_ranges(profile):
    out = _build(
        FileContext(
            file_path="a.py",
            lines=tuple(_lines(100)),
            valid_lines=frozenset({10, 12}),
        ),
        "partial",
        _cfg(),
        profile,
    )
    assert "仅显示 diff 附近 1 段" in out
    assert "   1| line 1" in out
    assert "  32| line 32" in out
    assert "\n...\n" not in out


def test_partial_separates_far_ranges(profile):
    out = _build(
        FileContext(
            file_path="a.py",
            lines=tuple(_lines(100)),
            valid_lines=frozenset({10, 60}),
        ),
        "partial",
        _cfg(),
        profile,
    )
    assert "仅显示 diff 附近 2 段" in out
    assert "\n...\n" in out


def test_cross_file_section_empty(profile):
    out = context.render_cross_file_section([], profile=profile)
    assert out.startswith("## 🔴 优先 1")
    assert "未发现 cross-file 关联" in out
    assert out.endswith("\n\n")


def test_cross_file_section_refs(profile):
    refs = [
        {"file": "tests/test_a.py", "line": 12, "ident": "f", "content": "x = f()"},
        {"file": "app.py", "line": 3, "ident": "f", "content": "f(2)"},
    ]
    out = context.render_cross_file_section(refs, profile=profile)
    assert "Python 端已 grep" in out
    assert "找到 2 条" in out
    assert (
        "**`tests/test_a.py:12`** 引用 `f`:\n```\nx = f()\n```\n\n"
        "**`app.py:3`** 引用 `f`:\n```\nf(2)\n```\n\n"
        "**请逐一分析"
    ) in out
    assert out.endswith("\n\n")


def test_unknown_strategy_falls_back_to_patch(profile):
    """未知 strategy 与旧实现一致：走 patch 分支（门面负责抛错前已校验）."""
    ctx = FileContext(file_path="a.py", lines=tuple(_lines(10)), valid_lines=frozenset({5}))
    cfg = _cfg(patch=3)
    assert _build(ctx, "bogus", cfg, profile) == _legacy_patch(
        "a.py", {5}, _lines(10), 3
    )
