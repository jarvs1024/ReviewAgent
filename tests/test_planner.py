"""`reviewagent/review/planner.py` 回归测试.

覆盖（对应 Task 8 的规划链路）：

* 扩展名过滤 / review_mode auto / 测试路径过滤 + 确定性抽样；
* 优先级打分（log1p + keyword + density + test_feature + new_file）；
* strategy 分配顺序（full 配额 / overflow 强制 patch / 预算降级）；
* full 源码预算三态（固定 / 关闭 / 自适应）与 stat 失败；
* 文件数上限（必保新增 + 关键路径）；
* 增量复用（blob）/ 跨文件影响强制重检 / 指纹失效（profile / ruleset）；
* 批次合并 FFD / 单文件捷径预算；
* 门面薄委托（`ImproveCommand` 五个 helper）与规则块校验和守卫。

所有期望值都按旧 `ImproveCommand` 的实现重算（或直接与被委托的旧 helper 对齐），
不引入新语义.
"""
from __future__ import annotations

import dataclasses
import hashlib
import math
import re
from types import SimpleNamespace

import pytest
from loguru import logger as loguru_logger

from reviewagent.commands.improve import _MAX_ARG_STRLEN, ImproveCommand
from reviewagent.config import current
from reviewagent.review import planner
from reviewagent.review.planner import (
    apply_file_count_cap,
    assign_strategy,
    full_source_budget_bytes,
    plan,
    sample_case_files,
    single_file_over_full_budget,
)

PROFILE = SimpleNamespace(
    name="test-profile", language="fake", scoring={}, rule_keys={}, fingerprint="profile-fp",
)

# 与 diff 语法/语言无关的噪声词 — FakePolicy 抽取符号时过滤，让跨文件影响断言更精确
_NOISE = {"diff", "git", "index", "new", "file", "mode", "added", "line"}


class FakePolicy:
    """LanguagePolicy 测试替身：符号抽取用通用词法，密度查表."""

    language = "fake"

    def __init__(self, *, densities=None, symbols=None):
        self.densities = dict(densities or {})
        self.symbols = symbols
        self.density_calls: list[tuple[str, int]] = []
        self.symbol_calls: list[str] = []

    def test_feature_density(self, path: str, size: int) -> float:
        self.density_calls.append((path, size))
        return self.densities.get(path, 0.0)

    def extract_symbols(self, diff: str) -> list[str]:
        self.symbol_calls.append(diff)
        if self.symbols is not None:
            return list(self.symbols)
        words = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", diff or "")
        return [w for w in words if w.lower() not in _NOISE]


def _diff_body(file_path: str, n_added: int) -> str:
    """构造一段合法 unified diff（n_added 个 ``+`` 行）."""
    head = [
        f"diff --git a/{file_path} b/{file_path}",
        "index 0000000..1111111 100644",
        f"--- a/{file_path}",
        f"+++ b/{file_path}",
        f"@@ -0,0 +1,{n_added} @@",
    ]
    body = [f"+added line {i}" for i in range(1, n_added + 1)]
    return "\n".join(head + body) + "\n"


def _plan(
    files,
    diffs=None,
    *,
    cfg=None,
    policy=None,
    sizes=None,
    file_bytes=None,
    line_counts=None,
    **kwargs,
):
    """plan() 便捷包装：注入 profile/policy/读取器，其余透传."""
    cfg = current() if cfg is None else cfg
    policy = FakePolicy() if policy is None else policy
    sizes = dict(sizes or {})
    file_bytes = dict(file_bytes or {})
    diffs = dict(diffs or {})
    line_map = kwargs.pop("line_map", None)
    if line_map is None and line_counts is not None:
        line_map = {fp: set(range(1, n + 1)) for fp, n in line_counts.items()}
    kwargs.setdefault("read_line_count", lambda fp: sizes.get(fp, 1))
    kwargs.setdefault("stat_bytes", lambda fp: file_bytes.get(fp, -1))
    return plan(
        files,
        diffs,
        profile=PROFILE,
        cfg=cfg,
        policy=policy,
        line_map=line_map,
        **kwargs,
    )


@pytest.fixture
def logs():
    """捕获 loguru 消息（reviewagent.logging_setup.logger 就是同一实例）."""
    messages: list[str] = []
    handler_id = loguru_logger.add(
        lambda message: messages.append(str(message).rstrip("\n")),
        level="INFO",
        format="{message}",
    )
    try:
        yield messages
    finally:
        loguru_logger.remove(handler_id)


def _budget_cmd(tmp_path, repo_context: str = ""):
    """绕过 __init__ 的 ImproveCommand（门面委托测试用）."""
    cmd = ImproveCommand.__new__(ImproveCommand)
    cmd.project_id = 34
    cmd.mr_iid = 999
    cmd.ws = SimpleNamespace(worktree=tmp_path, diff_file=tmp_path / ".diff")
    cmd._last_oc_result = None
    cmd.repo_context = repo_context
    return cmd


