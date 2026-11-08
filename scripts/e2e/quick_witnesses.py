"""快速档 witness 实现：`catalog.BEHAVIOR_CHECKS` 的每个 id 一个进程内 delta 断言.

每个函数接收当前 `Config`，只改被测字段（其余保持现网值），断言行为随之变化，
返回一行可读证据。失败即抛 `AssertionError` —— 由 `tests/test_config_effectiveness.py`
参数化执行，也供 `scripts/e2e/run_e2e.py --tier fast` 复用作配置生效性门禁。
"""
from __future__ import annotations

import dataclasses
import math
import os
import re
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from scripts.e2e.catalog import BEHAVIOR_CHECKS

QUICK: dict[str, Callable[[Any], str]] = {}


def witness(check: str) -> Callable[[Callable[[Any], str]], Callable[[Any], str]]:
    def register(fn: Callable[[Any], str]) -> Callable[[Any], str]:
        if check not in BEHAVIOR_CHECKS:
            raise RuntimeError(f"witness check not registered in catalog: {check}")
        QUICK[check] = fn
        return fn

    return register


def _cfg(cfg: Any, **overrides: Any) -> Any:
    return dataclasses.replace(cfg, **overrides)


@contextmanager
def _current_config(cfg: Any) -> Iterator[None]:
    """临时替换 `reviewagent.config._current`（读取方都在调用点惰性 import）."""
    from reviewagent import config as config_mod

    saved = config_mod._current
    config_mod._current = cfg
    try:
        yield
    finally:
        config_mod._current = saved


@contextmanager
def _without_env(name: str) -> Iterator[None]:
    saved = os.environ.pop(name, None)
    try:
        yield
    finally:
        if saved is not None:
            os.environ[name] = saved


def _suggestion(
    file: str,
    *,
    line: int = 1,
    severity: str = "high",
    score: int = 60,
    rationale: str = "",
) -> dict[str, Any]:
    return {
        "file": file,
        "start_line": line,
        "severity": severity,
        "score": score,
        "header": f"h-{file}-{line}",
        "rationale": rationale,
    }


def _merge(
    cfg: Any,
    suggestions: list[dict[str, Any]],
    *,
    priorities: Mapping[str, float] | None = None,
    new_files: set[str] | None = None,
) -> list[dict[str, Any]]:
    from reviewagent.review.validation import merge_chunks

    merged = merge_chunks(
        [{"summary_md": "", "suggestions": suggestions}],
        cfg=cfg,
        score_fn=lambda s: int(s.get("score", 0)),
        file_priorities=dict(priorities) if priorities else None,
        new_files=new_files,
    )
    return list(merged["suggestions"])


class _StubPolicy:
    language = "stub"

    def test_feature_density(self, path: str, size: int) -> float:
        return 0.0

    def extract_symbols(self, diff: str) -> list[str]:
        return []


def _diff_body(file_path: str, n_added: int) -> str:
    head = [
        f"diff --git a/{file_path} b/{file_path}",
        "index 0000000..1111111 100644",
        f"--- a/{file_path}",
        f"+++ b/{file_path}",
        f"@@ -0,0 +1,{n_added} @@",
    ]
    return "\n".join(head + [f"+added line {i}" for i in range(1, n_added + 1)]) + "\n"


def _plan(
    cfg: Any,
    files: list[str],
    *,
    new_files: set[str] | None = None,
    diff_lines: int = 3,
    file_lines: int = 100,
    file_bytes: int = 500,
):
    from reviewagent.review.planner import plan

    diffs = {fp: _diff_body(fp, diff_lines) for fp in files}
    line_map = {fp: set(range(1, diff_lines + 1)) for fp in files}
    return plan(
        files,
        diffs,
        profile=SimpleNamespace(name="quick", language="stub", scoring={}, rule_keys={}),
        cfg=cfg,
        policy=_StubPolicy(),
        read_line_count=lambda _fp: file_lines,
        stat_bytes=lambda _fp: file_bytes,
        rule_block_bytes=0,
        new_files=new_files or set(),
        line_map=line_map,
    )


# ---------------------------------------------------------------------------
# validation / 建议预算
# ---------------------------------------------------------------------------


