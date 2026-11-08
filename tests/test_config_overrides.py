"""config/profiles/<name>/review.yaml 场景覆盖：优先级 / 校验 / 指纹 / 新映射."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from reviewagent.config import apply_scenario, current
from reviewagent.scenario.profile import load_profile
from reviewagent.settings.schema import SCHEMA

_SCENARIO_FIELDS = tuple(
    spec.name for spec in SCHEMA if spec.scope == "scenario" and not spec.secret
)


@pytest.fixture(autouse=True)
def _restore_scenario_config():
    saved = {name: getattr(current(), name) for name in _SCENARIO_FIELDS}
    yield
    apply_scenario(saved)


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
    for rel in data["prompts"].values():
        path = profile_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {rel}\n", encoding="utf-8")
    return profile_dir


def _write_preset(root: Path, name: str, **overrides) -> None:
    data = {
        "schema_version": 1,
        "name": name,
        "language": "python",
        "prompts": {"improve": "prompts/improve.md", "describe": "prompts/describe.md"},
    }
    data.update(overrides)
    _write_yaml(root / "_presets" / f"{name}.yaml", data)


@pytest.fixture
def config_dir(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "config"
    monkeypatch.setenv("REVIEW_CONFIG_DIR", str(path))
    return path


def _write_review(config_dir: Path, profile: str, data: dict) -> None:
    _write_yaml(config_dir / "profiles" / profile / "review.yaml", data)


def test_review_config_overrides_preset(tmp_path, config_dir):
    _write_preset(tmp_path, "base", limits={"max_suggestions": 15}, scoring={"review_mode": "auto"})
    _write_profile(tmp_path, "alt", extends="base")
    _write_review(
        config_dir,
        "alt",
        {
            "schema_version": 1,
            "limits": {"max_suggestions": 7},
            "scoring": {"min_severity": "high"},
        },
    )

    profile = load_profile("alt", root=tmp_path)

    assert current().improve_max_suggestions == 7
    assert current().improve_min_severity == "high"
    assert profile.resolved["limits"]["max_suggestions"] == 7
    assert profile.sources["improve_max_suggestions"] == "config"
    assert profile.sources["improve_min_severity"] == "config"
    # 未被 review.yaml 覆盖的字段仍来自 preset
    assert profile.sources["improve_review_mode"] == "preset"
    assert current().improve_review_mode == "auto"


def test_review_config_overrides_profile_yaml(tmp_path, config_dir):
    _write_profile(tmp_path, "alt", limits={"max_suggestions": 9})
    _write_review(config_dir, "alt", {"schema_version": 1, "limits": {"max_suggestions": 4}})

    profile = load_profile("alt", root=tmp_path)

    assert current().improve_max_suggestions == 4
    assert profile.sources["improve_max_suggestions"] == "config"


def test_review_config_missing_is_optional(tmp_path, monkeypatch):
    monkeypatch.delenv("REVIEW_CONFIG_DIR", raising=False)
    _write_profile(tmp_path, "alt", limits={"max_suggestions": 12})

    profile = load_profile("alt", root=tmp_path)

    assert current().improve_max_suggestions == 12
    assert profile.sources["improve_max_suggestions"] == "profile"


def test_review_config_for_other_profile_is_ignored(tmp_path, config_dir):
    _write_profile(tmp_path, "alt")
    _write_review(config_dir, "other", {"schema_version": 1, "limits": {"max_suggestions": 3}})

    load_profile("alt", root=tmp_path)

    assert current().improve_max_suggestions == 15


def test_review_config_unknown_field_rejected(tmp_path, config_dir):
    _write_profile(tmp_path, "alt")
    _write_review(config_dir, "alt", {"schema_version": 1, "bogus": 1})

    with pytest.raises(ValueError, match="unknown review config fields"):
        load_profile("alt", root=tmp_path)


def test_review_config_schema_version_rejected(tmp_path, config_dir):
    _write_profile(tmp_path, "alt")
    _write_review(config_dir, "alt", {"schema_version": 2, "limits": {"max_suggestions": 3}})

    with pytest.raises(ValueError, match="review config schema_version"):
        load_profile("alt", root=tmp_path)


def test_review_config_new_scenario_mappings(tmp_path, config_dir):
    _write_profile(tmp_path, "alt")
    _write_review(
        config_dir,
        "alt",
        {
            "schema_version": 1,
            "limits": {
                "max_diff_chars": 1234,
                "opencode_max_diff_chars": 567,
                "full_source_max_bytes": 89,
                "new_file_full": False,
            },
            "scoring": {"min_score": 42, "min_severity": "high"},
        },
    )

    load_profile("alt", root=tmp_path)

    cfg = current()
    assert cfg.max_diff_chars == 1234
    assert cfg.opencode_max_diff_chars == 567
    assert cfg.improve_full_source_max_bytes == 89
    assert cfg.improve_new_file_full is False
    assert cfg.improve_min_score == 42
    assert cfg.improve_min_severity == "high"


def test_review_config_type_error_is_field_named(tmp_path, config_dir):
    _write_profile(tmp_path, "alt")
    _write_review(config_dir, "alt", {"schema_version": 1, "limits": {"max_suggestions": "many"}})

    with pytest.raises(ValueError, match="improve_max_suggestions"):
        load_profile("alt", root=tmp_path)


def test_review_config_changes_fingerprint(tmp_path, config_dir):
    _write_profile(tmp_path, "alt")
    _write_review(config_dir, "alt", {"schema_version": 1, "limits": {"max_suggestions": 7}})
    first = load_profile("alt", root=tmp_path)

    _write_review(config_dir, "alt", {"schema_version": 1, "limits": {"max_suggestions": 8}})
    second = load_profile("alt", root=tmp_path)

    assert first.fingerprint != second.fingerprint


def test_review_config_nested_typo_rejected(tmp_path, config_dir):
    """嵌套拼错必须启动失败 —— 否则静默不生效（limits.max_suggestion）。"""
    _write_profile(tmp_path, "alt")
    _write_review(config_dir, "alt", {"schema_version": 1, "limits": {"max_suggestion": 3}})

    with pytest.raises(ValueError, match=r"unknown review config fields: \['limits\.max_suggestion'\]"):
        load_profile("alt", root=tmp_path)


def test_review_config_nested_batch_typo_rejected(tmp_path, config_dir):
    _write_profile(tmp_path, "alt")
    _write_review(
        config_dir, "alt",
        {"schema_version": 1, "limits": {"batch": {"enable": True}}},
    )

    with pytest.raises(ValueError, match=r"limits\.batch\.enable"):
        load_profile("alt", root=tmp_path)


def test_review_config_group_must_be_mapping(tmp_path, config_dir):
    _write_profile(tmp_path, "alt")
    _write_review(config_dir, "alt", {"schema_version": 1, "limits": 5})

    with pytest.raises(ValueError, match="must be a mapping"):
        load_profile("alt", root=tmp_path)


def test_review_config_priority_weights_nested_allowed(tmp_path, config_dir):
    _write_profile(tmp_path, "alt")
    _write_review(
        config_dir, "alt",
        {"schema_version": 1, "scoring": {"priority_weights": {"diff": 11.5}}},
    )

    load_profile("alt", root=tmp_path)

    assert current().improve_priority_weight_diff == 11.5


def test_review_config_repo_context_and_reporting_groups(tmp_path, config_dir):
    _write_profile(tmp_path, "alt")
    _write_review(
        config_dir, "alt",
        {
            "schema_version": 1,
            "repo_context": {"files": ["AGENTS.md", "CONTRIBUTING.md"], "max_lines": 300},
            "reporting": {"weekly_title": "自定标题", "repo_scan_extensions": [".py"]},
        },
    )

    profile = load_profile("alt", root=tmp_path)

    assert current().repo_context_files == ("AGENTS.md", "CONTRIBUTING.md")
    assert current().repo_context_max_lines == 300
    assert profile.reporting["weekly_title"] == "自定标题"
    assert profile.reporting["repo_scan_extensions"] == [".py"]
    assert profile.sources["repo_context_files"] == "config"


def test_review_config_reporting_actions_file_not_user_overridable(tmp_path, config_dir):
    _write_profile(tmp_path, "alt")
    _write_review(
        config_dir, "alt",
        {"schema_version": 1, "reporting": {"actions_file": "../../escape.yaml"}},
    )

    with pytest.raises(ValueError, match=r"reporting\.actions_file"):
        load_profile("alt", root=tmp_path)


def test_env_override_shadowing_review_config_logs_warning(tmp_path, config_dir, monkeypatch):
    _write_profile(tmp_path, "alt")
    _write_review(config_dir, "alt", {"schema_version": 1, "limits": {"max_suggestions": 7}})
    monkeypatch.setenv("IMPROVE_MAX_SUGGESTIONS", "3")
    warnings: list[str] = []

    def _capture(message, *args, **_kwargs):
        warnings.append(message.format(*args) if args else message)

    from reviewagent.scenario import profile as profile_mod

    monkeypatch.setattr(profile_mod.logger, "warning", _capture)

    load_profile("alt", root=tmp_path)

    assert current().improve_max_suggestions == 3
    assert any("config.env_shadowed" in msg and "improve_max_suggestions" in msg for msg in warnings)