# ---------------------------------------------------------------------------
# 1) 扩展名过滤
# ---------------------------------------------------------------------------


def test_extension_filter_drops_non_code_files(logs):
    p = _plan(
        ["pkg/mod.py", "docs/readme.md", "assets/logo.png"],
        sizes={"pkg/mod.py": 10},
    )
    assert p.files == ("pkg/mod.py",)
    assert p.files_to_review == ("pkg/mod.py",)
    assert p.skipped == {}
    assert p.review_mode == "business"
    assert any("improve.file_filter" in m and "docs/readme.md" in m for m in logs)


def test_extension_filter_all_excluded_returns_empty_plan():
    p = _plan(["docs/readme.md", "assets/logo.png"])
    assert p.files == ()
    assert p.files_to_review == ()
    assert p.review_mode == "business"
    assert p.single_file is False


# ---------------------------------------------------------------------------
# 2) review_mode auto
# ---------------------------------------------------------------------------


def test_review_mode_auto_switches_to_test(logs):
    files = ["tests/a.py", "tests/b.py", "pkg/c.py"]
    p = _plan(files, sizes={fp: 5 for fp in files})
    assert p.review_mode == "test"
    # 默认 skip paths 含 "tests/" → 测试文件仍被过滤（D 配置语义）
    assert p.files == ("pkg/c.py",)
    assert p.test_skipped == ("tests/a.py", "tests/b.py")
    assert any("improve.review_mode_auto" in m and "-> test" in m for m in logs)


def test_review_mode_auto_stays_business_below_half():
    files = ["tests/a.py", "pkg/b.py", "pkg/c.py"]
    p = _plan(files, sizes={fp: 5 for fp in files})
    assert p.review_mode == "business"
    assert p.test_skipped == ("tests/a.py",)


def test_review_mode_explicit_is_not_auto():
    cfg = dataclasses.replace(current(), improve_review_mode="business")
    p = _plan(["tests/a.py"], cfg=cfg, sizes={"tests/a.py": 5})
    assert p.review_mode == "business"
    assert p.files_to_review == ()
    assert p.test_skipped == ("tests/a.py",)
    assert p.single_file is False  # skipped 非空 → 不算单文件捷径


# ---------------------------------------------------------------------------
# 3) 测试路径过滤 + 确定性抽样
# ---------------------------------------------------------------------------


def test_test_path_sampling_deterministic_and_matches_legacy(logs):
    cfg = dataclasses.replace(
        current(),
        improve_skip_test_paths=(),
        improve_test_sample_paths=("tests/",),
        improve_test_sample_max=2,
    )
    files = [
        "tests/test_a.py", "tests/test_b.py", "tests/test_c.py",
        "tests/test_d.py", "src/main.py",
    ]
    sizes = {fp: 3 for fp in files}
    first = _plan(files, cfg=cfg, sizes=sizes)
    second = _plan(files, cfg=cfg, sizes=sizes)

    # 确定性：同输入同输出
    assert first.files == second.files
    assert first.test_skipped == second.test_skipped

    kept, unsampled = ImproveCommand._sample_case_files(sorted(files), ("tests/",), 2)
    assert list(first.files) == kept
    assert first.test_skipped == tuple(sorted(unsampled))
    assert "src/main.py" in first.files
    assert len([fp for fp in first.files if fp.startswith("tests/")]) == 2

    # 抽样顺序 = md5 升序
    case_files = sorted([fp for fp in sorted(files) if fp.startswith("tests/")])
    expected = sorted(case_files, key=lambda fp: hashlib.md5(fp.encode()).hexdigest())[:2]
    assert {fp for fp in first.files if fp.startswith("tests/")} == set(expected)
    assert any("improve.test_file_sampling" in m for m in logs)


def test_sampling_disabled_when_max_zero():
    cfg = dataclasses.replace(
        current(),
        improve_skip_test_paths=(),
        improve_test_sample_paths=("tests/",),
        improve_test_sample_max=0,
    )
    files = ["tests/a.py", "tests/b.py", "tests/c.py"]
    p = _plan(files, cfg=cfg, sizes={fp: 3 for fp in files})
    assert p.test_skipped == ()
    assert len(p.files) == 3


# ---------------------------------------------------------------------------
# 4) 优先级打分
# ---------------------------------------------------------------------------


