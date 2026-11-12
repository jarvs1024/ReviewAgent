"""O5: E2E 临时项目清理脚本的**匹配与保护**规则测试.

Why 需要:
`scripts/ops/cleanup_e2e_projects.py` 会**真删** GitLab 项目 —— 删错了就是
把真实业务项目 (p31 ssd-real-biz-full) 删掉，不可逆。
所以匹配规则与保护名单必须有测试守着，不能只靠人肉 review 清单。

这些测试**不触网**: 只测纯函数 `_is_e2e_project` 与常量。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "ops"))

import cleanup_e2e_projects as cep  # noqa: E402


# ---------------------------------------------------------------------------
# 应被识别为 E2E 临时项目
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "e2e-review-20261108t1132-r1",
        "e2e-review-20261111t1727-r1",
        "e2e-review-20261112t0013-r1",
        "e2e-review-probe-r99-deletion_scheduled-4",
        "probe-create-test",
    ],
)
def test_recognizes_e2e_projects(path: str) -> None:
    assert cep._is_e2e_project(path) is True, path


# ---------------------------------------------------------------------------
# 必须被保护 —— 真实业务 / 手工项目
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "ssd-real-biz-full",              # p31 真实业务项目
        "ssd-real-biz-20261110235411",   # 真实业务副本
        "ssd-review-demo",
        "ssd-dynamic-proof-1",
        "manual-review-manual-1108-1958",
        "testmate-extensions",
        "reviewagent-e2e-20261108094607",  # root 的历史遗留，不属当前用户命名模式
    ],
)
def test_protects_non_e2e_projects(path: str) -> None:
    assert cep._is_e2e_project(path) is False, path


def test_protected_name_wins_over_e2e_pattern() -> None:
    """名字里含受保护片段时, 即使前缀像 E2E 也不删（保护优先）."""
    assert cep._is_e2e_project("e2e-review-real-biz-r1") is False


# ---------------------------------------------------------------------------
# 边界
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "",                              # 空
        "e2e-review",                   # 太短
        "e2e-review-notatimestamp-r1",  # 时间戳格式不对
        "e2e-review-20261108t1132",     # 缺 -rN
        "my-e2e-review-20261108t1132-r1",  # 前缀不对
        "e2e-review-20261108t1132-r1-extra",  # 后缀多余
    ],
)
def test_rejects_malformed_names(path: str) -> None:
    """命名不完全匹配的**不删** —— 宁可漏删不可误删."""
    assert cep._is_e2e_project(path) is False, path


def test_protected_list_covers_real_biz() -> None:
    """保护名单必须包含真实业务项目的关键片段."""
    assert any("real-biz" in s for s in cep.PROTECTED_SUBSTRINGS)


@pytest.mark.parametrize(
    "fragment",
    ["real-biz", "ssd-review-demo", "ssd-dynamic-proof", "manual-review", "testmate-extensions"],
)
def test_each_protected_fragment_is_enforced(fragment: str) -> None:
    """每个保护片段都要真正生效 —— 保护名单少一条, 对应项目就会被删."""
    # 构造一个「前缀像 E2E 但含保护片段」的名字
    assert cep._is_e2e_project(f"e2e-review-{fragment}-r1") is False, fragment
    assert fragment in cep.PROTECTED_SUBSTRINGS, (
        f"保护片段 {fragment!r} 不在名单里 —— 删掉它会让对应项目被误删"
    )


def test_real_biz_full_project_is_never_deletable() -> None:
    """p31 真实业务项目名必须被明确拒绝（回归守卫）."""
    assert cep._is_e2e_project("ssd-real-biz-full") is False