@witness("suggestion_max")
def check_suggestion_max(cfg: Any) -> str:
    base = _cfg(cfg, improve_min_score=0, improve_min_severity="", improve_max_suggestions_per_file=10)
    results = [_suggestion(f"f{i}.py", line=1) for i in range(4)]

    def kept(limit: int) -> int:
        return len(_merge(_cfg(base, improve_max_suggestions=limit), results))

    assert kept(0) == 4, "0 = 不限，应全部保留"
    assert kept(2) == 2, "上限 2 应截断到 2 条"
    assert kept(4) == 4
    return "improve_max_suggestions: 0→4条, 2→2条"


@witness("suggestion_per_file_max")
def check_suggestion_per_file_max(cfg: Any) -> str:
    base = _cfg(
        cfg,
        improve_min_score=0,
        improve_min_severity="",
        improve_max_suggestions=0,
        improve_min_suggestions_per_file=1,
    )
    results = [_suggestion("a.py", line=i) for i in range(1, 7)]

    def kept(limit: int) -> int:
        return len(_merge(_cfg(base, improve_max_suggestions_per_file=limit), results))

    assert kept(2) == 2
    assert kept(4) == 4
    return "improve_max_suggestions_per_file: 2→2条, 4→4条"


@witness("suggestion_per_file_min")
def check_suggestion_per_file_min(cfg: Any) -> str:
    base = _cfg(
        cfg,
        improve_min_score=0,
        improve_min_severity="",
        improve_max_suggestions=0,
        improve_max_suggestions_per_file=10,
    )
    results = [
        _suggestion("hi.py", line=i) for i in range(1, 7)
    ] + [_suggestion("lo.py", line=i) for i in range(1, 7)]
    priorities = {"hi.py": 1.0, "lo.py": 0.0}

    def lo_kept(floor: int) -> int:
        kept = _merge(
            _cfg(base, improve_min_suggestions_per_file=floor),
            results,
            priorities=priorities,
        )
        return sum(1 for s in kept if s["file"] == "lo.py")

    assert lo_kept(1) == 1, "低优先级文件按下限 1 条保留"
    assert lo_kept(4) == 4, "下限抬到 4 条后低优先级文件多留"
    return "improve_min_suggestions_per_file: 1→1条, 4→4条（低优先级文件）"


@witness("severity_filter")
def check_severity_filter(cfg: Any) -> str:
    base = _cfg(cfg, improve_min_score=0, improve_max_suggestions=0)
    results = [
        _suggestion("high.py", severity="high"),
        _suggestion("med.py", severity="medium"),
        _suggestion("low.py", severity="low"),
        _suggestion("exempt.py", severity="low", rationale="见 SSD-RULE-NAMING"),
    ]

    assert len(_merge(_cfg(base, improve_min_severity=""), results)) == 4
    kept = _merge(_cfg(base, improve_min_severity="high"), results)
    files = {s["file"] for s in kept}
    assert files == {"high.py", "exempt.py"}, files
    return "improve_min_severity=high: 4→2条（low 被过滤，SSD-RULE 引用豁免）"


@witness("score_filter")
def check_score_filter(cfg: Any) -> str:
    base = _cfg(cfg, improve_min_severity="", improve_max_suggestions=0)
    results = [
        _suggestion("s90.py", score=90),
        _suggestion("s50.py", score=50),
        _suggestion("s10.py", score=10),
        _suggestion("exempt.py", score=0, rationale="见 SSD-RULE-NAMING"),
    ]

    assert len(_merge(_cfg(base, improve_min_score=0), results)) == 4
    kept = _merge(_cfg(base, improve_min_score=50), results)
    files = {s["file"] for s in kept}
    assert files == {"s90.py", "s50.py", "exempt.py"}, files
    return "improve_min_score=50: 4→3条（score<50 被过滤，SSD-RULE 引用豁免）"


# ---------------------------------------------------------------------------
# 规划：过滤 / 抽样 / 上限 / 策略 / 预算 / 权重 / 批次
# ---------------------------------------------------------------------------