def test_priority_weights_and_order():
    cfg = current()
    files = ["services/api.py", "utils/big.py", "newmod/fresh.py", "pkg/dense.py"]
    sizes = {
        "services/api.py": 100, "utils/big.py": 4000,
        "newmod/fresh.py": 50, "pkg/dense.py": 10,
    }
    line_counts = {
        "services/api.py": 10, "utils/big.py": 80,
        "newmod/fresh.py": 2, "pkg/dense.py": 5,
    }
    policy = FakePolicy(densities={"pkg/dense.py": 0.6})
    p = _plan(
        files, sizes=sizes, line_counts=line_counts, policy=policy,
        new_files={"newmod/fresh.py"}, cfg=cfg,
    )

    def expect(fp: str) -> float:
        diff_size, file_size = line_counts[fp], sizes[fp]
        score = math.log1p(diff_size) * cfg.improve_priority_weight_diff
        stripped = fp.lstrip("/")
        if any(kw.lstrip("/") in fp or kw in stripped for kw in cfg.improve_keyword_paths):
            score += cfg.improve_priority_weight_keyword
        if file_size > 0 and diff_size / file_size > 0.3:
            score += cfg.improve_priority_weight_density
        score += policy.densities.get(fp, 0.0) * cfg.improve_priority_weight_test_feature
        if fp == "newmod/fresh.py":
            score += cfg.improve_priority_weight_keyword
        return score

    assert p.file_priorities == {fp: pytest.approx(expect(fp)) for fp in files}
    assert list(p.files) == sorted(files, key=expect, reverse=True)
    # 关键路径 + 大改动排序: big(非关键 80 行) 压过 api(关键 10 行)
    assert list(p.files)[0] == "utils/big.py"
    # 测试特征密度参与打分（0.6 * 15 = 9）
    assert policy.density_calls and {fp for fp, _ in policy.density_calls} == set(files)


def test_new_file_bonus_can_be_disabled():
    files = ["newmod/fresh.py", "pkg/plain.py"]
    line_counts = {"newmod/fresh.py": 2, "pkg/plain.py": 2}
    sizes = {fp: 50 for fp in files}
    with_bonus = _plan(files, sizes=sizes, line_counts=line_counts, new_files={"newmod/fresh.py"})
    without = _plan(
        files, sizes=sizes, line_counts=line_counts, new_files={"newmod/fresh.py"},
        cfg=dataclasses.replace(current(), improve_new_file_full=False),
    )
    delta = current().improve_priority_weight_keyword
    assert with_bonus.file_priorities["newmod/fresh.py"] == pytest.approx(
        without.file_priorities["newmod/fresh.py"] + delta
    )
    assert with_bonus.file_priorities["pkg/plain.py"] == pytest.approx(
        without.file_priorities["pkg/plain.py"]
    )


def test_test_feature_density_not_computed_when_weight_zero():
    cfg = dataclasses.replace(current(), improve_priority_weight_test_feature=0.0)
    policy = FakePolicy(densities={"pkg/a.py": 1.0})
    p = _plan(["pkg/a.py"], cfg=cfg, sizes={"pkg/a.py": 10}, line_counts={"pkg/a.py": 1}, policy=policy)
    assert policy.density_calls == []
    assert p.file_priorities["pkg/a.py"] == pytest.approx(
        math.log1p(1) * cfg.improve_priority_weight_diff
    )


def test_line_map_derived_from_diffs_when_not_provided():
    diffs = {"pkg/a.py": _diff_body("pkg/a.py", 4)}
    p = _plan(["pkg/a.py"], diffs, sizes={"pkg/a.py": 100})
    assert p.file_priorities["pkg/a.py"] == pytest.approx(
        math.log1p(4) * current().improve_priority_weight_diff
    )


# ---------------------------------------------------------------------------
# 5) strategy 分配
# ---------------------------------------------------------------------------


def test_strategy_order_follows_priority_and_quota():
    cfg = dataclasses.replace(current(), improve_full_files=1)
    files = ["a.py", "b.py", "services/c.py"]
    sizes = {"a.py": 1500, "b.py": 3000, "services/c.py": 3000}
    p = _plan(files, cfg=cfg, sizes=sizes, line_counts={fp: 3 for fp in files})
    # services/c.py 关键路径 → 优先级最高 → 吃唯一 full 配额
    assert list(p.files)[0] == "services/c.py"
    assert p.strategies == {"services/c.py": "full", "a.py": "partial", "b.py": "patch"}


def test_full_quota_zero_never_assigns_full():
    cfg = dataclasses.replace(current(), improve_full_files=0)
    files = ["a.py", "b.py"]
    p = _plan(files, cfg=cfg, sizes={fp: 100 for fp in files}, line_counts={fp: 2 for fp in files})
    assert set(p.strategies.values()) == {"partial"}


def test_overflow_forces_patch():
    out = assign_strategy(
        ["big.py"], {"big.py": (10, 2100)}, {"big.py"},
        cfg=current(), stat_bytes=lambda p: 1, budget_fn=lambda n: 1,
    )
    assert out["big.py"] == "patch"


