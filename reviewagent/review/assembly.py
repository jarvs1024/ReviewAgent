"""prompt 装配：只做模板加载 + 变量替换（语言无关）.

所有文案都在 `profiles/<name>/prompts/` 下：

* ``blocks/review.md`` 的 ``chunk`` / ``batch``       — 单文件 / 多文件批次框架；
* ``blocks/review.md`` 的 ``rules_repo`` / ``rules_general`` — 规则块（有 / 无仓库规则）；
* source / dynamic 片段由 `review/context.py` 使用。

调用方（runner / ImproveCommand 门面）负责拼接 prior anchors 等动态内容，
本模块不判断业务语义，只保证占位符严格匹配（缺 / 多都抛错）。
"""
from __future__ import annotations

from dataclasses import dataclass

from reviewagent.scenario.profile import Profile
from reviewagent.scenario.prompts import load_section, render_template

_REVIEW_TEMPLATE = "prompts/blocks/review.md"


@dataclass(frozen=True)
class ChunkPromptParts:
    cross_file_section: str
    rules_block: str
    prior_section: str
    file_path: str
    file_diff: str
    source_block: str
    valid_lines: str


@dataclass(frozen=True)
class BatchPromptParts:
    rules_block: str
    file_count: int
    file_list: str  # "- `a.py`\n- `b.py`"
    sections: str  # 各文件 section 以 "\n\n---\n\n" 连接


@dataclass(frozen=True)
class SectionParts:
    """batch 内单个文件 section 的变量快照（供后续 runner 分层组装）."""

    index: int
    total: int
    file_path: str
    cross_file_section: str
    prior_section: str
    file_diff: str
    source_block: str
    valid_lines: str


def build_chunk_prompt(parts: ChunkPromptParts, *, profile: Profile) -> str:
    template = load_section(
        _REVIEW_TEMPLATE, "chunk", profile, trim_trailing_newline=True
    )
    return render_template(
        template,
        {
            "cross_file_section": parts.cross_file_section,
            "rules_block": parts.rules_block,
            "prior_section": parts.prior_section,
            "file_path": parts.file_path,
            "file_diff": parts.file_diff,
            "source_block": parts.source_block,
            "valid_lines": parts.valid_lines,
        },
        template_path=f"{_REVIEW_TEMPLATE}#chunk",
        profile_name=profile.name,
    )


def build_batch_prompt(parts: BatchPromptParts, *, profile: Profile) -> str:
    template = load_section(
        _REVIEW_TEMPLATE, "batch", profile, trim_trailing_newline=True
    )
    return render_template(
        template,
        {
            "rules_block": parts.rules_block,
            "file_count": str(parts.file_count),
            "file_list": parts.file_list,
            "sections": parts.sections,
        },
        template_path=f"{_REVIEW_TEMPLATE}#batch",
        profile_name=profile.name,
    )


def build_rules_block(repo_rules: str, general_rules: str, *, profile: Profile) -> str:
    """规则块：有仓库规则用 ``rules_repo`` 节，否则 ``rules_general`` 节.

    两节都不裁剪尾随换行 — 末尾空行是渲染结果的一部分（与旧实现
    ``... + "\\n\\n"`` 逐字节一致）。
    """
    if repo_rules:
        template = load_section(_REVIEW_TEMPLATE, "rules_repo", profile)
        return render_template(
            template,
            {"repo_rules": repo_rules, "general_rules": general_rules},
            template_path=f"{_REVIEW_TEMPLATE}#rules_repo",
            profile_name=profile.name,
        )
    template = load_section(_REVIEW_TEMPLATE, "rules_general", profile)
    return render_template(
        template,
        {"general_rules": general_rules},
        template_path=f"{_REVIEW_TEMPLATE}#rules_general",
        profile_name=profile.name,
    )
