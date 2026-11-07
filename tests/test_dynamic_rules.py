"""动态规则（Task 13）：加载校验、provider 注册与超时降级 runner.

覆盖两条失败语义：
* 配置错误（schema / 未知字段 / 非法或重复 `DYN-*` 键 / 越出 profile 目录）
  在加载期硬失败；
* 运行期 provider 缺失、异常或超时一律降级为空证据，绝不阻塞 MR。
"""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml
from loguru import logger as loguru_logger

from reviewagent.review.dynamic import (
    DynamicEvidence,
    DynamicRule,
    DynamicRuleSet,
    register,
    run_dynamic,
)
from reviewagent.review.dynamic import get as get_provider
from reviewagent.review.dynamic import model as dynamic_model
from reviewagent.review.dynamic.runner import run_dynamic as run_dynamic_direct
from reviewagent.scenario.dynamic import load_dynamic_rules
from reviewagent.scenario.profile import Profile, load_profile

RULE_KEY = "DYN-CROSS-FILE-REFS"


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


@pytest.fixture(scope="module")
def base_profile() -> Profile:
    return load_profile(None)


@pytest.fixture(scope="module")
def declared_ruleset(base_profile: Profile) -> DynamicRuleSet:
    return load_dynamic_rules(base_profile)


def _write_yaml(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _ruleset(tmp_path: Path, base: Profile, payload: Any, *, rel: str = "dynamic_rules.yaml"):
    """在 tmp profile 目录写入 dynamic_rules 文件并加载."""
    root = tmp_path / "profile"
    _write_yaml(root / rel, payload)
    return load_dynamic_rules(replace(base, root=root, dynamic_rules=rel))


def _one_rule(**overrides: Any) -> dict[str, Any]:
    rule: dict[str, Any] = {
        "key": "DYN-ONLY",
        "title": "唯一规则",
        "provider": "some_provider",
        "enabled": True,
    }
    rule.update(overrides)
    return rule


# ---------------------------------------------------------------------------
# 模型与加载
# ---------------------------------------------------------------------------


def test_model_reexported_from_scenario_layer():
    """模型归属 scenario 数据层，review.dynamic.model 只做 re-export."""
    assert dynamic_model.DynamicRule is DynamicRule
    assert dynamic_model.DynamicEvidence is DynamicEvidence
    assert dynamic_model.DynamicRuleSet is DynamicRuleSet
    assert DynamicEvidence.__module__ == "reviewagent.scenario.dynamic"
    assert run_dynamic_direct is run_dynamic


def test_load_declared_rule(base_profile: Profile, declared_ruleset: DynamicRuleSet):
    rule = declared_ruleset.rules[0]
    assert rule.key == RULE_KEY
    assert rule.provider == "cross_file_refs"
    assert rule.enabled is True
    assert rule.title == "跨文件引用"
    # T14: 模板拆分为空 / 有引用两条，加载期校验存在性。
    assert rule.template is None
    assert rule.empty_template == "prompts/dynamic/cross_file_refs_empty.md"
    assert rule.items_template == "prompts/dynamic/cross_file_refs_refs.md"
    assert rule.params == {
        "rg_file_type": "py",
        "max_symbols_per_file": 15,
        "max_refs_per_file": 6,
        "hits_per_symbol": 2,
        "timeout_seconds": 35,
    }
    assert rule.limits == {"max_files": 60}
    assert declared_ruleset.fingerprint
    assert str((base_profile.root / "dynamic_rules.yaml").stat().st_mtime_ns) == (
        declared_ruleset.fingerprint
    )


def test_missing_dynamic_rules_file_fails(base_profile: Profile):
    bad = replace(base_profile, dynamic_rules="missing.yaml")
    with pytest.raises((ValueError, FileNotFoundError)):
        load_dynamic_rules(bad)


def test_no_dynamic_rules_declared_yields_empty_set(base_profile: Profile):
    ruleset = load_dynamic_rules(replace(base_profile, dynamic_rules=None))
    assert ruleset.rules == ()
    assert ruleset.fingerprint == ""


def test_empty_rules_list_is_declared_but_inactive(tmp_path, base_profile):
    ruleset = _ruleset(tmp_path, base_profile, {"schema_version": 1, "rules": []})
    assert ruleset.rules == ()
    assert ruleset.fingerprint  # 文件存在即有指纹


def test_profile_root_escape_rejected(tmp_path, base_profile):
    root = tmp_path / "profile"
    root.mkdir()
    outside = tmp_path / "outside.yaml"
    _write_yaml(outside, {"schema_version": 1, "rules": [_one_rule()]})
    escaped = replace(base_profile, root=root, dynamic_rules="../outside.yaml")
    with pytest.raises(ValueError, match="escapes profile root"):
        load_dynamic_rules(escaped)


def test_unsupported_schema_version_rejected(tmp_path, base_profile):
    with pytest.raises(ValueError, match="schema"):
        _ruleset(tmp_path, base_profile, {"schema_version": 2, "rules": []})


def test_non_mapping_document_rejected(tmp_path, base_profile):
    with pytest.raises(ValueError, match="schema"):
        _ruleset(tmp_path, base_profile, [{"key": "DYN-X"}])


def test_unknown_top_level_field_rejected(tmp_path, base_profile):
    with pytest.raises(ValueError, match="unknown dynamic_rules fields"):
        _ruleset(
            tmp_path,
            base_profile,
            {"schema_version": 1, "rules": [_one_rule()], "extra": True},
        )


def test_unknown_rule_field_rejected(tmp_path, base_profile):
    with pytest.raises(ValueError, match="unknown dynamic rule fields"):
        _ruleset(
            tmp_path,
            base_profile,
            {"schema_version": 1, "rules": [_one_rule(bogus=1)]},
        )


def test_missing_provider_rejected(tmp_path, base_profile):
    with pytest.raises(ValueError, match="provider"):
        _ruleset(tmp_path, base_profile, {"schema_version": 1, "rules": [{"key": "DYN-X"}]})


@pytest.mark.parametrize(
    "key",
    [
        "BAD-KEY",
        "dyn-lower",
        "DYN-",
        "DYN_a",
        pytest.param("DYN-A\n", id="trailing-newline"),
    ],
)
def test_invalid_key_format_rejected(tmp_path, base_profile, key: str):
    with pytest.raises(ValueError, match="invalid dynamic rule key"):
        _ruleset(tmp_path, base_profile, {"schema_version": 1, "rules": [_one_rule(key=key)]})


def test_duplicate_key_rejected(tmp_path, base_profile):
    payload = {
        "schema_version": 1,
        "rules": [_one_rule(key="DYN-DUP"), _one_rule(key="DYN-DUP")],
    }
    with pytest.raises(ValueError, match="duplicate dynamic rule key"):
        _ruleset(tmp_path, base_profile, payload)


def test_defaults_normalized(tmp_path, base_profile):
    ruleset = _ruleset(
        tmp_path,
        base_profile,
        {"schema_version": 1, "rules": [{"key": "DYN-MIN", "provider": "p"}]},
    )
    rule = ruleset.rules[0]
    assert rule.title == "DYN-MIN"
    assert rule.enabled is True
    assert rule.template is None
    assert rule.empty_template is None
    assert rule.items_template is None
    assert rule.params == {}
    assert rule.limits == {}


# ---------------------------------------------------------------------------
# DynamicRuleSet.with_rule
# ---------------------------------------------------------------------------


def test_with_rule_replaces_matching_rule_and_fingerprint(declared_ruleset):
    original = declared_ruleset.rules[0]
    patched = replace(original, title="替换后", params={"timeout_seconds": 1})
    updated = declared_ruleset.with_rule(patched)
    assert len(updated.rules) == 1
    assert updated.rules[0].title == "替换后"
    assert updated.rules[0].params == {"timeout_seconds": 1}
    assert updated.fingerprint != declared_ruleset.fingerprint
    assert updated.fingerprint.endswith(RULE_KEY)


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


def test_registry_register_overwrites_and_get_passes_kwargs():
    calls: list[tuple[str, dict[str, Any]]] = []

    class _First:
        def __init__(self, **kwargs: Any) -> None:
            calls.append(("first", kwargs))

        def fetch(self, **kwargs: Any):
            return ()

    class _Second:
        def __init__(self, **kwargs: Any) -> None:
            calls.append(("second", kwargs))

        def fetch(self, **kwargs: Any):
            return ()

    register("test_overwrite_provider", _First)
    # 同名再注册必须是**不同**工厂，否则无法判别"后者生效"。
    register("test_overwrite_provider", _Second)
    provider = get_provider("test_overwrite_provider", fetch_callback="cb")
    assert isinstance(provider, _Second)
    assert not isinstance(provider, _First)
    assert calls == [("second", {"fetch_callback": "cb"})]


def test_registry_unknown_provider_raises_key_error():
    with pytest.raises(KeyError, match="unknown dynamic provider: nope_not_registered"):
        get_provider("nope_not_registered")


# ---------------------------------------------------------------------------
# runner：成功 / 降级 / 超时 / 禁用
# ---------------------------------------------------------------------------


def _single_rule_run(ruleset, provider: str, *, timeout_s: float = 5, **kwargs):
    rule = replace(ruleset.rules[0], provider=provider)
    return run_dynamic(
        ruleset.with_rule(rule),
        files=["core.py"],
        diff_by_file={},
        worktree=None,
        timeout_s=timeout_s,
        **kwargs,
    )


def test_success_returns_tuple_refs(declared_ruleset):
    seen: dict[str, Any] = {}
    fetched: dict[str, Any] = {}

    class _Ok:
        def fetch(self, **kwargs: Any):
            fetched.update(kwargs)
            return [{"file": "a.py", "line": 3}]

    def _factory(**kwargs: Any) -> _Ok:
        seen.update(kwargs)
        return _Ok()

    register("test_ok_provider", _factory)
    out = _single_rule_run(declared_ruleset, "test_ok_provider", fetch_callback="cb")

    evidence = out[RULE_KEY]
    assert isinstance(evidence, DynamicEvidence)
    assert evidence.degraded is False
    assert evidence.reason == ""
    assert evidence.provider == "test_ok_provider"
    assert evidence.refs == ({"file": "a.py", "line": 3},)
    assert isinstance(evidence.refs, tuple)
    # factory 收到 fetch_callback；fetch 收到 files/diff/worktree/params。
    assert seen == {"fetch_callback": "cb"}
    assert fetched["files"] == ["core.py"]
    assert fetched["diff_by_file"] == {}
    assert fetched["worktree"] is None
    assert fetched["params"]["timeout_seconds"] == 35


class _Boom:
    def fetch(self, **kwargs: Any):
        raise TimeoutError("rg timeout")


def test_provider_failure_degrades(declared_ruleset):
    register("boom", lambda **kw: _Boom())
    out = _single_rule_run(declared_ruleset, "boom", timeout_s=1)
    evidence = out[RULE_KEY]
    assert evidence.degraded is True
    assert isinstance(evidence, DynamicEvidence)
    # provider 内部抛 TimeoutError 时线程已结束，不是 runner 的 "timeout" 判定。
    assert evidence.reason == "TimeoutError"
    assert evidence.refs == ()


def test_unknown_provider_degrades(declared_ruleset, logs):
    out = _single_rule_run(declared_ruleset, "not_registered_anywhere", timeout_s=1)
    evidence = out[RULE_KEY]
    assert evidence.degraded is True
    assert evidence.reason == "KeyError"
    assert evidence.refs == ()
    assert any("dynamic.degrade" in message for message in logs)


class _Slow:
    def fetch(self, **kwargs: Any):
        time.sleep(0.3)
        return [{"file": "late.py"}]


def test_timeout_degrades(declared_ruleset, logs):
    register("slow_provider", lambda **kw: _Slow())
    # 规则未声明 params.timeout_seconds 时才回退到调用方 timeout_s。
    rule = replace(declared_ruleset.rules[0], provider="slow_provider", params={})
    started = time.monotonic()
    out = run_dynamic(
        declared_ruleset.with_rule(rule),
        files=["core.py"],
        diff_by_file={},
        worktree=None,
        timeout_s=0.01,
    )
    elapsed = time.monotonic() - started

    evidence = out[RULE_KEY]
    assert evidence.degraded is True
    assert evidence.reason == "timeout"
    assert evidence.refs == ()
    assert elapsed < 0.3
    assert any(f"dynamic.timeout rule={RULE_KEY}" in message for message in logs)


def test_rule_param_timeout_overrides_fallback(declared_ruleset):
    register("slow_provider_2", lambda **kw: _Slow())
    rule = replace(
        declared_ruleset.rules[0],
        provider="slow_provider_2",
        params={"timeout_seconds": 0.01},
    )
    out = run_dynamic(
        declared_ruleset.with_rule(rule),
        files=["core.py"],
        diff_by_file={},
        worktree=None,
        timeout_s=30,
    )
    assert out[RULE_KEY].reason == "timeout"


@pytest.mark.parametrize(
    "bad_timeout",
    ["abc", None, {"nested": 1}],
    ids=["str", "none", "dict"],
)
def test_invalid_timeout_param_falls_back_to_caller_timeout(declared_ruleset, bad_timeout):
    """非法 params.timeout_seconds（非数值）不得抛错，回退 timeout_s 正常采集."""

    class _Fast:
        def fetch(self, **kwargs: Any):
            return [{"file": "fast.py"}]

    register("fast_provider_bad_timeout", lambda **kw: _Fast())
    rule = replace(
        declared_ruleset.rules[0],
        provider="fast_provider_bad_timeout",
        params={"timeout_seconds": bad_timeout},
    )
    out = run_dynamic(
        declared_ruleset.with_rule(rule),
        files=["core.py"],
        diff_by_file={},
        worktree=None,
        timeout_s=5,
    )
    evidence = out[RULE_KEY]
    assert evidence.degraded is False
    assert evidence.reason == ""
    assert evidence.refs == ({"file": "fast.py"},)


def test_disabled_rule_omitted_from_result(declared_ruleset):
    rule = replace(declared_ruleset.rules[0], enabled=False)
    out = run_dynamic(
        declared_ruleset.with_rule(rule),
        files=["core.py"],
        diff_by_file={},
        worktree=None,
        timeout_s=1,
    )
    assert out == {}
    assert RULE_KEY not in out