def test_assign_strategy_budget_downgrade_logs_project_fields(logs):
    cfg = dataclasses.replace(current(), improve_full_source_max_bytes=50_000)
    out = assign_strategy(
        ["serial.py", "core.py"], {"serial.py": (10, 2100), "core.py": (5, 5)}, set(),
        cfg=cfg,
        stat_bytes=lambda fp: 90_000 if fp == "serial.py" else 100,
        budget_fn=lambda n: full_source_budget_bytes(n, cfg=cfg, rule_bytes=0),
        project_id=34, mr_iid=999,
    )
    # 90_000 + 2100*7 = 104_700 > 50_000 → 不给 full, 且不占配额
    assert out["serial.py"] in ("partial", "patch")
    assert out["core.py"] == "full"
    assert any(
        "improve.full_source_budget_downgrade project=34 mr=999 file=serial.py" in m
        and "-> partial/patch" in m
        for m in logs
    )


def test_assign_strategy_stat_failure_keeps_full():
    out = assign_strategy(
        ["a.py"], {"a.py": (5, 100)}, set(),
        cfg=current(), stat_bytes=lambda fp: -1, budget_fn=lambda n: 1,
    )
    assert out["a.py"] == "full"


# ---------------------------------------------------------------------------
# 6) full 源码预算三态
# ---------------------------------------------------------------------------


def test_full_source_budget_three_modes():
    assert planner._MAX_ARG_STRLEN == _MAX_ARG_STRLEN == 131_072

    fixed_cfg = dataclasses.replace(current(), improve_full_source_max_bytes=40_000)
    assert full_source_budget_bytes(
        800, cfg=fixed_cfg, rule_bytes=7_416, repo_context="x" * 5_000,
    ) == 40_000

    off_cfg = dataclasses.replace(current(), improve_full_source_max_bytes=-1)
    assert full_source_budget_bytes(800, cfg=off_cfg, rule_bytes=7_416) == _MAX_ARG_STRLEN

    auto_cfg = dataclasses.replace(current(), improve_full_source_max_bytes=0)
    clip = auto_cfg.improve_clip_diff_lines

    def expect(diff_lines: int, rule_bytes: int, ctx: str) -> int:
        fixed = 8_192 + rule_bytes + len(ctx.encode())
        est = (diff_lines if clip <= 0 else min(max(diff_lines, 0), clip)) * 64
        return max(16_384, int(131_072 * 0.92) - fixed - est)

    assert full_source_budget_bytes(
        10, cfg=auto_cfg, rule_bytes=7_416, repo_context="",
    ) == expect(10, 7_416, "")
    # clip 生效: 超长 diff 按 clip 行估算
    assert full_source_budget_bytes(
        5_000, cfg=auto_cfg, rule_bytes=0, repo_context="",
    ) == expect(5_000, 0, "")

    small_ctx = full_source_budget_bytes(10, cfg=auto_cfg, rule_bytes=0, repo_context="")
    big_ctx = full_source_budget_bytes(10, cfg=auto_cfg, rule_bytes=0, repo_context="x" * 60_000)
    assert small_ctx > big_ctx, "repo_context 变大应压缩源码块预算"
    assert big_ctx >= 16_384, "自适应预算有 16KB 下限"


def test_full_source_budget_clip_disabled_uses_full_diff_lines():
    cfg = dataclasses.replace(
        current(), improve_full_source_max_bytes=0, improve_clip_diff_lines=0,
    )
    # clip=0: est_diff = diff_lines * 64 → 大 diff 直接压到下限
    assert full_source_budget_bytes(5_000, cfg=cfg, rule_bytes=0) == 16_384


# ---------------------------------------------------------------------------
# 7) 文件数上限
# ---------------------------------------------------------------------------


def test_file_count_cap_must_keep_matches_legacy():
    cfg = dataclasses.replace(current(), improve_max_files_to_review=3)
    files = ["new1.py", "services/kw.py", "opt1.py", "opt2.py", "opt3.py"]
    sizes = {fp: 5 for fp in files}
    line_counts = {
        "new1.py": 5, "services/kw.py": 4, "opt1.py": 3, "opt2.py": 2, "opt3.py": 1,
    }
    p = _plan(
        files, cfg=cfg, sizes=sizes, line_counts=line_counts, new_files={"new1.py"},
    )
    expected_kept, expected_truncated = ImproveCommand._apply_file_count_cap(
        list(p.files), {"new1.py"}, tuple(cfg.improve_keyword_paths), 3,
    )
    assert list(p.files_to_review) == expected_kept
    assert list(p.cap_truncated) == expected_truncated
    assert len(p.files_to_review) == 3
    assert "new1.py" in p.files_to_review
    assert "services/kw.py" in p.files_to_review
    assert p.cap_truncated == ("opt2.py", "opt3.py")
    assert p.skipped == {"cap": ("opt2.py", "opt3.py")}