@witness("test_sampling")
def check_test_sampling(cfg: Any) -> str:
    from reviewagent.review.planner import sample_case_files

    files = [f"testcase/test_{i}.py" for i in range(6)] + ["src/app.py"]
    kept_all, unsampled_all = sample_case_files(files, (), 0)
    assert kept_all == files and unsampled_all == []

    kept, unsampled = sample_case_files(files, ("testcase/",), 2)
    assert len(unsampled) == 4 and len(kept) == 3, (kept, unsampled)
    again, _ = sample_case_files(files, ("testcase/",), 2)
    assert again == kept, "抽样必须确定性（路径 md5 排序）"
    return "improve_test_sample_paths/max: 6 个 case 文件抽 2 个（确定性），未命中路径不抽样"


@witness("file_count_cap")
def check_file_count_cap(cfg: Any) -> str:
    files = [f"src/mod{i}.py" for i in range(5)] + ["core/keep.py"]
    base = _cfg(cfg, improve_keyword_paths=("/core/",))

    uncapped = _plan(_cfg(base, improve_max_files_to_review=0), files, new_files={"src/mod0.py"})
    capped = _plan(_cfg(base, improve_max_files_to_review=2), files, new_files={"src/mod0.py"})

    assert len(uncapped.files_to_review) == 6
    assert set(capped.files_to_review) == {"core/keep.py", "src/mod0.py"}, capped.files_to_review
    assert len(capped.cap_truncated) == 4
    return "improve_max_files_to_review=2: 6→2（必保新增 + 关键路径）"


@witness("file_strategy_full_quota")
def check_file_strategy_full_quota(cfg: Any) -> str:
    from reviewagent.review.planner import assign_strategy

    files = ["a.py", "b.py", "c.py"]
    meta = {fp: (5, 500) for fp in files}
    kwargs = dict(
        stat_bytes=lambda _fp: 1000,
        budget_fn=lambda _n: 100_000,
    )

    def counts(quota: int) -> dict[str, int]:
        strategy = assign_strategy(
            files, meta, (), cfg=_cfg(cfg, improve_full_files=quota), **kwargs
        )
        out: dict[str, int] = {}
        for value in strategy.values():
            out[value] = out.get(value, 0) + 1
        return out

    assert counts(1) == {"full": 1, "partial": 2}
    assert counts(0) == {"partial": 3}
    assert counts(3) == {"full": 3}
    return "improve_full_files: 1→full×1, 0→full×0, 3→full×3"


@witness("full_source_budget")
def check_full_source_budget(cfg: Any) -> str:
    from reviewagent.review.planner import _MAX_ARG_STRLEN, full_source_budget_bytes

    fixed = full_source_budget_bytes(
        100, cfg=_cfg(cfg, improve_full_source_max_bytes=100_000), rule_bytes=1000
    )
    adaptive = full_source_budget_bytes(
        100, cfg=_cfg(cfg, improve_full_source_max_bytes=0), rule_bytes=1000
    )
    closed = full_source_budget_bytes(
        100, cfg=_cfg(cfg, improve_full_source_max_bytes=-1), rule_bytes=1000
    )
    clipped = full_source_budget_bytes(
        100,
        cfg=_cfg(cfg, improve_full_source_max_bytes=0, improve_clip_diff_lines=10),
        rule_bytes=1000,
    )

    assert fixed == 100_000
    assert closed == _MAX_ARG_STRLEN
    assert 0 < adaptive < _MAX_ARG_STRLEN
    assert clipped - adaptive == (100 - 10) * 64, (clipped, adaptive)
    return f"improve_full_source_max_bytes: 固定={fixed}, 自适应={adaptive}, 关闭={closed}; clip 10→800 预算 +5760"


