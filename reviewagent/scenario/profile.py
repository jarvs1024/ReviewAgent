"""场景包（Profile）：场景配置应用与 data_dir 绑定.

设计要点（见 docs/superpowers/specs/2026-11-07-multi-language-review-design.md §5，
2026-11-08 修订：删除 preset/extends 层，见 docs/superpowers/specs/ 下修订说明）：

* 一个 profile = `profiles/<name>/profile.yaml`，只承载身份与内容引用
  （语言 / 提示词 / 规则 / 规则键 / capabilities / security）；
* 场景调参的唯一入口是 `<config>/profiles/<name>/review.yaml`，叠加在
  schema 默认值之上（review.yaml > schema 默认），只允许
  `file_filter` / `scoring` / `limits` / `context` / `repo_context` / `reporting`
  六组（`reporting` 仅展示/扫描类键）；
* 未知字段一律启动失败；历史上允许的 preset `extends` 与 profile 内调参组
  现在同样启动失败，并提示迁移到 review.yaml；
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
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from reviewagent.config import apply_scenario
from reviewagent.logging_setup import logger
from reviewagent.settings.binder import UNSET, explicit_env, spec_env_override
from reviewagent.settings.schema import SCHEMA_BY_NAME

_SUPPORTED_SCHEMA = 1
# 规则键 → 周报跟进动作的默认路径（相对 profile 根目录）；profile 可用
# `rule_actions` 字段改到任意文件，未显式配置且默认文件缺失时视为空表。
RULE_ACTIONS_FILE = "rules/rule_actions.yaml"
_ALLOWED_PROFILE_KEYS = {
    "schema_version", "name", "description", "language", "test_paths", "prompts", "rules",
    "dynamic_rules", "rule_keys", "rule_key_promotions", "rule_overrides", "disable",
    "rule_actions", "capabilities", "security",
}
# 曾经允许出现在 profile.yaml / preset 的调参组：报错时给出迁移指引。
_REMOVED_PROFILE_KEYS = {
    "extends", "file_filter", "scoring", "limits", "context", "repo_context",
}
# 用户配置只开放"按场景调参"的组；身份 / 规则 / 提示词仍由场景包作者维护。
# `rule_keys` 只开放 `repo_prefix`（见 _SCENARIO_MAPPING），规则内容本身仍归场景包。
_ALLOWED_REVIEW_CONFIG_KEYS = {
    "schema_version", "file_filter", "scoring", "limits", "context",
    "repo_context", "rule_keys", "reporting",
}
# 规则动作表路径由 profile 的 `rule_actions` 声明（缺省 rules/rule_actions.yaml），
# 属场景包作者域，不开放给用户 review.yaml 覆写；reporting 只开放展示/扫描类键。
_REPORTING_USER_KEYS = frozenset({"weekly_title", "repo_scan_extensions", "repo_scan_entry_files"})

_SCENARIO_MAPPING: dict[tuple[str, ...], str] = {
    ("file_filter", "exclude_extensions"): "review_exclude_extensions",
    ("file_filter", "skip_paths"): "improve_skip_test_paths",
    ("file_filter", "priority_paths"): "improve_keyword_paths",
    ("scoring", "feature_keywords"): "improve_test_feature_keywords",
    ("scoring", "review_mode"): "improve_review_mode",
    ("scoring", "test_sample_paths"): "improve_test_sample_paths",
    ("scoring", "test_sample_max"): "improve_test_sample_max",
    ("scoring", "min_score"): "improve_min_score",
    ("scoring", "min_severity"): "improve_min_severity",
    ("context", "partial_context_lines"): "improve_partial_context_lines",
    ("context", "patch_context_lines"): "improve_patch_context_lines",
    ("limits", "max_suggestions"): "improve_max_suggestions",
    ("limits", "max_suggestions_per_file"): "improve_max_suggestions_per_file",
    ("limits", "min_suggestions_per_file"): "improve_min_suggestions_per_file",
    ("limits", "max_files_to_review"): "improve_max_files_to_review",
    ("limits", "max_diff_chars"): "max_diff_chars",
    ("limits", "opencode_max_diff_chars"): "opencode_max_diff_chars",
    ("limits", "full_files"): "improve_full_files",
    ("limits", "full_source_max_bytes"): "improve_full_source_max_bytes",
    ("limits", "new_file_full"): "improve_new_file_full",
    ("limits", "parallel_workers"): "improve_parallel_workers",
    ("limits", "clip_diff_lines"): "improve_clip_diff_lines",
    ("limits", "batch", "enabled"): "improve_batch_enabled",
    ("limits", "batch", "max_diff_lines"): "improve_batch_max_diff_lines",
    ("limits", "batch", "max_files"): "improve_batch_max_files",
    ("limits", "batch", "max_total_lines"): "improve_batch_max_total_lines",
    ("repo_context", "rules_dir"): "repo_context_rules_dir",
    ("repo_context", "max_lines"): "repo_context_max_lines",
    ("rule_keys", "repo_prefix"): "rule_key_prefix",
}
for _weight in ("diff", "keyword", "density", "test_feature"):
    _SCENARIO_MAPPING[("scoring", "priority_weights", _weight)] = f"improve_priority_weight_{_weight}"


def _review_config_whitelist() -> dict[str, dict[str, frozenset[str] | None]]:
    """从映射表派生 review.yaml 的嵌套白名单（leaf -> 子键集合 / None）."""
    allowed: dict[str, dict[str, frozenset[str] | None]] = {}
    for path in _SCENARIO_MAPPING:
        group, rest = path[0], path[1:]
        if group not in _ALLOWED_REVIEW_CONFIG_KEYS:
            continue
        bucket = allowed.setdefault(group, {})
        if len(rest) == 1:
            bucket.setdefault(rest[0], None)
        else:
            nested = bucket.get(rest[0])
            merged = set(nested or ())
            merged.add(rest[1])
            bucket[rest[0]] = frozenset(merged)
    allowed["reporting"] = {key: None for key in _REPORTING_USER_KEYS}
    return allowed


_REVIEW_CONFIG_WHITELIST = _review_config_whitelist()


def _validate_review_config(data: dict[str, Any], path: Path) -> None:
    """顶层分组 + 嵌套键 + 分组类型全部校验（拼错必须启动失败）."""
    unknown = set(data) - _ALLOWED_REVIEW_CONFIG_KEYS
    if unknown:
        raise ValueError(f"unknown review config fields: {sorted(unknown)} ({path})")
    for group, value in data.items():
        if group == "schema_version" or value is None:
            continue
        if not isinstance(value, dict):
            raise ValueError(f"review config group {group!r} must be a mapping ({path})")
        allowed = _REVIEW_CONFIG_WHITELIST.get(group, {})
        bad: list[str] = []
        for key, sub in value.items():
            if key not in allowed:
                bad.append(f"{group}.{key}")
                continue
            nested = allowed[key]
            if nested is None:
                continue
            if not isinstance(sub, dict):
                raise ValueError(
                    f"review config group {group}.{key} must be a mapping ({path})"
                )
            bad.extend(f"{group}.{key}.{name}" for name in set(sub) - nested)
        if bad:
            raise ValueError(
                f"unknown review config fields: {sorted(bad)} ({path}); "
                f"allowed={sorted(_REVIEW_CONFIG_WHITELIST.get(group, {}))}"
            )


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
    rule_actions: str
    test_paths: tuple[str, ...]
    capabilities: dict[str, Any]
    security: dict[str, Any]
    reporting: dict[str, Any]
    resolved: dict[str, Any]
    sources: dict[str, str]
    fingerprint: str


def _deep_merge(base: Any, override: Any) -> Any:
    """深度融合：`None` = 继承，显式空 dict/list = 清空（与 `[]` 语义对齐）."""
    if override is None:
        return copy.deepcopy(base)
    if isinstance(override, dict):
        if not override:
            # `{}` 是显式空映射：必须清空 preset 值，而不是被当成"未提供"继承。
            return {}
        out = dict(base) if isinstance(base, dict) else {}
        for key, value in override.items():
            out[key] = _deep_merge(out.get(key), value)
        return out
    return copy.deepcopy(override)


def _read_yaml(path: Path, *, kind: str = "profile") -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{kind} must be a mapping: {path}")
    return data


def _resolve_root(root: Path | None) -> Path:
    if root is not None:
        return Path(root).expanduser().resolve()
    from reviewagent.config import current

    return Path(current().review_profile_root).expanduser().resolve()


def _resolve_config_dir(profile_root: Path) -> Path:
    """用户配置目录：与 `profiles/` 同级，`REVIEW_CONFIG_DIR` 可整体改位置."""
    override = spec_env_override(SCHEMA_BY_NAME["review_config_dir"])
    if override is not UNSET:
        return Path(override).expanduser().resolve()
    from reviewagent.config import current

    configured = current().review_config_dir
    if configured:
        return Path(configured).expanduser().resolve()
    return (profile_root.parent / "config").resolve()


def available_profiles(root: Path) -> list[str]:
    """列出可用 profile（附 description），供 `profile list` 与错误提示复用."""
    entries: list[str] = []
    for path in sorted(root.glob("*/profile.yaml")):
        name = path.parent.name
        try:
            data = _read_yaml(path)
        except (OSError, ValueError, yaml.YAMLError):
            entries.append(name)
            continue
        description = data.get("description")
        entries.append(f"{name} ({description})" if description else name)
    return entries


def default_profile_name(root: Path) -> str:
    """REVIEW_PROFILE 未设置时的隐式场景：根目录下唯一 profile，否则空串."""
    entries = sorted(path.parent.name for path in root.glob("*/profile.yaml"))
    return entries[0] if len(entries) == 1 else ""


def _resolve_profile_dir(name: str, root: Path) -> Path:
    if not name:
        candidates = sorted(path.parent.name for path in root.glob("*/profile.yaml"))
        if len(candidates) == 1:
            name = candidates[0]
            logger.info("scenario.profile_auto name={} (REVIEW_PROFILE unset)", name)
        elif not candidates:
            raise ValueError(f"no profiles found under {root}")
        else:
            raise ValueError(
                f"REVIEW_PROFILE is unset and profile root has multiple scenarios: "
                f"{candidates}; set REVIEW_PROFILE or pass --profile"
            )
    candidate = Path(name)
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        resolved = (root / name).resolve()
    if not resolved.is_dir() or resolved.parent != root:
        raise ValueError(
            f"profile '{name}' not found under {root}; "
            f"available={available_profiles(root)}"
        )
    return resolved


def _has_path(node: Any, path: tuple[str, ...]) -> bool:
    for part in path:
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def _apply_config(
    merged: dict[str, Any],
    sources: dict[str, str],
    config_raw: dict[str, Any] | None = None,
) -> dict[str, Any]:
    mapping = _SCENARIO_MAPPING
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
            _warn_env_shadowed(field_name, spec.env, config_raw, path, getattr(current_config(), field_name))
            continue
        if spec.env:
            # env 可能在 config 导入之后才设置（测试 / 二次加载）：统一经
            # binder.spec_env_override 二次解析（canonical + aliases），
            # 显式空值视为"未提供"，继续走 profile 值。
            env_value = spec_env_override(spec)
            if env_value is not UNSET:
                updates[field_name] = env_value
                sources[field_name] = "env"
                _warn_env_shadowed(field_name, spec.env, config_raw, path, env_value)
                continue
        updates[field_name] = node
        # 来源按真实出处标注：review.yaml → config；profile.yaml（如 rule_keys）→ profile。
        sources[field_name] = "config" if config_raw and _has_path(config_raw, path) else "profile"
    apply_scenario(updates)
    return updates


def current_config() -> Any:
    from reviewagent.config import current

    return current()


def _warn_env_shadowed(
    field_name: str,
    env_name: str,
    config_raw: dict[str, Any] | None,
    path: tuple[str, ...],
    effective: Any,
) -> None:
    """env 压过 review.yaml 时留痕：否则用户改了 review.yaml 却看不到任何效果."""
    if not config_raw or not _has_path(config_raw, path):
        return
    node: Any = config_raw
    for part in path:
        node = node[part]
    if isinstance(node, (list, tuple)) and isinstance(effective, (list, tuple)):
        differed = list(node) != list(effective)
    else:
        differed = node != effective
    if differed:
        logger.warning(
            "config.env_shadowed field={} env={} config_value={!r} effective={!r}",
            field_name,
            env_name,
            node,
            effective,
        )


def _fingerprint(root: Path, merged: dict[str, Any], files: list[str]) -> str:
    digest = hashlib.sha256()
    digest.update(json.dumps(merged, sort_keys=True, ensure_ascii=False).encode())
    for rel in sorted(files):
        path = root / rel
        if path.is_file():
            data = path.read_bytes()
        else:
            # 不能静默按空文件计算：缺失文件与空文件必须可区分，且留下 WARNING。
            logger.warning(
                "scenario.profile_fingerprint_missing path={} profile_root={}",
                rel,
                root,
            )
            data = b"\0missing\0"
        digest.update(rel.encode() + b"\0" + hashlib.sha256(data).digest())
    return digest.hexdigest()


def _reference_path(profile_dir: Path, rel: Any) -> Path:
    return (profile_dir / str(rel)).expanduser()


def _check_referenced_files(
    profile_dir: Path,
    merged: dict[str, Any],
    prompts: dict[str, Any],
    *,
    allow_missing_rules: bool,
) -> None:
    """引用文件存在性 fail-fast（prompts / dynamic_rules / rules）.

    `rules` 默认同样要求存在（启动即失败）；`allow_missing_rules=True` 仅供
    迁移 / 测试临时放行，此时记录 WARNING，并且仍计入 fingerprint。
    """
    missing_prompts = [
        f"prompts.{key}={rel!r}"
        for key, rel in prompts.items()
        if not _reference_path(profile_dir, rel).is_file()
    ]
    if missing_prompts:
        raise ValueError(
            "profile references missing prompt files: "
            + ", ".join(missing_prompts)
            + f" (profile root: {profile_dir})"
        )
    dynamic_rules = merged.get("dynamic_rules")
    if dynamic_rules and not _reference_path(profile_dir, dynamic_rules).is_file():
        raise ValueError(
            f"profile field dynamic_rules references missing file: {dynamic_rules!r} "
            f"(profile root: {profile_dir})"
        )
    missing_rules = [
        str(rel) for rel in merged.get("rules") or () if not _reference_path(profile_dir, rel).is_file()
    ]
    if missing_rules:
        if not allow_missing_rules:
            raise ValueError(
                f"profile field rules references missing file(s): {missing_rules} "
                f"(profile root: {profile_dir})"
            )
        for rel in missing_rules:
            logger.warning(
                "scenario.profile_missing_rule path={} profile_root={}",
                rel,
                profile_dir,
            )
    rule_actions = merged.get("rule_actions")
    if rule_actions and not _reference_path(profile_dir, rule_actions).is_file():
        raise ValueError(
            f"profile field rule_actions references missing file: {rule_actions!r} "
            f"(profile root: {profile_dir})"
        )


def _load_review_config(
    profile_root: Path,
    profile_name: str,
    merged: dict[str, Any],
    sources: dict[str, str],
) -> dict[str, Any]:
    """叠加用户配置 `<config>/profiles/<name>/review.yaml`（缺失即跳过）."""
    path = _resolve_config_dir(profile_root) / "profiles" / profile_name / "review.yaml"
    if not path.is_file():
        return {}
    config_raw = _read_yaml(path, kind="review config")
    _validate_review_config(config_raw, path)
    if config_raw.get("schema_version") != _SUPPORTED_SCHEMA:
        raise ValueError(
            f"unsupported review config schema_version: "
            f"{config_raw.get('schema_version')!r} ({path})"
        )
    for key, value in config_raw.items():
        if key == "schema_version":
            continue
        merged[key] = _deep_merge(merged.get(key), value)
        sources[key] = "config"
    return config_raw


def load_profile(
    name: str | None = None,
    *,
    root: Path | None = None,
    allow_missing_rules: bool = False,
) -> Profile:
    """加载（并应用场景配置）一个 profile.

    `name=None` 时读 `config.current().review_profile`（实例平面字段，不走
    `apply_scenario`）；为空则要求 profile 根目录下恰好一个场景包（自动选中），
    多个场景包必须显式配置 `REVIEW_PROFILE`。

    `allow_missing_rules` 默认 False：`rules` 引用的规则包缺失即启动失败
    （fail-fast）。显式传 True 只用于迁移 / 测试，缺失时降级为 WARNING。
    """
    profile_root = _resolve_root(root)
    if name is None:
        from reviewagent.config import current

        name = current().review_profile
    profile_dir = _resolve_profile_dir(name or "", profile_root)
    raw = _read_yaml(profile_dir / "profile.yaml")
    unknown = set(raw) - _ALLOWED_PROFILE_KEYS
    if "reporting" in unknown:
        raise ValueError(
            "profile.yaml no longer hosts 'reporting'; rule actions belong in "
            f"{RULE_ACTIONS_FILE}, display/scan keys belong in "
            f"<config>/profiles/{profile_dir.name}/review.yaml"
        )
    if unknown:
        removed = sorted(unknown & _REMOVED_PROFILE_KEYS)
        if removed:
            raise ValueError(
                f"profile.yaml no longer hosts tuning groups (found: {removed}); "
                f"move them to <config>/profiles/{profile_dir.name}/review.yaml "
                "(preset/extends inheritance was removed)"
            )
        raise ValueError(f"unknown profile fields: {sorted(unknown)}")
    if raw.get("schema_version") != _SUPPORTED_SCHEMA:
        raise ValueError(f"unsupported profile schema_version: {raw.get('schema_version')!r}")
    merged: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for key, value in raw.items():
        if key == "schema_version":
            continue
        merged[key] = _deep_merge(merged.get(key), value)
        sources[key] = "profile"
    config_raw = _load_review_config(profile_root, profile_dir.name, merged, sources)
    language = merged.get("language")
    if not language:
        raise ValueError(f"profile missing language: {profile_dir}")
    prompts = merged.get("prompts") or {}
    if not isinstance(prompts, dict):
        raise ValueError(f"profile field prompts must be a mapping: {profile_dir}")
    missing_required = [key for key in ("improve", "describe") if key not in prompts]
    if missing_required:
        fields = ", ".join(f"prompts.{key}" for key in missing_required)
        raise ValueError(f"profile missing required fields: {fields}: {profile_dir}")
    _check_referenced_files(
        profile_dir,
        merged,
        prompts,
        allow_missing_rules=allow_missing_rules,
    )
    _apply_config(merged, sources, config_raw)
    referenced = list(prompts.values()) + list(merged.get("rules") or [])
    if merged.get("dynamic_rules"):
        referenced.append(merged["dynamic_rules"])
    actions_rel = str(merged.get("rule_actions") or RULE_ACTIONS_FILE)
    if (profile_dir / actions_rel).is_file():
        referenced.append(actions_rel)
    blocks_dir = profile_dir / "prompts" / "blocks"
    if blocks_dir.is_dir():
        referenced.extend(
            path.relative_to(profile_dir).as_posix()
            for path in sorted(blocks_dir.glob("*.md"))
        )
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
        rule_actions=str(merged.get("rule_actions") or RULE_ACTIONS_FILE),
        test_paths=tuple(str(p) for p in (merged.get("test_paths") or ())),
        capabilities=dict(merged.get("capabilities") or {}),
        security=dict(merged.get("security") or {}),
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
    # 新 profile 不得看到上一个 profile 装配的规则（启动装配随后会重新 set）。
    _invalidate_rule_set()
    return _active


def deactivate() -> None:
    """清空激活缓存（测试隔离 / 重新装配用；不影响 `_current` 配置）."""
    global _active
    _active = None
    _invalidate_rule_set()


def _invalidate_rule_set() -> None:
    """惰性失效激活 RuleSet（避免 profile <-> rules 的模块级循环导入）."""
    from reviewagent.scenario.rules.resolver import set_active_rule_set

    set_active_rule_set(None)


_INSTANCE_SCHEMA = 1
_LOCK_TIMEOUT_SECONDS = 5.0


def _instance_error(data_dir: Path, detail: str) -> RuntimeError:
    return RuntimeError(
        f"data_dir {data_dir} has an invalid instance.json: {detail}; "
        "bind a new REVIEWAGENT_DATA_DIR or fix the file"
    )


def _load_instance(path: Path, data_dir: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise _instance_error(data_dir, f"unreadable JSON ({path}): {exc}") from exc
    if not isinstance(payload, dict):
        raise _instance_error(data_dir, f"expected a JSON object in {path}")
    if payload.get("schema_version") != _INSTANCE_SCHEMA:
        raise _instance_error(
            data_dir,
            f"unsupported schema_version {payload.get('schema_version')!r} in {path}",
        )
    return payload


def _check_bound_profile(path: Path, data_dir: Path, profile: Profile) -> None:
    existing = _load_instance(path, data_dir)
    if existing.get("profile") != profile.name:
        raise RuntimeError(
            f"data_dir {data_dir} is bound to profile {existing.get('profile')!r}, "
            f"refusing {profile.name!r}; use a new REVIEWAGENT_DATA_DIR"
        )


def bound_profile(data_dir: Path) -> str | None:
    """返回 `data_dir` 已绑定的 profile 名；尚未绑定返回 None.

    文件损坏 / schema 版本不符时抛 RuntimeError（消息指出 data_dir 与文件路径），
    供 bootstrap.readiness() 与 CLI 复用，避免各处自行解析 `instance.json`。
    """
    path = Path(data_dir) / "instance.json"
    if not path.exists():
        return None
    payload = _load_instance(path, Path(data_dir))
    return str(payload.get("profile") or "") or None


def bind_instance(profile: Profile, data_dir: Path) -> None:
    """把 data_dir 绑定到 profile（原子写 `instance.json`）.

    同一 data_dir 已绑定其他 profile / 损坏 / 版本不符时抛 RuntimeError
    （提示新建数据目录），避免 telemetry / cohort / 周报跨场景混用。

    首次写入用 `O_CREAT|O_EXCL` 锁文件串行化并发首启，写入自身仍是
    "同目录临时文件 + os.replace" 的原子替换。
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "instance.json"
    if path.exists():
        _check_bound_profile(path, data_dir, profile)
        return
    lock_path = data_dir / "instance.json.lock"
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    lock_fd: int | None = None
    while lock_fd is None:
        try:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"data_dir {data_dir}: timed out waiting for {lock_path.name}; "
                    "another process may be binding this data_dir"
                ) from None
            time.sleep(0.01)
    try:
        os.write(lock_fd, str(os.getpid()).encode())
        # 双重检查：等待锁期间另一个进程可能已完成首次绑定。
        if path.exists():
            _check_bound_profile(path, data_dir, profile)
            return
        payload = {"schema_version": _INSTANCE_SCHEMA, "profile": profile.name}
        tmp = data_dir / f".instance.json.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        try:
            tmp.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
    finally:
        os.close(lock_fd)
        lock_path.unlink(missing_ok=True)