def test_file_count_cap_disabled_when_zero():
    cfg = dataclasses.replace(current(), improve_max_files_to_review=0)
    files = [f"pkg/f{i}.py" for i in range(5)]
    p = _plan(files, cfg=cfg, sizes={fp: 5 for fp in files}, line_counts={fp: 1 for fp in files})
    assert len(p.files_to_review) == 5
    assert p.cap_truncated == ()


# ---------------------------------------------------------------------------
# 8) 增量复用（blob）
# ---------------------------------------------------------------------------


def test_blob_reuse_only_rehashes_changed_files():
    files = ["a.py", "b.py"]
    prior = {
        "a.py": {"blob_sha": "same", "head_sha": "old"},
        "b.py": {"blob_sha": "old", "head_sha": "old"},
    }
    blobs = {"a.py": "same", "b.py": "new"}
    p = _plan(
        files, sizes={fp: 5 for fp in files}, line_counts={fp: 1 for fp in files},
        prior_states=prior, head_sha="head", blob_sha=lambda fp: blobs[fp],
    )
    assert p.reused == ("a.py",)
    assert p.files_to_review == ("b.py",)
    assert p.prior_by_file == prior
    assert p.strategies["a.py"]  # 复用文件仍保留 strategy（旧行为）


def test_no_head_sha_reviews_everything():
    prior = {"a.py": {"blob_sha": "same"}}
    p = _plan(
        ["a.py"], sizes={"a.py": 5}, line_counts={"a.py": 1},
        prior_states=prior, head_sha="", blob_sha=lambda fp: "same",
    )
    assert p.files_to_review == ("a.py",)
    assert p.reused == ()


def test_no_prior_states_reviews_everything():
    p = _plan(
        ["a.py"], sizes={"a.py": 5}, line_counts={"a.py": 1},
        prior_states={}, head_sha="head", blob_sha=lambda fp: "same",
    )
    assert p.files_to_review == ("a.py",)
    assert p.reused == ()
    assert p.prior_by_file == {}


def test_file_without_prior_state_is_reviewed():
    prior = {"a.py": {"blob_sha": "same"}}
    p = _plan(
        ["a.py", "b.py"], sizes={"a.py": 5, "b.py": 5},
        line_counts={"a.py": 1, "b.py": 1},
        prior_states=prior, head_sha="head", blob_sha=lambda fp: "same",
    )
    assert p.reused == ("a.py",)
    assert p.files_to_review == ("b.py",)


# ---------------------------------------------------------------------------
# 9) 跨文件影响强制重检
# ---------------------------------------------------------------------------


def _cross_impact_files():
    return {
        "a.py": "diff --git a/a.py b/a.py\n+def declared_in_a(): pass\n",
        "b.py": "diff --git a/b.py b/b.py\n+x = declared_in_a()\n",
    }


def test_unchanged_file_forced_recheck_by_cross_impact():
    diffs = _cross_impact_files()
    prior = {"a.py": {"blob_sha": "sa"}, "b.py": {"blob_sha": "old"}}
    blobs = {"a.py": "sa", "b.py": "sb"}
    p = _plan(
        ["a.py", "b.py"], diffs,
        sizes={"a.py": 5, "b.py": 5}, line_counts={"a.py": 1, "b.py": 1},
        prior_states=prior, head_sha="head", blob_sha=lambda fp: blobs[fp],
    )
    assert p.files_to_review == ("a.py", "b.py")
    assert p.cross_reuse == ("a.py",)
    assert p.reused == ()


def test_unchanged_file_without_cross_impact_is_reused():
    diffs = {
        "a.py": "diff --git a/a.py b/a.py\n+def isolated_helper(): pass\n",
        "b.py": "diff --git a/b.py b/b.py\n+x = 1\n",
    }
    prior = {"a.py": {"blob_sha": "sa"}, "b.py": {"blob_sha": "old"}}
    blobs = {"a.py": "sa", "b.py": "sb"}
    p = _plan(
        ["a.py", "b.py"], diffs,
        sizes={"a.py": 5, "b.py": 5}, line_counts={"a.py": 1, "b.py": 1},
        prior_states=prior, head_sha="head", blob_sha=lambda fp: blobs[fp],
    )
    assert p.reused == ("a.py",)
    assert p.cross_reuse == ()
    assert p.files_to_review == ("b.py",)


# ---------------------------------------------------------------------------
# 10) 指纹失效（Task 15 预留字段）
# ---------------------------------------------------------------------------


def _fingerprint_plan(*, prior_b=("new", "r1"), **kwargs):
    """a.py 上轮指纹固定为 (old, r1)；b.py 由上轮指纹按用例给定."""
    prior = {
        "a.py": {"blob_sha": "s", "profile_fingerprint": "old", "ruleset_fingerprint": "r1"},
        "b.py": {
            "blob_sha": "s",
            "profile_fingerprint": prior_b[0],
            "ruleset_fingerprint": prior_b[1],
        },
    }
    return _plan(
        ["a.py", "b.py"], {fp: "" for fp in prior},
        sizes={"a.py": 5, "b.py": 5}, line_counts={"a.py": 1, "b.py": 1},
        prior_states=prior, head_sha="head", blob_sha=lambda fp: "s",
        project_id=34, mr_iid=999, **kwargs,
    )