@witness("priority_weights")
def check_priority_weights(cfg: Any) -> str:
    from reviewagent.review.planner import priority_score

    base = _cfg(
        cfg,
        improve_keyword_paths=(),
        improve_priority_weight_test_feature=0,
        improve_new_file_full=False,
    )
    diff_only = priority_score("a.py", diff_size=100, file_size=10_000, cfg=base)
    assert diff_only == math.log1p(100) * base.improve_priority_weight_diff

    zero_diff = priority_score(
        "a.py", diff_size=100, file_size=10_000, cfg=_cfg(base, improve_priority_weight_diff=0)
    )
    assert zero_diff == 0.0

    kw_on = priority_score(
        "src/core/x.py", diff_size=0, file_size=10_000,
        cfg=_cfg(base, improve_keyword_paths=("/core/",)),
    )
    kw_off = priority_score(
        "src/core/x.py", diff_size=0, file_size=10_000,
        cfg=_cfg(base, improve_keyword_paths=("/api/",)),
    )
    assert kw_on == base.improve_priority_weight_keyword and kw_off == 0.0

    dense = priority_score("a.py", diff_size=60, file_size=100, cfg=base)
    sparse = priority_score("a.py", diff_size=10, file_size=1000, cfg=base)
    expected = base.improve_priority_weight_density + (
        math.log1p(60) - math.log1p(10)
    ) * base.improve_priority_weight_diff
    assert math.isclose(dense - sparse, expected, rel_tol=1e-12)

    feature_on = priority_score(
        "a.py", diff_size=0, file_size=10_000, cfg=_cfg(base, improve_priority_weight_test_feature=15),
        test_density=0.5,
    )
    assert feature_on == 7.5

    new_on = priority_score(
        "a.py", diff_size=0, file_size=10_000, cfg=_cfg(base, improve_new_file_full=True), is_new=True
    )
    new_off = priority_score(
        "a.py", diff_size=0, file_size=10_000, cfg=base, is_new=True
    )
    assert new_on == base.improve_priority_weight_keyword and new_off == 0.0
    return "五路权重各自可独立开关：diff/keyword/density/test_feature/new_file"


@witness("test_feature_keywords")
def check_test_feature_keywords(cfg: Any) -> str:
    from reviewagent.review.policies.python import PythonPolicy

    lines = ["def test_x():", "    assert value == 1", "CUSTOM_MARKER setup"]
    size = len(lines)
    default_policy = PythonPolicy(profile=None, cfg=cfg, read_lines=lambda _p: lines)
    custom_policy = PythonPolicy(
        profile=None,
        cfg=_cfg(cfg, improve_test_feature_keywords=("CUSTOM_MARKER",)),
        read_lines=lambda _p: lines,
    )
    default_hits = default_policy.test_feature_density("t.py", size)
    only_marker = PythonPolicy(
        profile=None,
        cfg=_cfg(cfg, improve_test_feature_keywords=("CUSTOM_MARKER",)),
        read_lines=lambda _p: ["plain line"],
    ).test_feature_density("t.py", 1)
    custom_hits = custom_policy.test_feature_density("t.py", size)

    assert default_hits > 0
    assert only_marker == 0.0
    assert custom_hits > 0
    return f"improve_test_feature_keywords: 默认密度={default_hits:.2f}, 自定义关键词命中={custom_hits:.2f}"


@witness("plan_filters")
def check_plan_filters(cfg: Any) -> str:
    base = _cfg(cfg, improve_review_mode="business", improve_test_sample_paths=())
    excluded = _plan(base, ["pkg/app.py", "docs/readme.md"])
    assert excluded.files == ("pkg/app.py",), excluded.files
    flipped = _plan(_cfg(base, review_exclude_extensions=(".py",)), ["pkg/app.py", "docs/readme.md"])
    assert flipped.files == ("docs/readme.md",), flipped.files

    skipped = _plan(base, ["src/app.py", "tests/test_app.py"])
    assert skipped.files == ("src/app.py",) and skipped.test_skipped == ("tests/test_app.py",)
    no_skip = _plan(_cfg(base, improve_skip_test_paths=()), ["src/app.py", "tests/test_app.py"])
    assert set(no_skip.files) == {"src/app.py", "tests/test_app.py"} and no_skip.test_skipped == ()

    tests = ["tests/test_a.py", "tests/test_b.py", "tests/test_c.py", "src/app.py"]
    auto_test = _plan(_cfg(base, improve_review_mode="auto"), tests)
    auto_biz = _plan(
        _cfg(base, improve_review_mode="auto"),
        ["tests/test_a.py", "src/a.py", "src/b.py", "src/c.py"],
    )
    forced = _plan(_cfg(base, improve_review_mode="business"), tests)
    assert auto_test.review_mode == "test"
    assert auto_biz.review_mode == "business"
    assert forced.review_mode == "business"
    return "扩展名过滤 / 测试路径跳过 / review_mode auto↔business 均由配置驱动"


