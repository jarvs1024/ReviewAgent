"""提示词加载器 — 从 reviewagent/prompts/*.md 读 frontmatter + 内容.

每个 .md 文件 = YAML frontmatter + Markdown 正文.
frontmatter 字段:
    name:           agent 名称（必须）
    description:    一句话描述
    output_schema:  期望输出 JSON schema（可选）
    tools:          工具开关，如 {write: false, bash: false}
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import frontmatter

PROMPTS_DIR = Path(__file__).parent


def load(name: str) -> dict[str, Any]:
    """name: 'describe' / 'improve' / 'weekly_report' 等（不带 .md）.

    返回:
        {
            "name": str,
            "description": str,
            "output_schema": dict | None,
            "tools": dict,
            "prompt": str,         # Markdown 正文
            "tools_disabled": list[str],   # 简化：值为 false 的工具列表
        }
    """
    path = PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"prompt file not found: {path}")

    md = frontmatter.load(path)
    meta = dict(md.metadata)

    # 工具开关：值为 false 的视为禁用
    tools = meta.get("tools", {})
    tools_disabled = [k for k, v in tools.items() if v is False]

    return {
        "name": meta.get("name") or name,
        "description": meta.get("description", ""),
        "output_schema": meta.get("output_schema"),
        "tools": tools,
        "tools_disabled": tools_disabled,
        "prompt": md.content,
    }


def available() -> list[str]:
    """列出所有可用 prompt 名称（不含 .md 后缀）.

    排除以下划线开头的私有片段 (如 `_general_rules_block.md`),
    这些是给 chunk prompt 用的可复用 block, 不是独立 prompt.
    """
    return sorted(
        p.stem for p in PROMPTS_DIR.glob("*.md") if not p.stem.startswith("_")
    )


def load_block(name: str) -> str:
    """加载私有 block (下划线开头的 .md), 返回正文.

    用于把通用规则 / SSD 规则表等可复用片段注入到 chunk prompt 里,
    避免 system prompt 与 chunk prompt 引用脱节.
    """
    if not name.startswith("_"):
        raise ValueError(f"load_block 只接受下划线开头的 block 名, got: {name}")
    path = PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"block file not found: {path}")
    md = frontmatter.load(path)
    return md.content