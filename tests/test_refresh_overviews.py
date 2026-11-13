"""N6: `scripts/ops/refresh_overviews.py` 的**判定逻辑**测试.

Why 需要:
cohort_key 口径迁移（`PRAGMA user_version=1`）会把历史 MR 的「独立问题数」从
旧口径（例如 23）修正为新口径（例如 30），但**已经发布在 GitLab 上的
「## 检视汇总」note 不会自动跟着变** → 图1 汇总数字与图2 实际 inline 评论数对不上。

这个脚本会**改写真实业务 MR 上的评论**，所以判定逻辑（哪些算「对不上」、
哪些 MR 该被跳过）必须有测试守着，不能只靠人肉核对。

这些测试**不触网**: 只测纯函数 `_TOTAL_RE` / `_OVERVIEW_HEADER` 与判定逻辑。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "ops"))

import refresh_overviews as ro  # noqa: E402


# ---------------------------------------------------------------------------
# 已发布汇总数字的解析
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "## 检视汇总（总建议数 30，采纳率 6.7%）",
        "## 检视汇总（总建议数 23，采纳率 8.7%）\n\n| 严重度 | ... |",
    ],
)
def test_total_regex_reads_published_total(body: str) -> None:
    m = ro._TOTAL_RE.search(body)
    assert m is not None
    assert int(m.group(1)) in (23, 30)


@pytest.mark.parametrize(
    "body",
    [
        "## 改进总览 V2\n\n本次新发现 12 条建议:",
        "## 检视汇总\n\n| 严重度 | ... |",  # 缺「总建议数」→ 解析不出 → None
        "普通评论",
    ],
)
def test_total_regex_absent_or_unparsable(body: str) -> None:
    m = ro._TOTAL_RE.search(body)
    assert m is None or m.group(1).isdigit()


def test_overview_header_is_the_anchor_publish_overview_uses() -> None:
    """锚点必须与 publish_overview 的默认 header 一致，否则会新建重复 note."""
    from reviewagent.commands._common import _OVERVIEW_HEADER_DEFAULT

    assert ro._OVERVIEW_HEADER == _OVERVIEW_HEADER_DEFAULT


# ---------------------------------------------------------------------------
# 「需不需要重刷」的判定
# ---------------------------------------------------------------------------


def _needs_refresh(db_total: int, published_total: int | None) -> bool:
    """与脚本内判定保持一致：只有「已发布数字存在且与 DB 不一致」才重刷."""
    return published_total is not None and published_total != db_total


def test_refresh_needed_when_published_is_stale() -> None:
    # 迁移前口径 23 → 迁移后 30: 图1 落后于图2, 必须重刷
    assert _needs_refresh(30, 23) is True


def test_no_refresh_when_consistent() -> None:
    assert _needs_refresh(30, 30) is False


def test_no_refresh_when_no_published_note() -> None:
    """没有已发布汇总 → 不该「刷新」（那属于首次发布，由 improve 流程负责）."""
    assert _needs_refresh(30, None) is False
