"""scenario.profile — 默认回退 / preset 合并 / 未知字段 / instance 绑定."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from reviewagent.scenario.profile import DEFAULT_PROFILE, bind_instance, load_profile


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _write_profile(root: Path, name: str, **overrides) -> Path:
    data = {
        "schema_version": 1,
        "name": name,
        "language": "python",
        "prompts": {"improve": "prompts/improve.md", "describe": "prompts/describe.md"},
    }
    data.update(overrides)
    profile_dir = root / name
    _write_yaml(profile_dir / "profile.yaml", data)
    prompts = data.get("prompts")
    if isinstance(prompts, dict):
        for rel in prompts.values():
            path = profile_dir / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"# {rel}\n", encoding="utf-8")
    return profile_dir


@pytest.fixture(autouse=True)
def _restore_priority_weight():
    """load_profile 会把场景值写进全局 config；测试后恢复被 env 用例改动的字段."""
    from reviewagent.config import apply_scenario, current

    saved = current().improve_priority_weight_diff
    yield
    apply_scenario({"improve_priority_weight_diff": saved})


def test_default_profile_falls_back(monkeypatch):
    monkeypatch.delenv("REVIEW_PROFILE", raising=False)
    profile = load_profile(None)
    assert profile.name == DEFAULT_PROFILE
    assert profile.language == "python"


def test_missing_profile_lists_available(tmp_path):
    with pytest.raises(ValueError, match="available="):
        load_profile("does-not-exist", root=tmp_path)


def test_env_override_wins_over_schema_default(monkeypatch):
    """env 覆盖只改运行配置，不污染 profile 数据（§5.4）."""
    monkeypatch.setenv("IMPROVE_PRIORITY_WEIGHT_DIFF", "33")
    import reviewagent.config as config_module
    from reviewagent.settings import binder

    config_module._current = binder.from_env()
    profile = load_profile(None)
    assert profile.name == DEFAULT_PROFILE
    assert profile.language == "python"
    from reviewagent.config import current

    # 调参组不在 profile.yaml 里：resolved 不携带 scoring，env 值经
    # 启动期 from_env 直接生效。
    assert current().improve_priority_weight_diff == 33.0
    assert "scoring" not in profile.resolved


def test_default_profile_applies_scenario_config():
    from reviewagent.config import current

    profile = load_profile(None)
    cfg = current()
    # 单测环境隔离了 <config>/：调参字段回落 schema 默认值
    assert cfg.improve_max_suggestions == 15
    assert cfg.improve_review_mode == "auto"
    assert cfg.repo_context_files == ("AGENTS.md",)
    assert cfg.review_exclude_extensions[0] == ".md"
    # rule_keys 是 profile.yaml 的身份/内容键：值仍来自场景包
    assert cfg.rule_key_prefix == "SSD"
    assert profile.sources["rule_keys"] == "profile"
    assert profile.sources["rule_key_prefix"] == "profile"
    # test_paths 从 preset 的 scoring.test_paths 提升为顶层键
    assert profile.test_paths == ("tests/", "test_*.py")
    # review.yaml 装载的三组（file_filter/scoring/limits 等）默认缺席
    assert "scoring" not in profile.sources


def test_unknown_profile_field_rejected(tmp_path):
    _write_profile(tmp_path, "bad", bogus=1)
    with pytest.raises(ValueError, match="unknown profile fields"):
        load_profile("bad", root=tmp_path)


def test_profile_yaml_tuning_groups_rejected_with_migration_hint(tmp_path):
    """调参组写在 profile.yaml → 启动失败并给出 review.yaml 迁移提示."""
    _write_profile(tmp_path, "alt", limits={"max_suggestions": 7})
    with pytest.raises(ValueError, match=r"move them to .*review\.yaml"):
        load_profile("alt", root=tmp_path)


def test_profile_yaml_extends_rejected(tmp_path):
    """extends 随 preset 层一并移除：出现即报错."""
    _write_profile(tmp_path, "alt", extends="base")
    with pytest.raises(ValueError, match=r"move them to .*review\.yaml"):
        load_profile("alt", root=tmp_path)


def test_profile_yaml_reporting_display_keys_rejected(tmp_path):
    """reporting 展示/扫描键只允许出现在 review.yaml."""
    _write_profile(tmp_path, "alt", reporting={"weekly_title": "周报"})
    with pytest.raises(ValueError, match=r"review\.yaml"):
        load_profile("alt", root=tmp_path)


def test_missing_language_rejected(tmp_path):
    _write_profile(tmp_path, "bad", language=None)
    with pytest.raises(ValueError, match="missing language"):
        load_profile("bad", root=tmp_path)


def test_missing_required_prompt_rejected(tmp_path):
    _write_profile(tmp_path, "bad", prompts={"improve": "prompts/improve.md"})
    with pytest.raises(ValueError, match="prompts.describe"):
        load_profile("bad", root=tmp_path)


def test_load_profile_none_uses_current_review_profile(tmp_path, monkeypatch):
    """profile 选择走 current().review_profile（instance 平面，不经 apply_scenario）."""
    import reviewagent.config as config_module

    _write_profile(tmp_path, "alt")
    monkeypatch.setattr(
        config_module,
        "_current",
        replace(
            config_module._current,
            review_profile="alt",
            review_profile_root=str(tmp_path),
        ),
    )
    profile = load_profile(None)
    assert profile.name == "alt"
    assert profile.root == (tmp_path / "alt").resolve()


def test_instance_binding_rejects_other_profile(tmp_path):
    profile = load_profile(None)
    bind_instance(profile, tmp_path)
    other = replace(profile, name="other")
    with pytest.raises(RuntimeError, match="bound to profile"):
        bind_instance(other, tmp_path)
    assert json.loads((tmp_path / "instance.json").read_text())["profile"] == DEFAULT_PROFILE


def test_instance_binding_is_atomic_and_idempotent(tmp_path):
    profile = load_profile(None)
    bind_instance(profile, tmp_path)
    payload = json.loads((tmp_path / "instance.json").read_text(encoding="utf-8"))
    assert payload == {"schema_version": 1, "profile": DEFAULT_PROFILE}
    assert not (tmp_path / "instance.json.tmp").exists()
    # 同 profile 再次绑定：直接返回，不重写
    before = (tmp_path / "instance.json").stat().st_mtime_ns
    bind_instance(profile, tmp_path)
    assert (tmp_path / "instance.json").stat().st_mtime_ns == before


def test_active_or_load_returns_cached(monkeypatch):
    import reviewagent.scenario.profile as sp

    profile = load_profile(None)
    monkeypatch.setattr(sp, "_active", profile)
    assert sp.active() is profile
    assert sp.active_or_load() is profile


# ---------- Fix round 1：引用文件 fail-fast / 校验 / instance ----------


def test_profile_schema_version_rejected(tmp_path):
    _write_profile(tmp_path, "bad", schema_version=2)
    with pytest.raises(ValueError, match="unsupported profile schema_version"):
        load_profile("bad", root=tmp_path)


def test_absolute_profile_outside_root_rejected(tmp_path):
    outside_root = tmp_path / "outside"
    _write_profile(outside_root, "ext")
    with pytest.raises(ValueError, match="not found under"):
        load_profile(str(outside_root / "ext"), root=tmp_path)


def test_missing_profile_lists_descriptions(tmp_path):
    _write_profile(tmp_path, "alt", description="备选场景")
    with pytest.raises(ValueError, match="alt \\(备选场景\\)"):
        load_profile("nope", root=tmp_path)


def test_missing_referenced_prompt_file_rejected(tmp_path):
    profile_dir = _write_profile(tmp_path, "bad")
    (profile_dir / "prompts" / "improve.md").unlink()
    with pytest.raises(ValueError, match="prompts.improve"):
        load_profile("bad", root=tmp_path)


def test_missing_dynamic_rules_file_rejected(tmp_path):
    _write_profile(tmp_path, "bad", dynamic_rules="dynamic_rules.yaml")
    with pytest.raises(ValueError, match="dynamic_rules"):
        load_profile("bad", root=tmp_path)


def test_missing_actions_file_rejected(tmp_path):
    _write_profile(tmp_path, "bad", reporting={"actions_file": "reporting/rule_actions.yaml"})
    with pytest.raises(ValueError, match="reporting.actions_file"):
        load_profile("bad", root=tmp_path)


def test_missing_rules_fail_by_default(tmp_path, monkeypatch):
    """规则包缺失默认 fail-fast；显式 `allow_missing_rules=True` 才降级为 WARNING."""
    import reviewagent.scenario.profile as sp

    warnings: list[tuple[str, tuple]] = []
    monkeypatch.setattr(
        sp.logger,
        "warning",
        lambda message, *args, **kwargs: warnings.append((message, args)),
    )
    _write_profile(tmp_path, "alt", rules=["rules/general_rules.md"])
    with pytest.raises(ValueError, match="rules"):
        load_profile("alt", root=tmp_path)
    profile = load_profile("alt", root=tmp_path, allow_missing_rules=True)
    assert profile.rules == ("rules/general_rules.md",)
    assert any("profile_missing_rule" in message for message, _ in warnings)


def test_fingerprint_stable_and_sensitive_to_profile_content(tmp_path):
    _write_profile(tmp_path, "alt", capabilities={"ast_validation": True})
    first = load_profile("alt", root=tmp_path)
    second = load_profile("alt", root=tmp_path)
    assert first.fingerprint == second.fingerprint

    _write_profile(tmp_path, "alt", capabilities={"ast_validation": False})
    changed = load_profile("alt", root=tmp_path)
    assert changed.fingerprint != first.fingerprint


def test_instance_binding_rejects_corrupt_json(tmp_path):
    (tmp_path / "instance.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="invalid instance.json") as excinfo:
        bind_instance(load_profile(None), tmp_path)
    assert str(tmp_path) in str(excinfo.value)


def test_instance_binding_rejects_unsupported_schema(tmp_path):
    (tmp_path / "instance.json").write_text(
        json.dumps({"schema_version": 2, "profile": DEFAULT_PROFILE}),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="schema_version") as excinfo:
        bind_instance(load_profile(None), tmp_path)
    assert str(tmp_path) in str(excinfo.value)
