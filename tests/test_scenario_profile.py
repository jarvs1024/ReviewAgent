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


def test_profile_merges_preset_and_keeps_env_override(monkeypatch):
    monkeypatch.setenv("IMPROVE_PRIORITY_WEIGHT_DIFF", "33")
    profile = load_profile(None)
    assert profile.name == DEFAULT_PROFILE
    assert profile.language == "python"
    assert profile.resolved["scoring"]["priority_weights"]["diff"] == 20
    from reviewagent.config import current

    # env 只覆盖运行配置，不污染 profile 数据（§5.4）
    assert current().improve_priority_weight_diff == 33.0


def test_default_profile_applies_scenario_config():
    from reviewagent.config import current

    profile = load_profile(None)
    cfg = current()
    assert profile.reporting["weekly_title"] == "SSD自动化代码检视周报"
    assert cfg.improve_max_suggestions == 15
    assert cfg.improve_review_mode == "auto"
    assert cfg.repo_context_files == ("AGENTS.md",)
    assert cfg.rule_key_prefix == "SSD"
    assert cfg.review_exclude_extensions[0] == ".md"
    assert profile.sources["scoring"] == "preset"
    assert profile.sources["context"] == "preset"
    assert profile.sources["reporting"] == "profile"


def test_preset_merge_and_profile_override(tmp_path):
    _write_yaml(
        tmp_path / "_presets" / "base.yaml",
        {
            "schema_version": 1,
            "name": "base",
            "language": "python",
            "prompts": {"improve": "prompts/improve.md", "describe": "prompts/describe.md"},
            "capabilities": {"ast_validation": True, "cross_file_search": False},
            "context": {"partial_context_lines": 20},
        },
    )
    _write_profile(tmp_path, "alt", extends="base", capabilities={"cross_file_search": True})
    profile = load_profile("alt", root=tmp_path)
    # 映射深度融合：preset 独有的键保留，profile 覆盖同名字段
    assert profile.resolved["capabilities"] == {
        "ast_validation": True,
        "cross_file_search": True,
    }
    # preset 提供、profile 未重写的键仍然生效
    assert profile.prompts == {
        "improve": "prompts/improve.md",
        "describe": "prompts/describe.md",
    }
    assert profile.sources["capabilities"] == "profile"
    assert profile.sources["context"] == "preset"


def test_unknown_profile_field_rejected(tmp_path):
    _write_profile(tmp_path, "bad", bogus=1)
    with pytest.raises(ValueError, match="unknown profile fields"):
        load_profile("bad", root=tmp_path)


def test_unknown_preset_field_rejected(tmp_path):
    _write_yaml(
        tmp_path / "_presets" / "base.yaml",
        {"schema_version": 1, "language": "python", "bogus": 1},
    )
    _write_profile(tmp_path, "bad", extends="base")
    with pytest.raises(ValueError, match="unknown preset fields"):
        load_profile("bad", root=tmp_path)


def test_preset_must_not_extend(tmp_path):
    _write_yaml(
        tmp_path / "_presets" / "base.yaml",
        {"schema_version": 1, "language": "python", "extends": "other"},
    )
    _write_profile(tmp_path, "bad", extends="base")
    with pytest.raises(ValueError, match="preset must not extend"):
        load_profile("bad", root=tmp_path)


@pytest.mark.parametrize("bad_name", ["a/b", "a.b", "../base", ".hidden"])
def test_invalid_preset_name_rejected(tmp_path, bad_name):
    _write_profile(tmp_path, "bad", extends=bad_name)
    with pytest.raises(ValueError, match="invalid preset name"):
        load_profile("bad", root=tmp_path)


def test_unknown_preset_rejected(tmp_path):
    _write_profile(tmp_path, "bad", extends="nope")
    with pytest.raises(ValueError, match="unknown preset"):
        load_profile("bad", root=tmp_path)


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