@witness("batch_grouping")
def check_batch_grouping(cfg: Any) -> str:
    from reviewagent.review.planner import group_files_for_batching

    files = ["a.py", "b.py", "c.py", "d.py"]
    diff = {fp: "x\ny\n" for fp in files}
    strategy = {fp: "partial" for fp in files}

    def groups(**kwargs: int) -> list[list[str]]:
        return group_files_for_batching(files, diff, strategy, **kwargs)

    by_files = groups(max_diff_lines=60, max_files=2, max_total_lines=300)
    assert len(by_files) == 2 and all(len(g) == 2 for g in by_files), by_files
    by_total = groups(max_diff_lines=60, max_files=10, max_total_lines=3)
    assert len(by_total) == 4 and all(len(g) == 1 for g in by_total), by_total
    by_diff = groups(max_diff_lines=1, max_files=10, max_total_lines=300)
    assert len(by_diff) == 4, by_diff
    one = groups(max_diff_lines=60, max_files=10, max_total_lines=300)
    assert len(one) == 1 and len(one[0]) == 4, one

    base = _cfg(cfg, improve_batch_max_diff_lines=60, improve_batch_max_files=5,
                improve_batch_max_total_lines=300)
    on = _plan(_cfg(base, improve_batch_enabled=True), ["a.py", "b.py"], file_lines=5000, file_bytes=200_000)
    off = _plan(_cfg(base, improve_batch_enabled=False), ["a.py", "b.py"], file_lines=5000, file_bytes=200_000)
    assert on.batches == (("a.py", "b.py"),), on.batches
    assert off.batches == (("b.py",), ("a.py",)) or set(off.batches) == {("a.py",), ("b.py",)}
    return "improve_batch_*: 4 文件按 max_files=2 合 2 批；enabled=False 逐文件独立"


@witness("source_context_lines")
def check_source_context_lines(cfg: Any) -> str:
    from reviewagent.review import context
    from reviewagent.review.policies.python import PythonPolicy
    from reviewagent.review.types import FileContext
    from reviewagent.scenario.profile import load_profile

    profile = load_profile("ssd-python-autotest")
    lines = [f"line {i}" for i in range(1, 61)]
    ctx = FileContext(
        file_path="a.py",
        lines=tuple(lines),
        valid_lines=frozenset({30}),
        start_line=1,
    )
    numbered = re.compile(r"^\s*\d+\| ", re.MULTILINE)

    def count(strategy: str, field: str, n: int) -> int:
        local = _cfg(cfg, **{field: n})
        policy = PythonPolicy(profile=profile, cfg=local, read_lines=lambda _p: lines)
        block = context.build_source_block(ctx, strategy, cfg=local, policy=policy, profile=profile)
        return len(numbered.findall(block))

    assert count("partial", "improve_partial_context_lines", 1) == 3
    assert count("partial", "improve_partial_context_lines", 5) == 11
    assert count("patch", "improve_patch_context_lines", 0) == 0
    assert count("patch", "improve_patch_context_lines", 2) == 5
    return "partial ±1→3 行, ±5→11 行; patch ±0→0 行, ±2→5 行"


@witness("repo_context_max_lines")
def check_repo_context_max_lines(cfg: Any) -> str:
    from reviewagent.scenario.rules import repo

    class _FakeGitlab:
        def __init__(self, files: dict[str, str]) -> None:
            self.files = files

        def _get_project(self, _pid: int):
            return SimpleNamespace(default_branch="main")

        def get_file_at_sha(self, _pid: int, path: str, _ref: str):
            return self.files.get(path)

        def list_repository_tree(self, _pid: int, _path: str, _ref: str):
            return []

    content = "\n".join(f"line-{i}" for i in range(40))
    client = _FakeGitlab({"AGENTS.md": content})

    def render(max_lines: int) -> str:
        repo._files_cache.clear()
        repo._files_cache_ts.clear()
        with _current_config(_cfg(cfg, repo_context_max_lines=max_lines)):
            return repo.build_repo_context(client, 9001)

    small = render(20)
    big = render(2000)
    assert "line-39" in big, "预算充足时应带上文件尾部"
    assert "line-39" not in small, "预算不足时应截断文件尾部"
    assert len(small) < len(big)
    return f"repo_context_max_lines: 20→{len(small)}B（截断）, 2000→{len(big)}B"


