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
from loguru import logger as loguru_logger

from reviewagent.review.dynamic.providers import cross_file_refs as mod
from reviewagent.review.dynamic.providers.cross_file_refs import (
    DEFAULT_EXCLUDES,
    CrossFileRefsProvider,
    collect_refs,
    find_refs,
)
from reviewagent.review.policies.python import extract_identifiers as _py_idents
from reviewagent.scenario.dynamic import load_dynamic_rules
from reviewagent.scenario.profile import Profile, load_profile


@pytest.fixture
def logs():
    """捕获 loguru 消息（provider 的非法参数告警）."""
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


def test_provider_has_no_find_refs_method():
    """R1：单文件路径直接调模块函数，provider 不再暴露 find_refs（死方法已删）."""
    assert not hasattr(CrossFileRefsProvider, "find_refs")
    assert callable(mod.find_refs)


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


def test_collect_refs_argv_uses_params_and_profile_excludes(monkeypatch):
    calls = _patch_run(monkeypatch, stdout="")
    excludes = ["!reviewagent/**", "!tests/**", "!.venv/**"]
    params = {
        "rg_file_type": "py",
        "max_symbols_per_file": 1,
        "max_refs_per_file": 3,
        "hits_per_symbol": 2,
        "timeout_seconds": 35,
        "excludes": excludes,
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
    assert _g_values(argv) == excludes
    assert calls[0]["kwargs"]["timeout"] == 30
    assert calls[0]["kwargs"]["cwd"] == "/tmp/wt"

    # limits 保留但不消费：去掉 limits 后 argv 必须完全相同。
    calls_again = _patch_run(monkeypatch, stdout="")
    collect_refs(
        ["a.py"], {"a.py": _DIFF_TWO_IDENTS}, "/tmp/wt",
        params={k: v for k, v in params.items() if k != "limits"},
    )
    assert calls_again[0]["argv"] == argv


def test_collect_refs_default_has_no_type_or_exclude_flags(monkeypatch):
    """共享层默认语言中立：不注入 `-t py`，也不注入任何 `-g` 排除项."""
    assert DEFAULT_EXCLUDES == ()
    calls = _patch_run(monkeypatch, stdout="")
    collect_refs(["a.py"], {"a.py": _DIFF_TWO_IDENTS}, "/tmp/wt", params={})
    argv = calls[0]["argv"]
    assert "-t" not in argv
    assert _g_values(argv) == []


def test_collect_refs_profile_excludes_drive_rg(monkeypatch):
    """默认 profile 的 dynamic_rules 排除项按场景声明，逐字传给 rg."""
    from reviewagent.scenario.dynamic import load_dynamic_rules
    from reviewagent.scenario.profile import load_profile

    params = dict(load_dynamic_rules(load_profile(None)).rules[0].params)
    assert params["excludes"] == [
        "!reviewagent/**",
        "!tests/**",
        "!docs/**",
        "!tmp_scripts/**",
        "!settings/**",
        "!.venv/**",
        "!**/__pycache__/**",
        "!.git/**",
    ]
    calls = _patch_run(monkeypatch, stdout="")
    collect_refs(["a.py"], {"a.py": _DIFF_TWO_IDENTS}, "/tmp/wt", params=params)
    argv = calls[0]["argv"]
    assert _g_values(argv) == params["excludes"]
    assert argv[argv.index("-t") + 1] == "py"


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
        # 生产路径由门面注入当前策略提取器；这里显式注入 Python 版。
        extract_symbols=_py_idents,
    )
    # 每个 ident 只取 1 条命中。
    assert refs == [{"file": "b.py", "line": 3, "content": "alpha()", "ident": "alpha"}]
    argv = calls[0]["argv"]
    assert _g_values(argv) == ["!a.py"]
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
# params 数值校验（<=0 / 非数值 → clamp 到默认并告警）
# ---------------------------------------------------------------------------


def test_collect_refs_clamps_non_positive_int_params(monkeypatch, logs):
    """hits_per_symbol=0 / max_refs_per_file=-1 必须 clamp 到 legacy 默认.

    未 clamp 时 `hits >= 0` 立即成立 → 每个 ident 只留 1 条（静默退化）。
    """
    stdout = "b.py:1:alpha()\nc.py:2:alpha()\nd.py:3:alpha()\n"
    _patch_run(monkeypatch, stdout)
    out = collect_refs(
        ["a.py"], {"a.py": "+def alpha():\n"}, "/tmp/wt",
        params={
            "hits_per_symbol": 0,
            "max_refs_per_file": -1,
            "rule_key": "DYN-TEST",
        },
    )
    # 默认 hits=2 → 只保留前两条；max_refs=-1 未变成 1 条。
    assert [r["file"] for r in out["a.py"]] == ["b.py", "c.py"]
    invalid = [m for m in logs if "dynamic.param.invalid" in m]
    assert any("param=hits_per_symbol value=0 -> 2" in m for m in invalid)
    assert any("param=max_refs_per_file value=-1 -> 6" in m for m in invalid)
    assert all("rule=DYN-TEST" in m for m in invalid)