def test_profile_fingerprint_invalidation(logs):
    p = _fingerprint_plan(profile_fingerprint="new", ruleset_fingerprint="r1")
    assert p.files_to_review == ("a.py",)
    assert p.reused == ("b.py",)
    invalidated = [m for m in logs if "review.state_invalidated" in m]
    assert invalidated == [
        "review.state_invalidated project=34 mr=999 file=a.py reason=profile"
    ]


def test_ruleset_fingerprint_invalidation(logs):
    p = _fingerprint_plan(
        prior_b=("old", "r2"), profile_fingerprint="old", ruleset_fingerprint="r2",
    )
    assert p.files_to_review == ("a.py",)
    assert p.reused == ("b.py",)
    invalidated = [m for m in logs if "review.state_invalidated" in m]
    assert invalidated == [
        "review.state_invalidated project=34 mr=999 file=a.py reason=ruleset"
    ]


def test_both_fingerprints_differ_reports_profile_once(logs):
    p = _fingerprint_plan(
        prior_b=("new", "r2"), profile_fingerprint="new", ruleset_fingerprint="r2",
    )
    assert p.files_to_review == ("a.py",)
    assert p.reused == ("b.py",)
    invalidated = [m for m in logs if "review.state_invalidated" in m]
    assert len(invalidated) == 1
    assert "reason=profile" in invalidated[0]


def test_none_fingerprints_keep_blob_only_behavior():
    p = _fingerprint_plan(profile_fingerprint=None, ruleset_fingerprint=None)
    assert p.reused == ("a.py", "b.py")
    assert p.files_to_review == ()


def test_missing_prior_fingerprint_keeps_blob_only_behavior():
    prior = {"a.py": {"blob_sha": "s"}}
    p = _plan(
        ["a.py"], sizes={"a.py": 5}, line_counts={"a.py": 1},
        prior_states=prior, head_sha="head", blob_sha=lambda fp: "s",
        profile_fingerprint="new", ruleset_fingerprint="r2",
    )
    assert p.reused == ("a.py",)
    assert p.files_to_review == ()


# ---------------------------------------------------------------------------
# 11) 批次合并（FFD）
# ---------------------------------------------------------------------------


def test_batching_ffd_matches_legacy():
    cfg = dataclasses.replace(
        current(),
        improve_full_files=0,
        improve_batch_max_total_lines=100,
        improve_batch_max_files=5,
        improve_batch_max_diff_lines=60,
    )
    diffs = {
        "pkg/a.py": _diff_body("pkg/a.py", 45),   # 50 行 diff
        "pkg/b.py": _diff_body("pkg/b.py", 35),   # 40 行
        "pkg/c.py": _diff_body("pkg/c.py", 25),   # 30 行
        "pkg/d.py": _diff_body("pkg/d.py", 15),   # 20 行
        "pkg/e.py": _diff_body("pkg/e.py", 5),    # 10 行
        "pkg/huge.py": _diff_body("pkg/huge.py", 60),  # 65 行 > 60 → 独立 chunk
    }
    sizes = {fp: 100 for fp in diffs}
    p = _plan(list(diffs), diffs, cfg=cfg, sizes=sizes)
    legacy = ImproveCommand._group_files_for_batching(
        list(p.files_to_review), diffs, p.strategies,
        max_diff_lines=60, max_files=5, max_total_lines=100,
    )
    assert p.batches == tuple(tuple(group) for group in legacy)
    assert [len(group) for group in p.batches] == [1, 2, 3]
    assert ("pkg/huge.py",) in p.batches


def test_batching_disabled_yields_single_file_groups():
    cfg = dataclasses.replace(current(), improve_batch_enabled=False)
    files = ["pkg/a.py", "pkg/b.py"]
    p = _plan(files, cfg=cfg, sizes={fp: 100 for fp in files}, line_counts={fp: 2 for fp in files})
    assert p.batches == (("pkg/a.py",), ("pkg/b.py",))


def test_batches_empty_when_nothing_to_review():
    prior = {"a.py": {"blob_sha": "s"}}
    p = _plan(
        ["a.py"], sizes={"a.py": 5}, line_counts={"a.py": 1},
        prior_states=prior, head_sha="head", blob_sha=lambda fp: "s",
    )
    assert p.files_to_review == ()
    assert p.batches == ()


# ---------------------------------------------------------------------------
# 12) 单文件捷径预算
# ---------------------------------------------------------------------------


