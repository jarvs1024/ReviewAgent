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


def _available_profiles(root: Path) -> list[str]:
    """列出可用 profile（附 description），用于"profile 不存在"的错误提示."""
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
        raise ValueError(
            f"profile '{name}' not found under {root}; "
            f"available={_available_profiles(root)}"
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
        if spec.env:
            # env 可能在 config 导入之后才设置（测试 / 二次加载）：统一经
            # binder.spec_env_override 二次解析（canonical + aliases），
            # 显式空值视为"未提供"，继续走 profile 值。
            env_value = spec_env_override(spec)
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
    """引用文件存在性 fail-fast（prompts / dynamic_rules / reporting.actions_file）.

    `rules` 首期允许缺失（规则包由 Task 5/6 落地），但必须显式参数化；
    缺失时记录 WARNING，并且仍计入 fingerprint。
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
    reporting = merged.get("reporting")
    actions_file = reporting.get("actions_file") if isinstance(reporting, dict) else None
    if actions_file and not _reference_path(profile_dir, actions_file).is_file():
        raise ValueError(
            f"profile field reporting.actions_file references missing file: {actions_file!r} "
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


def load_profile(
    name: str | None = None,
    *,
    root: Path | None = None,
    allow_missing_rules: bool = True,
) -> Profile:
    """加载（并应用场景配置）一个 profile.

    `name=None` 时读 `config.current().review_profile`（实例平面字段，不走
    `apply_scenario`）；为空则回退 `DEFAULT_PROFILE` 并 WARNING。

    `allow_missing_rules` 首期为 True（规则包尚未落地）；Task 5 会把默认改为
    False，届时缺失 `rules` 引用文件将直接启动失败。
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
        if preset.get("schema_version") != _SUPPORTED_SCHEMA:
            raise ValueError(
                f"unsupported preset schema_version: {preset.get('schema_version')!r} "
                f"({preset_path})"
            )
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
    _apply_config(merged, raw, sources)
    referenced = list(prompts.values()) + list(merged.get("rules") or [])
    if merged.get("dynamic_rules"):
        referenced.append(merged["dynamic_rules"])
    reporting = merged.get("reporting")
    if isinstance(reporting, dict) and reporting.get("actions_file"):
        referenced.append(reporting["actions_file"])
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


def deactivate() -> None:
    """清空激活缓存（测试隔离 / 重新装配用；不影响 `_current` 配置）."""
    global _active
    _active = None


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
