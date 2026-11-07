"""cross_file_refs 标识符提取器注入契约（Task 19 fix 1）.

共享层 `review/**` 不得 import `review.policies.*`（spec §7.5.1），因此 provider
不再直接调用 `extract_identifiers`，改由门面注入当前语言策略的
`extract_symbols`；无注入时回退模块内语言中立的默认提取器。

覆盖：
* 注入的提取器决定 rg pattern（不含默认提取器会产出的符号）；
* 不注入时走模块内默认提取器（支持完整 diff 与裸 `+` 行片段）；
* 门面 `_find_cross_file_refs` / `_collect_cross_file_refs_for_mr` 把
  `self._prompt_policy().extract_symbols` 原样转发。
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from reviewagent.review.dynamic.providers import cross_file_refs as mod
from reviewagent.review.dynamic.providers.cross_file_refs import collect_refs, find_refs

_DIFF = "+def alpha():\n+    return beta_value\n"


def _patch_run(monkeypatch, stdout: str = "") -> list[list[str]]:
    """捕获 rg argv（不启动真实子进程）."""
    calls: list[list[str]] = []

    def _fake_run(argv, **kwargs):
        calls.append(list(argv))
        return SimpleNamespace(stdout=stdout, stderr="", returncode=0)

    monkeypatch.setattr(mod.subprocess, "run", _fake_run)
    return calls


def _pattern(argv: list[str]) -> str:
    """rg argv 里的 pattern（搜索路径 `.` 之前的最后一个位置参数）."""
    return argv[argv.index(".") - 1]


def test_injected_extractor_builds_rg_pattern(monkeypatch):
    calls = _patch_run(monkeypatch)

    out = collect_refs(
        ["a.py"], {"a.py": _DIFF}, "/tmp/wt",
        params={"rg_file_type": "py"},
        extract_symbols=lambda diff: ["injected_sym"],
    )

    assert out == {"a.py": []}
    assert len(calls) == 1
    pattern = _pattern(calls[0])
    assert "injected_sym" in pattern
    # 默认提取器会从同一 diff 产出 alpha / beta_value / return — 注入后不得出现。
    for default_sym in ("alpha", "beta_value", "return"):
        assert default_sym not in pattern


def test_default_extractor_used_when_not_injected(monkeypatch):
    calls = _patch_run(monkeypatch)

    collect_refs(["a.py"], {"a.py": _DIFF}, "/tmp/wt", params={"rg_file_type": "py"})

    assert len(calls) == 1
    pattern = _pattern(calls[0])
    # 语言中立默认实现：added-line 全部标识符（含 def / return 等关键字）。
    assert "alpha" in pattern
    assert "beta_value" in pattern


def test_default_extractor_handles_full_diff_and_bare_added_line(monkeypatch):
    full_diff = (
        "diff --git a/a.py b/a.py\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -0,0 +1,1 @@\n"
        "+def gamma():\n"
    )
    # 同一默认提取器既能吃裸 `+` 行片段，也能吃完整 unified diff。
    assert mod._default_extract_symbols(_DIFF) == ["alpha", "beta_value", "def", "return"]
    assert mod._default_extract_symbols("+def alpha():") == ["alpha", "def"]
    # `+++` 头行被排除，只留 added 行里的标识符。
    assert mod._default_extract_symbols(full_diff) == ["def", "gamma"]
    assert mod._default_extract_symbols("") == []

    calls = _patch_run(monkeypatch)
    find_refs("a.py", "+def alpha():", "/tmp/wt", params={})
    # 默认提取器产出 alpha + def → 每个 ident 一次 rg。
    assert len(calls) == 2
    assert any("alpha" in _pattern(argv) for argv in calls)


def _facade_cmd(extractor):
    from reviewagent.commands.improve import ImproveCommand

    cmd = ImproveCommand.__new__(ImproveCommand)
    cmd._dynamic_rules = None
    cmd._prompt_policy = lambda: SimpleNamespace(extract_symbols=extractor)
    return cmd


def test_facade_find_refs_forwards_policy_extractor(monkeypatch):
    seen: dict[str, Any] = {}

    def _fake_find(file_path, diff, worktree, *, params=None, extract_symbols=None):
        seen.update(
            file_path=file_path, diff=diff, worktree=worktree,
            params=params, extract_symbols=extract_symbols,
        )
        return []

    def _extractor(diff: str) -> list[str]:
        return ["policy_sym"]

    monkeypatch.setattr(mod, "find_refs", _fake_find)
    cmd = _facade_cmd(_extractor)

    assert cmd._find_cross_file_refs("a.py", _DIFF, "/tmp/wt") == []
    assert seen["extract_symbols"] is _extractor
    assert seen["params"]["max_symbols_per_file"] == 15


def test_facade_collect_refs_forwards_policy_extractor(monkeypatch):
    seen: dict[str, Any] = {}

    def _fake_collect(files, diff_by_file, worktree, *, params=None, extract_symbols=None):
        seen.update(
            files=files, diff_by_file=diff_by_file, worktree=worktree,
            params=params, extract_symbols=extract_symbols,
        )
        return {fp: [] for fp in files}

    def _extractor(diff: str) -> list[str]:
        return ["policy_sym"]

    monkeypatch.setattr(mod, "collect_refs", _fake_collect)
    cmd = _facade_cmd(_extractor)

    out = cmd._collect_cross_file_refs_for_mr(["a.py"], {"a.py": _DIFF}, "/tmp/wt")
    assert out == {"a.py": []}
    assert seen["extract_symbols"] is _extractor
    assert seen["params"]["max_refs_per_file"] == 6