def test_single_file_over_full_budget():
    budget = lambda n: 50_000  # noqa: E731
    # MR 1136: 90KB + 2100 行 → 超预算
    assert single_file_over_full_budget(
        ["serial.py"], {"serial.py": (70, 2100)},
        stat_bytes=lambda fp: 90_000, budget_fn=budget,
    ) is True
    # 预算内
    assert single_file_over_full_budget(
        ["core.py"], {"core.py": (5, 5)},
        stat_bytes=lambda fp: 100, budget_fn=budget,
    ) is False
    # 多文件不适用
    assert single_file_over_full_budget(
        ["a.py", "b.py"], {}, stat_bytes=lambda fp: 10 ** 9, budget_fn=budget,
    ) is False
    # 读不到大小 → 不拦截
    assert single_file_over_full_budget(
        ["missing.py"], {"missing.py": (5, 100)},
        stat_bytes=lambda fp: -1, budget_fn=budget,
    ) is False


def test_single_file_flag_only_when_nothing_skipped():
    one = _plan(["only.py"], sizes={"only.py": 5}, line_counts={"only.py": 1})
    assert one.single_file is True
    with_skipped = _plan(
        ["tests/test_a.py", "only.py"],
        sizes={"tests/test_a.py": 5, "only.py": 5},
        line_counts={"tests/test_a.py": 1, "only.py": 1},
    )
    assert with_skipped.files == ("only.py",)
    assert with_skipped.test_skipped == ("tests/test_a.py",)
    assert with_skipped.single_file is False


# ---------------------------------------------------------------------------
# 13) 门面薄委托 + 规则块校验和
# ---------------------------------------------------------------------------


def test_facade_sample_and_cap_delegate_to_planner():
    assert isinstance(ImproveCommand.__dict__["_sample_case_files"], staticmethod)
    assert isinstance(ImproveCommand.__dict__["_apply_file_count_cap"], staticmethod)
    files = ["tests/test_a.py", "tests/test_b.py", "src/main.py"]
    assert ImproveCommand._sample_case_files(files, ("tests/",), 1) == sample_case_files(
        files, ("tests/",), 1
    )
    cap_files = ["new1.py", "api/kw1.py", "opt1.py"]
    assert ImproveCommand._apply_file_count_cap(
        cap_files, {"new1.py"}, ("/api/",), 2,
    ) == apply_file_count_cap(cap_files, {"new1.py"}, ("/api/",), 2)


def test_facade_rule_block_text_checksum_unchanged():
    text = ImproveCommand._rule_block_text()
    assert hashlib.sha256(text.encode()).hexdigest() == (
        "84ed4563f513be9589e8ab4aa97c49d2699bc226348377455dd65a8b2fa9712c"
    )


def test_facade_budget_delegates_with_rule_block_bytes(monkeypatch, tmp_path):
    cfg = dataclasses.replace(current(), improve_full_source_max_bytes=0)
    monkeypatch.setattr("reviewagent.commands.improve.config", cfg)
    cmd = _budget_cmd(tmp_path, repo_context="x" * 100)
    rule_bytes = len(ImproveCommand._rule_block_text().encode())
    for diff_lines in (0, 10, 5_000):
        assert cmd._full_source_budget_bytes(diff_lines) == full_source_budget_bytes(
            diff_lines, cfg=cfg, rule_bytes=rule_bytes, repo_context="x" * 100,
        )


def test_facade_budget_reads_module_config_at_call_time(monkeypatch, tmp_path):
    cmd = _budget_cmd(tmp_path)
    monkeypatch.setattr(
        "reviewagent.commands.improve.config",
        dataclasses.replace(current(), improve_full_source_max_bytes=40_000),
    )
    assert cmd._full_source_budget_bytes(800) == 40_000
    monkeypatch.setattr(
        "reviewagent.commands.improve.config",
        dataclasses.replace(current(), improve_full_source_max_bytes=-1),
    )
    assert cmd._full_source_budget_bytes(800) == _MAX_ARG_STRLEN


def test_facade_assign_strategy_matches_planner(monkeypatch, tmp_path):
    cfg = dataclasses.replace(current(), improve_full_files=1)
    monkeypatch.setattr("reviewagent.commands.improve.config", cfg)
    (tmp_path / "a.py").write_bytes(b"x" * 100)
    (tmp_path / "b.py").write_bytes(b"x" * 90_000)
    cmd = _budget_cmd(tmp_path)
    meta = {"a.py": (3, 1500), "b.py": (10, 3000)}
    facade = cmd._assign_strategy(["a.py", "b.py"], meta, set())
    expected = assign_strategy(
        ["a.py", "b.py"], meta, set(), cfg=cfg,
        stat_bytes=cmd._stat_file_bytes,
        budget_fn=cmd._full_source_budget_bytes,
        project_id=34, mr_iid=999,
    )
    assert facade == expected == {"a.py": "full", "b.py": "patch"}


