"""场景包（Profile）：preset 合并、场景配置应用与 data_dir 绑定.

设计要点（见 docs/superpowers/specs/2026-11-07-multi-language-review-design.md §5）：

* 一个 profile = `profiles/<name>/profile.yaml`，可 `extends` 一个
  `profiles/_presets/<name>.yaml`；preset 不得再 `extends`（只允许一级继承）；
* 未知字段 / 未知 preset / 多级继承 / 非法 `extends` 名称一律启动失败；
* profile 中的场景字段通过 `config.apply_scenario()` 应用到运行时配置；
  显式 env 仍然优先（§5.4：env 只覆盖运行配置，不污染 profile 数据）；
* `{data_dir}/instance.json` 记录首次绑定的 profile，同一 data_dir 换 profile
  必须换目录（原子写，冲突时提示新建 `REVIEWAGENT_DATA_DIR`）。
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from reviewagent.config import apply_scenario
from reviewagent.logging_setup import logger
from reviewagent.settings.binder import UNSET, coerce, explicit_env
from reviewagent.settings.schema import SCHEMA_BY_NAME

DEFAULT_PROFILE = "ssd-python-autotest"
_SHARED = "_shared"
_PRESETS = "_presets"
_SUPPORTED_SCHEMA = 1
_ALLOWED_PROFILE_KEYS = {
    "schema_version", "name", "description", "extends", "language", "prompts", "rules",
    "dynamic_rules", "rule_keys", "rule_key_promotions", "rule_overrides", "disable",
    "repo_context", "capabilities", "security", "file_filter", "scoring", "reporting",
    "limits", "context",
}


@dataclass(frozen=True)
class Profile:
    name: str
    language: str
    root: Path
    prompts: dict[str, str]
    rules: tuple[str, ...]
    dynamic_rules: str | None
    rule_keys: dict[str, Any]
    rule_key_promotions: tuple[dict[str, Any], ...]
    rule_overrides: dict[str, dict[str, Any]]
    disable: tuple[str, ...]
    repo_context: dict[str, Any]
    capabilities: dict[str, Any]
    security: dict[str, Any]
    file_filter: dict[str, Any]
    scoring: dict[str, Any]
    reporting: dict[str, Any]
    resolved: dict[str, Any]
    sources: dict[str, str]
    fingerprint: str


def _deep_merge(base: Any, override: Any) -> Any:
    if override is None:
        return copy.deepcopy(base)
    if isinstance(base, dict) and isinstance(override, dict):
        out = dict(base)
        for key, value in override.items():
            out[key] = _deep_merge(out.get(key), value)
        return out
    return copy.deepcopy(override)


def _read_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"profile must be a mapping: {path}")
    return data


def _resolve_root(root: Path | None) -> Path:
    if root is not None:
        return Path(root).expanduser().resolve()
    from reviewagent.config import current

    return Path(current().review_profile_root).expanduser().resolve()


def _resolve_profile_dir(name: str, root: Path) -> Path:
    if not name:
        name = DEFAULT_PROFILE
        logger.warning("scenario.profile_default name={} (REVIEW_PROFILE unset)", name)
    candidate = Path(name)
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        resolved = (root / name).resolve()
    if not resolved.is_dir() or resolved.parent != root:
        available = sorted(p.parent.name for p in root.glob("*/profile.yaml"))
        raise ValueError(f"profile '{name}' not found under {root}; available={available}")
    return resolved


def _has_path(node: Any, path: tuple[str, ...]) -> bool:
    for part in path:
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def _apply_config(
    profile_dir: Path,
    merged: dict[str, Any],
    raw: dict[str, Any],
    sources: dict[str, str],
) -> dict[str, Any]:
    mapping = {
        ("file_filter", "exclude_extensions"): "review_exclude_extensions",
        ("file_filter", "skip_paths"): "improve_skip_test_paths",
        ("file_filter", "priority_paths"): "improve_keyword_paths",
        ("scoring", "feature_keywords"): "improve_test_feature_keywords",
        ("scoring", "review_mode"): "improve_review_mode",
        ("scoring", "test_sample_paths"): "improve_test_sample_paths",
        ("scoring", "test_sample_max"): "improve_test_sample_max",
        ("context", "partial_context_lines"): "improve_partial_context_lines",
        ("context", "patch_context_lines"): "improve_patch_context_lines",
        ("limits", "max_suggestions"): "improve_max_suggestions",
        ("limits", "max_suggestions_per_file"): "improve_max_suggestions_per_file",
        ("limits", "min_suggestions_per_file"): "improve_min_suggestions_per_file",
        ("limits", "max_files_to_review"): "improve_max_files_to_review",
        ("limits", "full_files"): "improve_full_files",
        ("limits", "parallel_workers"): "improve_parallel_workers",
        ("limits", "clip_diff_lines"): "improve_clip_diff_lines",
        ("limits", "batch", "enabled"): "improve_batch_enabled",
        ("limits", "batch", "max_diff_lines"): "improve_batch_max_diff_lines",
        ("limits", "batch", "max_files"): "improve_batch_max_files",
        ("limits", "batch", "max_total_lines"): "improve_batch_max_total_lines",
        ("repo_context", "files"): "repo_context_files",
        ("repo_context", "rules_dir"): "repo_context_rules_dir",
        ("repo_context", "max_lines"): "repo_context_max_lines",
        ("rule_keys", "repo_prefix"): "rule_key_prefix",
    }
    for weight in ("diff", "keyword", "density", "test_feature"):
        mapping[("scoring", "priority_weights", weight)] = f"improve_priority_weight_{weight}"
    explicit = explicit_env()
    updates: dict[str, Any] = {}
    for path, field_name in mapping.items():
        node: Any = merged
        for part in path:
            if not isinstance(node, dict) or part not in node:
                node = None
                break
            node = node[part]
        if node is None:
            continue
        spec = SCHEMA_BY_NAME[field_name]
        if spec.env and spec.env in explicit:
            # env 在 config 导入时已生效：保持现状，不用 profile 值覆盖。
            sources[field_name] = "env"
            continue
        if spec.env and spec.env in os.environ:
            # env 在 config 导入之后才设置（测试 / 二次加载）：按 env 平面语义
            # 重新解析并应用，显式空值视为"未提供"，继续走 profile 值。
            env_value = coerce(spec, os.environ[spec.env])
            if env_value is not UNSET:
                updates[field_name] = env_value
                sources[field_name] = "env"
                continue
        updates[field_name] = node
        sources[field_name] = "profile" if _has_path(raw, path) else "preset"
    apply_scenario(updates)
    return updates


def _fingerprint(root: Path, merged: dict[str, Any], files: list[str]) -> str:
    digest = hashlib.sha256()
    digest.update(json.dumps(merged, sort_keys=True, ensure_ascii=False).encode())
    for rel in sorted(files):
        data = (root / rel).read_bytes() if (root / rel).exists() else b""
        digest.update(rel.encode() + b"\0" + hashlib.sha256(data).digest())
    return digest.hexdigest()


def load_profile(name: str | None = None, *, root: Path | None = None) -> Profile:
    """加载（并应用场景配置）一个 profile.

    `name=None` 时读 `config.current().review_profile`（实例平面字段，不走
    `apply_scenario`）；为空则回退 `DEFAULT_PROFILE` 并 WARNING。
    """
    profile_root = _resolve_root(root)
    if name is None:
        from reviewagent.config import current

        name = current().review_profile
    profile_dir = _resolve_profile_dir(name or "", profile_root)
    raw = _read_yaml(profile_dir / "profile.yaml")
    unknown = set(raw) - _ALLOWED_PROFILE_KEYS
    if unknown:
        raise ValueError(f"unknown profile fields: {sorted(unknown)}")
    if raw.get("schema_version") != _SUPPORTED_SCHEMA:
        raise ValueError(f"unsupported profile schema_version: {raw.get('schema_version')!r}")
    extends = raw.get("extends")
    merged: dict[str, Any] = {}
    sources: dict[str, str] = {}
    if extends:
        preset_name = str(extends)
        if "/" in preset_name or "." in preset_name:
            raise ValueError(f"invalid preset name (must be a single level name): {extends}")
        preset_path = profile_root / _PRESETS / f"{preset_name}.yaml"
        if not preset_path.exists():
            raise ValueError(f"unknown preset '{preset_name}': {preset_path}")
        preset = _read_yaml(preset_path)
        if "extends" in preset:
            raise ValueError(f"preset must not extend another preset: {preset_path}")
        unknown = set(preset) - _ALLOWED_PROFILE_KEYS
        if unknown:
            raise ValueError(f"unknown preset fields: {sorted(unknown)}")
        merged = _deep_merge(merged, preset)
        sources.update({k: "preset" for k in preset})
    for key, value in raw.items():
        # name/description 属于 profile 自身身份（preset 的同名字段不得覆盖），
        # schema_version 已在上面校验，extends 只用于定位 preset。
        if key in {"schema_version", "extends"}:
            continue
        merged[key] = _deep_merge(merged.get(key), value)
        sources[key] = "profile"
    language = merged.get("language")
    if not language:
        raise ValueError(f"profile missing language: {profile_dir}")
    prompts = merged.get("prompts") or {}
    for required in ("improve", "describe"):
        if required not in prompts:
            raise ValueError(f"profile missing prompts.{required}: {profile_dir}")
    _apply_config(profile_dir, merged, raw, sources)
    referenced = list(prompts.values()) + list(merged.get("rules") or [])
    if merged.get("dynamic_rules"):
        referenced.append(merged["dynamic_rules"])
    if merged.get("reporting", {}).get("actions_file"):
        referenced.append(merged["reporting"]["actions_file"])
    return Profile(
        name=str(merged.get("name") or profile_dir.name),
        language=str(language),
        root=profile_dir,
        prompts={k: str(v) for k, v in prompts.items()},
        rules=tuple(str(p) for p in (merged.get("rules") or [])),
        dynamic_rules=merged.get("dynamic_rules"),
        rule_keys=dict(merged.get("rule_keys") or {}),
        rule_key_promotions=tuple(merged.get("rule_key_promotions") or ()),
        rule_overrides=dict(merged.get("rule_overrides") or {}),
        disable=tuple(merged.get("disable") or ()),
        repo_context=dict(merged.get("repo_context") or {}),
        capabilities=dict(merged.get("capabilities") or {}),
        security=dict(merged.get("security") or {}),
        file_filter=dict(merged.get("file_filter") or {}),
        scoring=dict(merged.get("scoring") or {}),
        reporting=dict(merged.get("reporting") or {}),
        resolved=merged,
        sources=sources,
        fingerprint=_fingerprint(profile_dir, merged, referenced),
    )


_active: Profile | None = None


def active() -> Profile:
    if _active is None:
        raise RuntimeError("scenario profile not initialized; call load_and_activate() at startup")
    return _active


def active_or_load() -> Profile:
    global _active
    if _active is None:
        from reviewagent.config import current

        _active = load_profile(current().review_profile)
    return _active


def load_and_activate() -> Profile:
    global _active
    from reviewagent.config import current

    _active = load_profile(current().review_profile)
    return _active


def bind_instance(profile: Profile, data_dir: Path) -> None:
    """把 data_dir 绑定到 profile（原子写 `instance.json`）.

    同一 data_dir 已绑定其他 profile 时抛 RuntimeError（提示新建数据目录），
    避免 telemetry / cohort / 周报跨场景混用。
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "instance.json"
    payload = {"schema_version": 1, "profile": profile.name}
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing.get("profile") != profile.name:
            raise RuntimeError(
                f"data_dir {data_dir} is bound to profile {existing.get('profile')!r}, "
                f"refusing {profile.name!r}; use a new REVIEWAGENT_DATA_DIR"
            )
        return
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
