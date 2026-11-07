"""场景 prompt 加载：按 profile 解析模板路径 + 严格占位符渲染."""
from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import frontmatter

from reviewagent.scenario.profile import Profile, active_or_load

_PLACEHOLDER = re.compile(r"\{\{([a-zA-Z_][a-zA-Z0-9_]*)\}\}")


def _profile(profile: Profile | None) -> Profile:
    return profile or active_or_load()


def _prompt_path(name: str, profile: Profile | None = None) -> Path:
    prof = _profile(profile)
    rel = prof.prompts.get(name)
    if rel is None:
        path = (prof.root.parent / "_shared" / "prompts" / f"{name}.md").resolve()
        if not path.exists():
            raise FileNotFoundError(f"prompt '{name}' not configured in profile {prof.name}")
        return path
    path = (prof.root / rel).resolve()
    if prof.root.resolve() not in path.parents:
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
    prof = _profile(profile)
    names = {p.stem for p in prof.root.glob("prompts/*.md") if not p.stem.startswith("_")}
    return sorted(names)


def load_block(name: str, profile: Profile | None = None) -> str:
    if not name.startswith("_"):
        raise ValueError(f"load_block 只接受下划线开头的 block 名, got: {name}")
    prof = _profile(profile)
    path = (prof.root / "prompts" / f"{name}.md").resolve()
    if not path.exists():
        path = (prof.root.parent / "_shared" / "prompts" / f"{name}.md").resolve()
    if prof.root.resolve() not in path.parents:
        raise ValueError(f"block escapes profile root: {name}")
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


def render_template(text: str, values: Mapping[str, str], *, template_path: str) -> str:
    found = set(_PLACEHOLDER.findall(text))
    missing = found - set(values)
    extra = set(values) - found
    if missing or extra:
        raise ValueError(
            f"template {template_path}: missing={sorted(missing)} extra={sorted(extra)}"
        )
    return _PLACEHOLDER.sub(lambda m: values[m.group(1)], text)
