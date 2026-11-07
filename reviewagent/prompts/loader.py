"""提示词加载器 — 兼容门面，实际解析委托 `reviewagent.scenario.prompts`.

每个 .md 文件 = YAML frontmatter + Markdown 正文.
frontmatter 字段:
    name:           agent 名称（必须）
    description:    一句话描述
    output_schema:  期望输出 JSON schema（可选）
    tools:          工具开关，如 {write: false, bash: false}

`PROMPTS_DIR` 保留指向本包目录（旧路径），供 `scripts/sync_qoder_agents.py`
等外部脚本兼容导入；实际模板解析按当前场景 profile 进行.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from reviewagent.scenario.prompts import available as _available
from reviewagent.scenario.prompts import load as _load
from reviewagent.scenario.prompts import load_block as _load_block

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
    return _load(name)


def available() -> list[str]:
    """列出当前 profile 下所有可用 prompt 名称（不含 .md 后缀）.

    排除以下划线开头的私有片段 (如 `_general_rules_block.md`),
    这些是给 chunk prompt 用的可复用 block, 不是独立 prompt.
    """
    return _available()


def load_block(name: str) -> str:
    """加载私有 block (下划线开头的 .md), 返回正文.

    用于把通用规则 / SSD 规则表等可复用片段注入到 chunk prompt 里,
    避免 system prompt 与 chunk prompt 引用脱节.
    """
    return _load_block(name)