def test_facade_single_file_budget_logs_downgrade(monkeypatch, tmp_path, logs):
    cfg = dataclasses.replace(current(), improve_full_source_max_bytes=50_000)
    monkeypatch.setattr("reviewagent.commands.improve.config", cfg)
    (tmp_path / "serial.py").write_bytes(b"x" * 90_000)
    cmd = _budget_cmd(tmp_path)
    assert cmd._single_file_over_full_budget(["serial.py"], {"serial.py": (70, 2100)}) is True
    assert any(
        "improve.full_source_budget_downgrade project=34 mr=999 file=serial.py" in m
        and "single-file" in m
        for m in logs
    )
    assert cmd._single_file_over_full_budget(["missing.py"], {"missing.py": (5, 100)}) is False


# ---------------------------------------------------------------------------
# 14) 端到端对齐: plan() vs 旧 `_call_agent` 内联规划
# ---------------------------------------------------------------------------


def test_plan_matches_legacy_call_agent(monkeypatch, tmp_path):
    """同一输入下，plan() 的检视集合/顺序/strategy/skip 与旧 _call_agent 完全一致."""
    from reviewagent.llm.base import LLMResult

    (tmp_path / "services").mkdir()
    (tmp_path / "services" / "api.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    (tmp_path / "utils").mkdir()
    (tmp_path / "utils" / "helper.py").write_text(
        "".join(f"v{i} = {i}\n" for i in range(10)), encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_api.py").write_text("assert True\n", encoding="utf-8")

    line_map = {
        "services/api.py": {1, 2, 3, 4, 5},
        "utils/helper.py": set(range(1, 41)),
        "tests/test_api.py": {1, 2},
    }
    diffs = {
        "services/api.py": _diff_body("services/api.py", 5),
        "utils/helper.py": _diff_body("utils/helper.py", 40),
        "tests/test_api.py": _diff_body("tests/test_api.py", 2),
    }
    sizes = {"services/api.py": 2, "utils/helper.py": 10, "tests/test_api.py": 1}

    cfg = dataclasses.replace(
        current(),
        improve_full_files=1,
        improve_batch_enabled=False,
        improve_priority_weight_test_feature=0.0,
    )
    monkeypatch.setattr("reviewagent.commands.improve.config", cfg)

    ws = SimpleNamespace(worktree=tmp_path, diff_file=tmp_path / ".diff")
    cmd = _budget_cmd(tmp_path)
    cmd.ws = ws
    monkeypatch.setattr(cmd, "_diff_line_map", lambda: dict(line_map))
    monkeypatch.setattr(cmd, "_split_diff_by_file", lambda df, files: {
        fp: diffs[fp] for fp in files
    })
    monkeypatch.setattr(cmd, "_collect_cross_file_refs_for_mr", lambda files, dbf, wt: {
        fp: [] for fp in files
    })
    monkeypatch.setattr(cmd, "_get_mr_head_sha", lambda: None)
    monkeypatch.setattr(cmd, "_call_chunk", lambda prompt, w, fp: LLMResult(
        data={"summary_md": "", "suggestions": []},
        prompt_tokens=1, completion_tokens=1, model="t",
    ))
    captured: list[tuple[str, str | None]] = []
    original = cmd._build_chunk_prompt

    def _spy(fp, diff, valid_lines, ws_, cross_file_refs=None, strategy=None, prior_issues=None):
        captured.append((fp, strategy))
        return original(
            fp, diff, valid_lines, ws_,
            cross_file_refs=cross_file_refs, strategy=strategy, prior_issues=prior_issues,
        )

    monkeypatch.setattr(cmd, "_build_chunk_prompt", _spy)

    result = cmd._call_agent(ws)
    legacy_order = [fp for fp, _ in captured]
    legacy_strategy = dict(captured)

    p = _plan(
        list(line_map), diffs,
        cfg=cfg,
        sizes=sizes,
        line_map=line_map,
        stat_bytes=cmd._stat_file_bytes,
        budget_fn=cmd._full_source_budget_bytes,
    )

    assert legacy_order == list(p.files_to_review)
    assert legacy_strategy == {fp: p.strategies[fp] for fp in legacy_order}
    assert p.review_mode == "business"
    assert p.test_skipped == ("tests/test_api.py",)
    assert "tests/test_api.py" in result["summary_md"]
    # strategy 分布与优先级一致: helper 改动最大 → 吃唯一 full 配额
    assert p.strategies["utils/helper.py"] == "full"
    assert p.strategies["services/api.py"] == "partial"


# ---------------------------------------------------------------------------
# 15) 语言中立守卫
# ---------------------------------------------------------------------------


def test_planner_is_language_neutral():
    import ast
    import inspect

    src = inspect.getsource(planner)
    imports: list[str] = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
    assert not [m for m in imports if m.startswith("reviewagent.review.policies")]
    assert not [m for m in imports if m in ("os", "ast", "reviewagent.settings")]