@witness("rule_key_prefix")
def check_rule_key_prefix(cfg: Any) -> str:
    from reviewagent.review.validation import merge_chunks
    from reviewagent.scenario.rules import repo
    from reviewagent.scenario.rules.keys import build_key_patterns

    low = _suggestion("a.py", severity="low", rationale="见 ZZZ-RULE-NAMING")
    common = _cfg(cfg, improve_min_severity="high", improve_max_suggestions=0, improve_min_score=0)

    def kept(prefix: str) -> int:
        merged = merge_chunks(
            [{"summary_md": "", "suggestions": [low]}],
            cfg=_cfg(common, rule_key_prefix=prefix),
            score_fn=lambda s: 0,
        )
        return len(merged["suggestions"])

    assert kept("SSD") == 0, "前缀不匹配 → 不享受规则引用豁免"
    assert kept("ZZZ") == 1, "前缀匹配 → 低严重度豁免"

    patterns = build_key_patterns({"repo_prefix": "ZZZ"})
    assert patterns.extract("ZZZ-RULE-A 与 SSD-RULE-B") == ["ZZZ-RULE-A"]

    with _current_config(_cfg(cfg, rule_key_prefix="ZZZ")):
        assert repo.extract_rule_keys("ZZZ-RULE-A SSD-RULE-B") == ["ZZZ-RULE-A"]
    return "rule_key_prefix: 豁免/抽取正则随前缀切换（SSD↔ZZZ）"


@witness("weekly_timezone")
def check_weekly_timezone(cfg: Any) -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from reviewagent.reporting.runner import _resolve_tz, _week_bounds

    moment = datetime(2026, 1, 4, 16, 30, tzinfo=UTC)
    shanghai = _week_bounds(now=moment, tz=ZoneInfo("Asia/Shanghai"))
    new_york = _week_bounds(now=moment, tz=ZoneInfo("America/New_York"))
    assert shanghai[0].date().isoformat() == "2026-01-05", shanghai
    assert new_york[0].date().isoformat() == "2025-12-29", new_york
    try:
        _resolve_tz("Mars/Olympus")
    except ValueError as exc:
        assert "timezone" in str(exc)
    else:  # pragma: no cover - 防御性
        raise AssertionError("非法时区必须 fail-fast")
    return "weekly_timezone: 同一时刻 UTC+8→2026-01-05 周一, America/New_York→2025-12-29；非法时区报错"


@witness("review_config_dir")
def check_review_config_dir(cfg: Any) -> str:
    from reviewagent.scenario import profile as profile_mod

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "profiles"
        root.mkdir()
        custom = Path(tmp) / "custom-config"
        with _without_env("REVIEW_CONFIG_DIR"):
            with _current_config(_cfg(cfg, review_config_dir=str(custom))):
                assert profile_mod._resolve_config_dir(root) == custom.resolve()
            with _current_config(_cfg(cfg, review_config_dir="")):
                assert profile_mod._resolve_config_dir(root) == (root.parent / "config").resolve()
    return "review_config_dir: Config 值覆盖默认 <root>/config，空值回退默认"


def run_quick(cfg: Any = None) -> list[tuple[str, str]]:
    """执行全部快速档 witness，返回 `[(check_id, evidence)]`；失败直接抛出."""
    if cfg is None:
        from reviewagent.config import current

        cfg = current()
    missing = BEHAVIOR_CHECKS - set(QUICK)
    if missing:
        raise RuntimeError(f"missing quick witnesses: {sorted(missing)}")
    return [(name, QUICK[name](cfg)) for name in sorted(QUICK)]


__all__ = ["QUICK", "run_quick", "witness"]
