"""cross_file_refs provider（Task 14）：迁移后的 rg 采集与加载期模板校验.

覆盖：
* provider 扁平化 + `review_file` 标注、回调只收 3 个位置参数；
* 无回调时回退模块级 `collect_refs`，单文件 `find_refs` 委托；
* params 驱动 `-t` / excludes / hits / refs 上限，`limits` 保留但不消费；
* dynamic_rules 声明的模板文件存在性 / 路径逃逸校验。
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from reviewagent.review.dynamic.providers import cross_file_refs as mod
from reviewagent.review.dynamic.providers.cross_file_refs import (
    DEFAULT_EXCLUDES,
    CrossFileRefsProvider,
    collect_refs,
    find_refs,
)
from reviewagent.scenario.dynamic import load_dynamic_rules
from reviewagent.scenario.profile import Profile, load_profile


@pytest.fixture(scope="module")
def base_profile() -> Profile:
    return load_profile(None)


def _ref(file: str = "caller.py", line: int = 3, ident: str = "alpha") -> dict[str, Any]:
    return {"file": file, "line": line, "content": f"{ident}()", "ident": ident}


# ---------------------------------------------------------------------------
# provider：扁平化 / 标注 / 回调
# ---------------------------------------------------------------------------


def test_provider_flattens_and_annotates_review_file():
    captured: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    ref_a = _ref("caller_a.py", 3, "alpha")
    ref_b = _ref("caller_b.py", 9, "beta")

    def _callback(*args: Any, **kwargs: Any):
        captured.append((args, kwargs))
        return {"a.py": [ref_a], "b.py": [ref_b]}

    provider = CrossFileRefsProvider(_callback)
    out = provider.fetch(
        files=["a.py", "b.py"], diff_by_file={"a.py": "", "b.py": ""},
        worktree="/tmp/wt", params={"timeout_seconds": 35},
    )

    # 回调只收 3 个位置参数（files, diff_by_file, worktree）。
    assert len(captured) == 1
    args, kwargs = captured[0]
    assert args == (["a.py", "b.py"], {"a.py": "", "b.py": ""}, "/tmp/wt")
    assert kwargs == {}

    assert out == [
        {**ref_a, "review_file": "a.py"},
        {**ref_b, "review_file": "b.py"},
    ]
    # 原 dict 不被污染，legacy 键序保持不变，review_file 追加在末尾。
    assert "review_file" not in ref_a
    assert list(out[0]) == ["file", "line", "content", "ident", "review_file"]


def test_provider_falls_back_to_module_collect_refs(monkeypatch):
    seen: dict[str, Any] = {}

    def _fake_collect(files, diff_by_file, worktree, *, params=None):
        seen.update(files=files, diff_by_file=diff_by_file, worktree=worktree, params=params)
        return {"a.py": [_ref("caller.py", 5, "alpha")]}

    monkeypatch.setattr(mod, "collect_refs", _fake_collect)
    provider = CrossFileRefsProvider(None)
    out = provider.fetch(
        files=["a.py"], diff_by_file={"a.py": "+def alpha():"},
        worktree="/tmp/wt", params={"rg_file_type": "py"},
    )
    assert out == [{**_ref("caller.py", 5, "alpha"), "review_file": "a.py"}]
    assert seen["params"] == {"rg_file_type": "py"}
    assert seen["files"] == ["a.py"]


def test_provider_find_refs_delegates_to_module(monkeypatch):
    calls: list[tuple[Any, ...]] = []

    def _fake_find(file_path, diff, worktree, *, params=None):
        calls.append((file_path, diff, worktree, params))
        return [_ref("other.py", 7, "alpha")]

    monkeypatch.setattr(mod, "find_refs", _fake_find)
    provider = CrossFileRefsProvider(lambda *a, **kw: {})
    out = provider.find_refs("a.py", "+def alpha():", "/tmp/wt")
    assert out == [_ref("other.py", 7, "alpha")]
    assert calls == [("a.py", "+def alpha():", "/tmp/wt", None)]


# ---------------------------------------------------------------------------
# params → argv（monkeypatch subprocess.run，保持确定性）
# ---------------------------------------------------------------------------


_DIFF_TWO_IDENTS = (
    "diff --git a/a.py b/a.py\n"
    "--- a/a.py\n+++ b/a.py\n"
    "@@ -0,0 +1,2 @@\n"
    "+def alpha():\n"
    "+def beta():\n"
)


def _patch_run(monkeypatch, stdout: str) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def _fake_run(argv, **kwargs):
        calls.append({"argv": argv, "kwargs": kwargs})
        return SimpleNamespace(stdout=stdout, stderr="", returncode=0)

    monkeypatch.setattr(mod.subprocess, "run", _fake_run)
    return calls


def _g_values(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, arg in enumerate(argv) if arg == "-g"]


def test_collect_refs_argv_uses_params_and_legacy_excludes(monkeypatch):
    calls = _patch_run(monkeypatch, stdout="")
    params = {
        "rg_file_type": "py",
        "max_symbols_per_file": 1,
        "max_refs_per_file": 3,
        "hits_per_symbol": 2,
        "timeout_seconds": 35,
        "limits": {"max_files": 60},  # reserved：不得影响 argv
    }
    out = collect_refs(
        ["a.py"], {"a.py": _DIFF_TWO_IDENTS}, "/tmp/wt", params=params,
    )
    assert out == {"a.py": []}
    assert len(calls) == 1
    argv = calls[0]["argv"]
    assert argv[:3] == ["rg", "-n", "--no-heading"]
    assert argv[argv.index("-t") + 1] == "py"
    # max_symbols_per_file=1 → 只保留排序后的第一个 ident。
    assert any("alpha" in arg for arg in argv)
    assert not any("beta" in arg for arg in argv)
    assert _g_values(argv) == list(DEFAULT_EXCLUDES)
    assert calls[0]["kwargs"]["timeout"] == 30
    assert calls[0]["kwargs"]["cwd"] == "/tmp/wt"

    # limits 保留但不消费：去掉 limits 后 argv 必须完全相同。
    calls_again = _patch_run(monkeypatch, stdout="")
    collect_refs(
        ["a.py"], {"a.py": _DIFF_TWO_IDENTS}, "/tmp/wt",
        params={k: v for k, v in params.items() if k != "limits"},
    )
    assert calls_again[0]["argv"] == argv


def test_collect_refs_omits_type_flag_when_param_empty(monkeypatch):
    calls = _patch_run(monkeypatch, stdout="")
    collect_refs(
        ["a.py"], {"a.py": _DIFF_TWO_IDENTS}, "/tmp/wt",
        params={"rg_file_type": ""},
    )
    assert "-t" not in calls[0]["argv"]


def test_collect_refs_returns_empty_without_worktree(monkeypatch):
    calls = _patch_run(monkeypatch, stdout="")
    assert collect_refs(["a.py"], {"a.py": _DIFF_TWO_IDENTS}, None) == {"a.py": []}
    assert calls == []


def test_find_refs_argv_self_exclusion_timeout_and_hits(monkeypatch):
    stdout = "b.py:3:alpha()\nc.py:4:alpha()\nd.py:5:alpha()\n"
    calls = _patch_run(monkeypatch, stdout)
    refs = find_refs(
        "a.py", "+def alpha():", "/tmp/wt",
        params={"hits_per_symbol": 1, "max_refs_per_file": 6},
    )
    # 每个 ident 只取 1 条命中。
    assert refs == [{"file": "b.py", "line": 3, "content": "alpha()", "ident": "alpha"}]
    argv = calls[0]["argv"]
    assert _g_values(argv) == ["!a.py", *DEFAULT_EXCLUDES]
    assert calls[0]["kwargs"]["timeout"] == 8


def test_find_refs_respects_max_refs(monkeypatch):
    stdout = "b.py:1:alpha()\nc.py:2:alpha()\nd.py:3:alpha()\n"
    _patch_run(monkeypatch, stdout)
    refs = find_refs(
        "a.py", "+def alpha():", "/tmp/wt",
        params={"hits_per_symbol": 2, "max_refs_per_file": 2},
    )
    assert [r["file"] for r in refs] == ["b.py", "c.py"]


def test_collect_refs_buckets_exclude_self(monkeypatch):
    diff = "+def alpha():\n"
    stdout = "a.py:1:alpha()\nb.py:2:alpha()\n"
    _patch_run(monkeypatch, stdout)
    out = collect_refs(
        ["a.py", "b.py"], {"a.py": diff, "b.py": diff}, "/tmp/wt",
        params={"hits_per_symbol": 2, "max_refs_per_file": 6},
    )
    assert [r["file"] for r in out["a.py"]] == ["b.py"]
    assert [r["file"] for r in out["b.py"]] == ["a.py"]


# ---------------------------------------------------------------------------
# loader：模板文件存在性 / 逃逸校验
# ---------------------------------------------------------------------------


def _load_root(tmp_path: Path, base: Profile, rule: dict[str, Any]):
    root = tmp_path / "profile"
    root.mkdir(parents=True, exist_ok=True)
    (root / "rules.yaml").write_text(
        yaml.safe_dump({"schema_version": 1, "rules": [rule]}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return load_dynamic_rules(replace(base, root=root, dynamic_rules="rules.yaml"))


def _rule(**overrides: Any) -> dict[str, Any]:
    rule = {"key": "DYN-TPL", "provider": "cross_file_refs"}
    rule.update(overrides)
    return rule


@pytest.mark.parametrize("field", ["template", "empty_template", "items_template"])
def test_missing_template_fails(tmp_path, base_profile, field):
    with pytest.raises(ValueError, match=field):
        _load_root(tmp_path, base_profile, _rule(**{field: "prompts/dynamic/missing.md"}))


def test_template_path_escape_fails(tmp_path, base_profile):
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    with pytest.raises(ValueError, match="escapes profile root"):
        _load_root(tmp_path, base_profile, _rule(empty_template="../outside.md"))


def test_declared_templates_load_and_resolve(tmp_path, base_profile):
    root = tmp_path / "profile"
    dynamic = root / "prompts" / "dynamic"
    dynamic.mkdir(parents=True)
    (dynamic / "empty.md").write_text("empty", encoding="utf-8")
    (dynamic / "items.md").write_text("items", encoding="utf-8")
    ruleset = _load_root(
        tmp_path, base_profile,
        _rule(
            empty_template="prompts/dynamic/empty.md",
            items_template="prompts/dynamic/items.md",
        ),
    )
    rule = ruleset.rules[0]
    assert rule.empty_template == "prompts/dynamic/empty.md"
    assert rule.items_template == "prompts/dynamic/items.md"
    assert rule.template is None
