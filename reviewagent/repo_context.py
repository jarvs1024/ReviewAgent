"""兼容门面：实现已迁至 `reviewagent/scenario/rules/repo.py`.

保留本模块只为既有调用方（`reviewagent/commands/_common.py`、
`reviewagent/reporting/rule_translate.py` 以及测试的 monkeypatch 目标）不破：
`review` 侧不再拥有规则解析实现，规则读取/渲染一律在 scenario 规则层。
"""
from __future__ import annotations

from reviewagent.scenario.rules.repo import (  # noqa: F401
    INSTRUCTION_FILES_INTRO,
    TRUNCATION_MARKER,
    build_repo_context,
    extract_rule_keys,
    fetch_rule_files,
    render_instruction_files,
)

__all__ = [
    "INSTRUCTION_FILES_INTRO",
    "TRUNCATION_MARKER",
    "build_repo_context",
    "extract_rule_keys",
    "fetch_rule_files",
    "render_instruction_files",
]
