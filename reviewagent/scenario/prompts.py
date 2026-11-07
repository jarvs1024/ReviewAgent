"""场景 prompt 加载：按 profile 解析模板路径 + 严格占位符渲染."""
from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import frontmatter

from reviewagent.scenario.profile import Profile, active_or_load

_SHARED = "_shared"
# 占位符两侧允许空白：`{{name}}` / `{{ name }}` 均可。
_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


def _profile(profile: Profile | None) -> Profile:
    return profile or active_or_load()


def _reject_unsafe_name(name: str) -> None:
    """拒绝路径分隔符与前导 `.`，避免 name 被拼成目录穿越路径."""
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise ValueError(
            f"invalid prompt name (no path separators or leading '.'): {name!r}"
        )


def _inside(base: Path, candidate: Path) -> bool:
    return candidate == base or base in candidate.parents


def _prompt_path(name: str, profile: Profile | None = None) -> Path:
    _reject_unsafe_name(name)
    prof = _profile(profile)
    root = prof.root.resolve()
    rel = prof.prompts.get(name)
    if rel is None:
        shared_root = (root.parent / _SHARED).resolve()
        path = (shared_root / "prompts" / f"{name}.md").resolve()
        if not _inside(shared_root, path):
            raise ValueError(f"prompt escapes shared root: {name}")
        if not path.is_file():
            raise FileNotFoundError(f"prompt '{name}' not configured in profile {prof.name}")
        return path
    path = (root / rel).resolve()
    if not _inside(root, path):
        raise ValueError(f"prompt escapes profile root: {rel}")
    return path


def load(name: str, profile: Profile | None = None) -> dict[str, Any]:
    """加载 prompt（frontmatter + 正文），字段与旧 `prompts.loader.load` 一致."""
    md = frontmatter.load(_prompt_path(name, profile))
    meta = dict(md.metadata)
    tools = meta.get("tools", {})
    return {
        "name": meta.get("name") or name,
        "description": meta.get("description", ""),
        "output_schema": meta.get("output_schema"),
        "tools": tools,
        "tools_disabled": [k for k, v in tools.items() if v is False],
        "prompt": md.content,
    }


def available(profile: Profile | None = None) -> list[str]:
    """列出当前 profile 配置的 prompt 名（键来自 profile.prompts，不 glob 磁盘）."""
    prof = _profile(profile)
    return sorted(str(name) for name in prof.prompts if not str(name).startswith("_"))


def load_block(name: str, profile: Profile | None = None) -> str:
    if name == "_general_rules_block":
        # 通用规则正文已迁移到规则包：走 RuleSet 渲染，不再读旧 block 文件。
        from reviewagent.scenario.rules.render import render_rule_block
        from reviewagent.scenario.rules.resolver import active_rule_set

        prof = _profile(profile)
        return render_rule_block(active_rule_set(), profile=prof)
    if not name.startswith("_"):
        raise ValueError(f"load_block 只接受下划线开头的 block 名, got: {name}")
    _reject_unsafe_name(name)
    prof = _profile(profile)
    root = prof.root.resolve()
    path = (root / "prompts" / f"{name}.md").resolve()
    if not _inside(root, path):
        raise ValueError(f"block escapes profile root: {name}")
    if not path.is_file():
        shared_root = (root.parent / _SHARED).resolve()
        path = (shared_root / "prompts" / f"{name}.md").resolve()
        if not _inside(shared_root, path):
            raise ValueError(f"block escapes shared root: {name}")
        if not path.is_file():
            raise FileNotFoundError(
                f"block '{name}' not found in profile {prof.name} "
                f"({root / 'prompts'}) or shared ({shared_root / 'prompts'})"
            )
    return frontmatter.load(path).content


def load_template(
    rel_path: str,
    profile: Profile | None = None,
    *,
    trim_trailing_newline: bool = False,
) -> str:
    prof = _profile(profile)
    path = (prof.root / rel_path).resolve()
    if prof.root.resolve() not in path.parents:
        raise ValueError(f"template escapes profile root: {rel_path}")
    text = path.read_text(encoding="utf-8")
    return text.rstrip("\n") if trim_trailing_newline else text


def render_template(
    text: str,
    values: Mapping[str, str],
    *,
    template_path: str,
    profile_name: str | None = None,
) -> str:
    found = set(_PLACEHOLDER.findall(text))
    missing = found - set(values)
    extra = set(values) - found
    if missing or extra:
        where = f"profile {profile_name}, " if profile_name else ""
        raise ValueError(
            f"template {template_path} ({where}missing={sorted(missing)} "
            f"extra={sorted(extra)})"
        )
    return _PLACEHOLDER.sub(lambda m: values[m.group(1)], text)
