"""仓库规则上下文 — 从仓库默认分支读取 AGENTS.md 等规则文件并注入 prompt.

参考 pr-agent 的 repo_context.py 实现:
    - 从默认分支读取规则文件 (安全: 不信任 PR head)
    - 渲染为 <instruction_files> XML 块
    - 提取规则键 (如 ZLG-RULE-NO-LOG-EXC)
    - TTL 缓存避免重复 API 调用
"""
from __future__ import annotations

import re
import time
from collections import OrderedDict
from html import escape
from typing import Any

from reviewagent.config import config
from reviewagent.logging_setup import logger

TRUNCATION_MARKER = "...(truncated)..."
INSTRUCTION_FILES_INTRO = (
    "You are being given instruction files. Follow them as project-specific guidance when reviewing code."
)
MARKDOWN_FENCE = "`````"

# ---------- 缓存 ----------
_CACHE_MAX_SIZE = 64
_CACHE_TTL_SECONDS = 15 * 60  # 15 分钟


class _RepoContextCache:
    """LRU + TTL 缓存."""

    def __init__(self, max_size: int = _CACHE_MAX_SIZE, ttl_seconds: int = _CACHE_TTL_SECONDS):
        self._max_size = max(1, max_size)
        self._ttl_seconds = max(0, ttl_seconds)
        self._entries: OrderedDict[str, tuple[str, float]] = OrderedDict()

    def get(self, key: str) -> str | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if expires_at <= time.monotonic():
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return value

    def set(self, key: str, value: str) -> None:
        self._entries[key] = (value, time.monotonic() + self._ttl_seconds)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_size:
            self._entries.popitem(last=False)


_cache = _RepoContextCache()


# ---------- 规则键提取 ----------
_RULE_KEY_PATTERN_CACHE: dict[str, re.Pattern[str]] = {}


def _rule_key_pattern() -> re.Pattern[str]:
    """构建规则键正则: <PREFIX>-RULE-<KEY>."""
    prefix = config.rule_key_prefix or "ZLG"
    pat = _RULE_KEY_PATTERN_CACHE.get(prefix)
    if pat is None:
        pat = re.compile(rf"(?<![\w])({re.escape(prefix)}-RULE-[A-Z0-9-]+)(?![\w])")
        _RULE_KEY_PATTERN_CACHE[prefix] = pat
    return pat


def extract_rule_keys(text: str) -> list[str]:
    """从文本中提取规则键 (去重, 保序)."""
    if not text:
        return []
    seen: list[str] = []
    for m in _rule_key_pattern().finditer(text):
        key = m.group(1)
        if key not in seen:
            seen.append(key)
    return seen


# ---------- 渲染 ----------
def _get_markdown_fence(content: str) -> str:
    fence = MARKDOWN_FENCE
    while fence in content:
        fence += "`"
    return fence


def render_instruction_files(files: dict[str, str], max_lines: int = 500) -> str:
    """把文件内容渲染为 <instruction_files> XML 块 (带行数预算)."""
    if not files:
        return ""

    parts = [
        INSTRUCTION_FILES_INTRO,
        "<instruction_files>",
    ]
    closing_tag = "</instruction_files>"

    if max_lines < len(parts) + 1:
        return ""

    for path, content in files.items():
        scope = path.rsplit("/", 1)[0] if "/" in path else "repo-root"
        fence = _get_markdown_fence(content)
        file_header = [
            f'<file path="{escape(path, quote=True)}" scope="{escape(scope, quote=True)}">',
            f"{fence}markdown",
        ]
        file_footer = [
            fence,
            "</file>",
            "",
        ]
        content_lines = content.rstrip().splitlines()
        reserved = len(file_header) + len(file_footer) + 1
        available = max_lines - len(parts) - reserved
        if available < 0 or (content_lines and available < 1):
            break

        parts.extend(file_header)
        if available >= len(content_lines):
            parts.extend(content_lines)
        else:
            if available > 1:
                parts.extend(content_lines[: available - 1])
            parts.append(TRUNCATION_MARKER)
            parts.extend(file_footer)
            break

        parts.extend(file_footer)

    parts.append(closing_tag)
    return "\n".join(parts).strip()