def test_collect_refs_clamps_non_numeric_int_params(monkeypatch, logs):
    stdout = "b.py:1:alpha()\n"
    _patch_run(monkeypatch, stdout)
    out = collect_refs(
        ["a.py"], {"a.py": "+def alpha():\n"}, "/tmp/wt",
        params={"hits_per_symbol": "abc", "max_refs_per_file": None},
    )
    assert [r["file"] for r in out["a.py"]] == ["b.py"]
    invalid = [m for m in logs if "dynamic.param.invalid" in m]
    assert any("param=hits_per_symbol value=abc -> 2" in m for m in invalid)
    assert any("param=max_refs_per_file value=None -> 6" in m for m in invalid)


def test_find_refs_clamps_non_positive_hits(monkeypatch, logs):
    stdout = "b.py:3:alpha()\nc.py:4:alpha()\nd.py:5:alpha()\n"
    _patch_run(monkeypatch, stdout)
    refs = find_refs(
        "a.py", "+def alpha():", "/tmp/wt",
        params={"hits_per_symbol": -2, "max_refs_per_file": 0},
        extract_symbols=_py_idents,
    )
    # 默认 hits=2 / max_refs=6 → 两条命中，而不是 1 条或空。
    assert [r["file"] for r in refs] == ["b.py", "c.py"]
    assert any("dynamic.param.invalid" in m for m in logs)


def test_collect_refs_clamps_infinite_param(monkeypatch, logs):
    """YAML `.inf` → `int()` 抛 OverflowError；必须 clamp 到默认而非逃逸.

    未兜底时同步门面路径（改善 → find_refs）会把 OverflowError 抛给调用方，
    动态线程内则整条规则降级。这里用 hits_per_symbol=10 让默认的
    max_refs_per_file=6 成为唯一实际上限（否则每个 ident 只留 2 条）。
    """
    stdout = "".join(f"f{i}.py:{i}:alpha()\n" for i in range(1, 10))
    _patch_run(monkeypatch, stdout)
    out = collect_refs(
        ["a.py"], {"a.py": "+def alpha():\n"}, "/tmp/wt",
        params={"hits_per_symbol": 10, "max_refs_per_file": float("inf")},
    )
    assert len(out["a.py"]) == 6  # legacy 默认值，不是 9（无上限）也不是空
    invalid = [m for m in logs if "dynamic.param.invalid" in m]
    assert any("param=max_refs_per_file value=inf -> 6" in m for m in invalid)


# ---------------------------------------------------------------------------
# 门面参数优先级：规则 max_refs_per_file vs 显式第 4 位置参数
# ---------------------------------------------------------------------------


def _facade_cmd(*, rule_max_refs: int = 2):
    """最小 ImproveCommand（跳过 __init__）+ 手搭 DynamicRuleSet."""
    from reviewagent.commands.improve import ImproveCommand
    from reviewagent.review.dynamic import DynamicRule, DynamicRuleSet

    rule = DynamicRule(
        key="DYN-CROSS-FILE-REFS",
        title="跨文件引用",
        provider="cross_file_refs",
        enabled=True,
        params={"max_refs_per_file": rule_max_refs},
    )
    cmd = ImproveCommand.__new__(ImproveCommand)
    cmd._dynamic_rules = DynamicRuleSet(rules=(rule,), fingerprint="fp:facade")
    return cmd


def _capture_collect(monkeypatch) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    def _fake_collect(files, diff_by_file, worktree, *, params=None, extract_symbols=None):
        seen["params"] = params
        seen["extract_symbols"] = extract_symbols
        return {fp: [] for fp in files}

    monkeypatch.setattr(mod, "collect_refs", _fake_collect)
    return seen


def test_facade_collect_refs_honours_rule_max_refs(monkeypatch):
    """3 个位置参数调用时，规则里的 max_refs_per_file 必须生效（不被写死 6 覆盖）."""
    seen = _capture_collect(monkeypatch)
    cmd = _facade_cmd(rule_max_refs=2)

    out = cmd._collect_cross_file_refs_for_mr(
        ["a.py"], {"a.py": "+def alpha():"}, "/tmp/wt",
    )

    assert out == {"a.py": []}
    assert seen["params"]["max_refs_per_file"] == 2


def test_facade_collect_refs_explicit_arg_wins_over_rule(monkeypatch):
    """显式第 4 位置参数优先于规则声明的值."""
    seen = _capture_collect(monkeypatch)
    cmd = _facade_cmd(rule_max_refs=2)

    cmd._collect_cross_file_refs_for_mr(
        ["a.py"], {"a.py": "+def alpha():"}, "/tmp/wt", 7,
    )

    assert seen["params"]["max_refs_per_file"] == 7


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
