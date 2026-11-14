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
from unittest.mock import MagicMock, patch

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


def test_refresh_needed_when_published_is_stale() -> None:
    # 迁移前口径 23 → 迁移后 30: 图1 落后于图2, 必须重刷
    published = {"total": 23, "has_head": True, "inline_count": 12}
    assert ro._needs_refresh(30, published) is True


def test_no_refresh_when_consistent() -> None:
    published = {"total": 30, "has_head": True, "inline_count": 12}
    assert ro._needs_refresh(30, published) is False


def test_no_refresh_when_no_published_note() -> None:
    """没有已发布汇总 → 不该「刷新」（那属于首次发布，由 improve 流程负责）."""
    assert ro._needs_refresh(30, None) is False


# ---------------------------------------------------------------------------
# D2: 缺 HEAD 行也算「对不上」
#
# Why: 状态迁移路径 (/dismiss /adopt /resolve /auto_detect) 刷新汇总时不传
# head_sha → 底部 "HEAD <短码>" 整行消失. 存量 52 个有汇总 note 的 MR 里
# 19 个已中招. 旧判定只看「总建议数」, 这 19 个的总数是一致的 → 永远不会被
# 运维脚本挑出来修 (实测修复比例 0/19).
# ---------------------------------------------------------------------------

_FULL_BODY = (
    "## 检视汇总（总建议数 10，采纳率 20.0%）\n\n"
    "| 严重度 | ⏳ 待处理 | ✅ 已采纳 | ❌ 已忽略 | 🔒 已关闭（未分类） | 合计 |\n"
    "|:---:|:---:|:---:|:---:|:---:|:---:|\n"
    "| 🔴 HIGH | 2 | 0 | 1 | 0 | 3 |\n"
    "\n"
    "🆕 **最后新增 7 条** · ⏱ 2026-11-14 11:24:11 CST · HEAD 949f9c8\n"
)

_BODY_NO_HEAD = (
    "## 检视汇总（总建议数 10，采纳率 20.0%）\n\n"
    "| 严重度 | ⏳ 待处理 | ✅ 已采纳 | ❌ 已忽略 | 🔒 已关闭（未分类） | 合计 |\n"
    "|:---:|:---:|:---:|:---:|:---:|:---:|\n"
    "| 🔴 HIGH | 2 | 0 | 1 | 0 | 3 |\n"
    "\n"
    "🆕 **最后新增 0 条** · ⏱ 2026-11-14 11:24:11 CST\n"
)


def test_published_meta_reads_total_head_and_inline_count() -> None:
    meta = ro._published_meta_from_body(_FULL_BODY)
    assert meta["total"] == 10
    assert meta["has_head"] is True
    assert meta["inline_count"] == 7


def test_published_meta_detects_missing_head() -> None:
    meta = ro._published_meta_from_body(_BODY_NO_HEAD)
    assert meta["total"] == 10
    assert meta["has_head"] is False
    assert meta["inline_count"] == 0


def test_needs_refresh_when_head_missing_even_if_total_consistent() -> None:
    """总数一致但缺 HEAD 行 → 仍需重刷 (否则存量 19 个 MR 永远修不好)."""
    published = ro._published_meta_from_body(_BODY_NO_HEAD)
    assert ro._needs_refresh(10, published) is True


def test_no_refresh_when_total_and_head_both_consistent() -> None:
    published = ro._published_meta_from_body(_FULL_BODY)
    assert ro._needs_refresh(10, published) is False


def test_apply_preserves_published_inline_count() -> None:
    """回填不能把「最后新增 N 条」归零.

    Why: 「最后新增 N 条」是用户判断这轮检视新增了几条的唯一线索.
    运维脚本原来固定传 inline_posted_count=0 → 每刷一次就把这个线索抹掉
    (F2). 回填时必须原样保留已发布的数值.
    """
    published = ro._published_meta_from_body(_FULL_BODY)
    assert ro._refresh_inline_count(published) == 7


def test_refresh_inline_count_defaults_to_zero() -> None:
    assert ro._refresh_inline_count(None) == 0
    assert ro._refresh_inline_count({"total": 10, "has_head": True, "inline_count": None}) == 0


def test_main_apply_passes_preserved_inline_count(tmp_path) -> None:
    """端到端: main(--apply) 调 publish_overview 时带上已发布的 inline_count.

    场景 = 已发布总数陈旧 (23) 需回填, 且正文里带着「最后新增 7 条」.
    回填后这个数字必须还在 —— 它是用户判断这轮检视新增了几条的唯一线索.
    """
    stale = ro._published_meta_from_body(
        "## 检视汇总（总建议数 23，采纳率 8.7%）\n\n"
        "| 严重度 | ... |\n\n"
        "🆕 **最后新增 7 条** · ⏱ 2026-11-14 11:24:11 CST · HEAD 949f9c8\n"
    )
    mock_pub = MagicMock(return_value=4242)

    with patch.object(ro, "_mr_pairs", return_value=[(34, 263)]), \
         patch.object(ro, "_db_total", return_value=30), \
         patch.object(ro, "_published_meta", return_value=stale), \
         patch("reviewagent.commands._common.publish_overview", mock_pub), \
         patch.object(sys, "argv", ["refresh_overviews.py", "--apply"]):
        rc = ro.main()

    assert rc == 0
    mock_pub.assert_called_once()
    kwargs = mock_pub.call_args.kwargs
    assert kwargs["inline_posted_count"] == 7, "回填必须保留已发布的「最后新增 N 条」"
    assert kwargs["run_late_detect"] is False


def test_main_apply_refreshes_when_head_missing(tmp_path) -> None:
    """端到端: 缺 HEAD 行 (总数却一致) 也必须被挑出来回填."""
    missing_head = ro._published_meta_from_body(_BODY_NO_HEAD)
    mock_pub = MagicMock(return_value=4242)

    with patch.object(ro, "_mr_pairs", return_value=[(34, 263)]), \
         patch.object(ro, "_db_total", return_value=10), \
         patch.object(ro, "_published_meta", return_value=missing_head), \
         patch("reviewagent.commands._common.publish_overview", mock_pub), \
         patch.object(sys, "argv", ["refresh_overviews.py", "--apply"]):
        rc = ro.main()

    assert rc == 0
    mock_pub.assert_called_once()