# ---------- 规则文件获取 ----------
# 缓存: {cache_key: {path: content}}
_files_cache: dict[str, dict[str, str]] = {}
_files_cache_ts: dict[str, float] = {}


def _fetch_all_rule_files(gitlab_client: Any, project_id: int) -> dict[str, str]:
    """从仓库默认分支读取所有规则文件 (AGENTS.md + rules_dir). 带 TTL 缓存."""
    context_files = config.repo_context_files
    rules_dir = config.repo_context_rules_dir

    if not context_files and not rules_dir:
        return {}

    cache_key = f"{project_id}:{','.join(context_files)}:{rules_dir}"

    # TTL 缓存检查
    ts = _files_cache_ts.get(cache_key, 0)
    if cache_key in _files_cache and (time.monotonic() - ts) < _CACHE_TTL_SECONDS:
        return _files_cache[cache_key]

    # 获取默认分支
    try:
        project = gitlab_client._get_project(project_id)
        default_branch = getattr(project, "default_branch", None) or "main"
    except Exception as e:
        logger.warning("repo_context.get_default_branch failed project={}: {}", project_id, e)
        return {}

    files: dict[str, str] = {}

    # 1. 读取配置中列出的文件 (AGENTS.md 等)
    for file_path in context_files:
        file_path = file_path.strip()
        if not file_path:
            continue
        try:
            content = gitlab_client.get_file_at_sha(project_id, file_path, default_branch)
        except Exception as e:
            logger.warning("repo_context.read_file failed project={} file={}: {}", project_id, file_path, e)
            continue
        if content:
            files[file_path] = content.rstrip()

    # 2. 读取规则目录下的所有 .md 文件
    if rules_dir:
        try:
            tree = gitlab_client.list_repository_tree(project_id, rules_dir, default_branch)
        except Exception as e:
            logger.warning("repo_context.list_rules_dir failed project={} dir={}: {}", project_id, rules_dir, e)
            tree = []

        for item in tree:
            if item.get("type") != "blob":
                continue
            path = item.get("path", "")
            if not path.endswith(".md"):
                continue
            if path in files:
                continue
            try:
                content = gitlab_client.get_file_at_sha(project_id, path, default_branch)
            except Exception as e:
                logger.warning("repo_context.read_rule failed project={} file={}: {}", project_id, path, e)
                continue
            if content:
                files[path] = content.rstrip()

    # 更新缓存
    _files_cache[cache_key] = files
    _files_cache_ts[cache_key] = time.monotonic()

    if files:
        rule_keys = extract_rule_keys("\n".join(files.values()))
        logger.info(
            "repo_context.loaded project={} files={} rules={}",
            project_id, list(files.keys()), rule_keys[:20],
        )

    return files


def fetch_rule_files(gitlab_client: Any, project_id: int) -> dict[str, str]:
    """返回 {path: content} 字典 (原文, 不渲染). 供写入 worktree 让 agent 自己 read."""
    return _fetch_all_rule_files(gitlab_client, project_id)


# ---------- 主入口 ----------
def build_repo_context(gitlab_client: Any, project_id: int) -> str:
    """从仓库默认分支读取规则文件 + 规则目录, 渲染为 instruction_files 块.

    Args:
        gitlab_client: GitLabClient 实例
        project_id: GitLab project ID

    Returns:
        渲染后的 instruction_files 文本 (空字符串表示无规则文件)
    """
    files = _fetch_all_rule_files(gitlab_client, project_id)
    if not files:
        return ""

    result = render_instruction_files(files, config.repo_context_max_lines)
    return result
