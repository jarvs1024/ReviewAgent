"""仓库规则（spec §6.4）：读取仓库规则文件、渲染注入块、抽取规则键、请求级 overlay.

实现自 `reviewagent/repo_context.py` 原样迁入，唯一语义改动是配置读取改为
调用时 `current()`（不再在 import 期绑定 `config` 代理），因此本模块与
`rules` 包其余模块一样属于纯规则层：不 import 检视引擎（`review`），也不
直接读进程环境变量（env 只经 `settings.binder` 进入配置）。

参考 pr-agent 的 repo_context.py 实现:
    - 从默认分支读取规则文件 (安全: 不信任 PR head)
    - 渲染为 <instruction_files> XML 块
    - 提取规则键 (如 ZLG-RULE-NO-LOG-EXC)
    - TTL 缓存避免重复 API 调用

规则文件**不假设固定目录/前缀/条数**：从 `repo_context_files` 入口文件
（默认 `AGENTS.md`）出发，跟随其中的 Markdown 链接发现规则索引与规则文件；
`index.md` 自动展开同目录，目录链接展开其一层的 `.md`；`repo_context_rules_dir`
保留为显式覆盖（默认空=自动）。发现结果同时生成运行时 `<rule_index>`，
保证截断时模型仍能拿到完整键清单。

`parse_repo_overlay` 把仓库规则表（Markdown 表格行）转成请求级 `Rule` 覆盖：
* 仓库新规则：键命中 `base.key_patterns.is_repo(key)`，任意键均可新增；
* 场景规则覆盖：键是内置键且已在 `base.index_by_key` 中（同键覆盖，spec §6.4），
  只换 severity/title/priority/source，其余字段沿用基础规则；
* 其余键（自由文本 / 无关表格）静默忽略——不把叙事内容误判成规则；
* 围栏代码块内的表格行（示例）不参与解析。
"""
from __future__ import annotations

import posixpath
import re
import time
from collections import OrderedDict, deque
from dataclasses import replace
from html import escape
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

from reviewagent.logging_setup import logger
from reviewagent.scenario.rules.model import Rule
from reviewagent.security import fence_for, normalize_untrusted, security_flag

if TYPE_CHECKING:  # 仅类型：避免在 import 期拉起 profile/config 的 env 解析
    from reviewagent.scenario.profile import Profile
    from reviewagent.scenario.rules.resolver import RuleSet

TRUNCATION_MARKER = "...(truncated)..."
INSTRUCTION_FILES_INTRO = (
    "You are being given instruction files. Follow them as project-specific guidance when reviewing code."
)
MARKDOWN_FENCE = "`````"

_SEVERITIES = frozenset({"critical", "high", "medium", "low"})
_REPO_RULE_PRIORITY = 1000

# 动态发现护栏：入口→index→规则文件 至多两跳；文件数与单文件体积设上限，
# 避免 AGENTS.md 链接到整个文档树时把默认分支全部拉下来。
_MAX_LINK_DEPTH = 2
_MAX_DISCOVERED_FILES = 60
_MAX_RULE_FILE_BYTES = 512 * 1024
_MD_LINK_RE = re.compile(r"\[[^\]]*\]\(\s*<?([^)\s>]+)>?[^)]*\)")
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")

# 运行时规则索引使用通用仓库键形状（任意大写前缀），与具体仓库无关。
_REPO_KEY_SOURCE = r"[A-Z][A-Z0-9]*-RULE-[A-Z0-9-]+"
_REPO_KEY_RE = re.compile(_REPO_KEY_SOURCE)
_HEADING_RE = re.compile(r"^###\s+(\S+)(?:\s+(.*))?$")

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
    """构建规则键正则: `<PREFIX>-RULE-<KEY>`；auto 时匹配任意大写前缀."""
    from reviewagent.scenario.rules.keys import repo_key_source

    prefix = repo_key_source(current_prefix())
    pat = _RULE_KEY_PATTERN_CACHE.get(prefix)
    if pat is None:
        pat = re.compile(rf"(?<![\w])({prefix})(?![\w])")
        _RULE_KEY_PATTERN_CACHE[prefix] = pat
    return pat


def current_prefix() -> str:
    """当前配置的仓库规则前缀（惰性读配置，避免 import 期绑定）."""
    from reviewagent.config import current

    return current().rule_key_prefix


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
def _normalize_enabled() -> bool:
    """是否清理指令文件里的不可信控制字符（读激活 profile 的 security 开关）.

    开关缺失 / profile 尚未装配 / 装配异常时保持**开启**（fail-safe：清理只会
    去掉控制字符，正常内容逐字节不变）。
    """
    from reviewagent.scenario.profile import active_or_load

    try:
        profile = active_or_load()
    except Exception:  # noqa: BLE001 - 开关解析失败不得影响规则渲染
        return True
    return security_flag(profile, "normalize_untrusted", default=True)


def _get_markdown_fence(content: str) -> str:
    """自适应围栏：起点为 ``MARKDOWN_FENCE``（5 反引号），内容含更长连续反引号时加长."""
    return fence_for(content, minimum=len(MARKDOWN_FENCE))


def render_instruction_files(files: dict[str, str], max_lines: int = 500) -> str:
    """把文件内容渲染为 <instruction_files> XML 块 (带行数预算).

    文件内容是不可信数据：按 profile 的 ``security.normalize_untrusted`` 清理
    控制字符，围栏长度始终大于内容中的最长连续反引号（防提前闭合）。
    """
    if not files:
        return ""

    parts = [
        INSTRUCTION_FILES_INTRO,
        "<instruction_files>",
    ]
    closing_tag = "</instruction_files>"

    if max_lines < len(parts) + 1:
        return ""

    normalize = _normalize_enabled()
    items = list(files.items())

    for index, (path, content) in enumerate(items):
        if normalize:
            content = normalize_untrusted(content)
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
        # 每个文件最小占用：2 行头 + 3 行尾 + 1 行分隔。后续文件先预留，
        # 避免第一个超大文件吃光预算导致其后的规则文件被静默丢弃。
        reserved = 6
        remaining = len(items) - index
        keep_for_later = 6 * (remaining - 1)
        budget_left = max_lines - len(parts)
        fair_share = max(0, budget_left // remaining - reserved)
        available = max(0, budget_left - reserved - keep_for_later)
        available = min(available, fair_share) if remaining > 1 else available
        if available < 0 or (content_lines and available < 1):
            continue

        parts.extend(file_header)
        if available >= len(content_lines):
            parts.extend(content_lines)
        else:
            if available > 1:
                parts.extend(content_lines[: available - 1])
            parts.append(TRUNCATION_MARKER)
        parts.extend(file_footer)

    parts.append(closing_tag)
    return "\n".join(parts).strip()


# ---------- 规则文件获取 ----------
# 缓存: {cache_key: {path: content}}
_files_cache: dict[str, dict[str, str]] = {}
_files_cache_ts: dict[str, float] = {}


def _parse_markdown_links(text: str) -> list[str]:
    """提取 Markdown 内联链接目标（`[text](target)`），保持出现顺序."""
    return [m.group(1).strip() for m in _MD_LINK_RE.finditer(text or "")]


def _resolve_repo_link(target: str, base_dir: str) -> tuple[str, bool] | None:
    """把 Markdown 链接解析为仓库相对路径.

    只接受仓库内相对链接：拒绝外链 / 绝对路径 / 反斜杠 / `..` 段；
    返回 `(path, is_dir)`，无法安全解析时返回 None。
    """
    raw = target.strip().strip("<>").strip()
    if not raw or raw.startswith("#"):
        return None
    if _SCHEME_RE.match(raw):
        return None
    raw = unquote(raw.split("#", 1)[0].split("?", 1)[0]).strip()
    if not raw or raw.startswith("/") or "\\" in raw:
        return None
    is_dir = raw.endswith("/")
    raw = raw.rstrip("/")
    if not raw:
        return None
    if any(seg == ".." for seg in raw.split("/")):
        return None
    resolved = posixpath.normpath(posixpath.join(base_dir, raw))
    if resolved in ("", ".", "..") or resolved.startswith(("../", "/")):
        return None
    return resolved, is_dir


def _list_md_files(
    gitlab_client: Any, project_id: int, directory: str, ref: str, *, recursive: bool
) -> list[str]:
    """列出目录下的 `.md` blob 路径（不抛错，失败返回空）."""
    try:
        tree = gitlab_client.list_repository_tree(
            project_id, directory, ref, recursive=recursive
        )
    except Exception as e:
        logger.warning(
            "repo_context.list_dir failed project={} dir={}: {}", project_id, directory, e
        )
        return []
    return [
        str(item.get("path") or "")
        for item in tree or []
        if item.get("type") == "blob" and str(item.get("path") or "").endswith(".md")
    ]


def _read_rule_file(
    gitlab_client: Any, project_id: int, path: str, ref: str
) -> str | None:
    """读取单个规则文件；超限 / 失败返回 None."""
    try:
        content = gitlab_client.get_file_at_sha(project_id, path, ref)
    except Exception as e:
        logger.warning("repo_context.read_file failed project={} file={}: {}", project_id, path, e)
        return None
    if not content:
        return None
    if len(content.encode("utf-8")) > _MAX_RULE_FILE_BYTES:
        logger.warning(
            "repo_context.file_too_large project={} file={} bytes={} limit={}",
            project_id, path, len(content.encode("utf-8")), _MAX_RULE_FILE_BYTES,
        )
        return None
    return content.rstrip()


def _discover_rule_files(
    gitlab_client: Any,
    project_id: int,
    default_branch: str,
    entry_files: tuple[str, ...],
    explicit_dir: str,
) -> dict[str, str]:
    """从入口文件跟随 Markdown 链接发现规则文件（不假设目录/前缀/条数）."""
    files: dict[str, str] = {}
    queue: deque[tuple[str, int]] = deque()
    queued: set[str] = set()
    for entry in entry_files:
        path = entry.strip()
        while path.startswith("./"):
            path = path[2:]
        path = path.lstrip("/")
        if path and path not in queued:
            queued.add(path)
            queue.append((path, 0))

    while queue and len(files) < _MAX_DISCOVERED_FILES:
        path, depth = queue.popleft()
        content = _read_rule_file(gitlab_client, project_id, path, default_branch)
        if content is None:
            continue
        files[path] = content
        if depth >= _MAX_LINK_DEPTH or not path.lower().endswith(".md"):
            continue

        base_dir = posixpath.dirname(path)
        targets: list[tuple[str, bool]] = []
        for target in _parse_markdown_links(content):
            resolved = _resolve_repo_link(target, base_dir)
            if resolved is not None:
                targets.append(resolved)

        # index.md 的兄弟文件即规则文件（未必逐个被链接），自动展开一层。
        if posixpath.basename(path).lower() == "index.md":
            for sibling in _list_md_files(
                gitlab_client, project_id, base_dir, default_branch, recursive=False
            ):
                targets.append((sibling, False))

        for target, is_dir in targets:
            if depth + 1 > _MAX_LINK_DEPTH:
                continue
            if is_dir:
                discovered = _list_md_files(
                    gitlab_client, project_id, target, default_branch, recursive=False
                )
            elif target.lower().endswith(".md"):
                discovered = [target]
            else:
                continue
            for item in discovered:
                if item not in queued:
                    queued.add(item)
                    queue.append((item, depth + 1))

    # 显式目录覆盖（兼容旧配置）：自动发现之后补充，保持“入口文件在前”。
    if explicit_dir:
        for path in _list_md_files(
            gitlab_client, project_id, explicit_dir.strip("/"), default_branch, recursive=True
        ):
            if path in files or len(files) >= _MAX_DISCOVERED_FILES:
                continue
            content = _read_rule_file(gitlab_client, project_id, path, default_branch)
            if content is not None:
                files[path] = content
    return files


def _fetch_all_rule_files(gitlab_client: Any, project_id: int) -> dict[str, str]:
    """从仓库默认分支读取规则文件（入口链接发现 + 可选目录覆盖）. 带 TTL 缓存."""
    from reviewagent.config import current

    cfg = current()
    context_files = cfg.repo_context_files
    rules_dir = cfg.repo_context_rules_dir

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

    files = _discover_rule_files(
        gitlab_client, project_id, default_branch, tuple(context_files), rules_dir
    )

    # 更新缓存（空结果也缓存，避免故障期反复打 API）
    _files_cache[cache_key] = files
    _files_cache_ts[cache_key] = time.monotonic()

    if files:
        rule_keys = extract_rule_keys("\n".join(files.values()))
        logger.info(
            "repo_context.loaded project={} files={} rules={}",
            project_id, list(files.keys()), rule_keys[:20],
        )

    return files


def _repo_rule_entries(files: dict[str, str]) -> dict[str, dict[str, str]]:
    """从已抓取文本抽取仓库规则键 → {severity,title}（表格行 + `### KEY 标题`）."""
    entries: dict[str, dict[str, str]] = {}
    for content in files.values():
        in_fence = False
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            heading = _HEADING_RE.match(stripped)
            if heading:
                key = heading.group(1).strip("`")
                if _REPO_KEY_RE.fullmatch(key):
                    entry = entries.setdefault(key, {"severity": "", "title": ""})
                    if heading.group(2) and not entry["title"]:
                        entry["title"] = heading.group(2).strip()
                continue
            if not stripped.startswith("|"):
                continue
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            key = cells[0].strip("`").strip() if cells else ""
            if not _REPO_KEY_RE.fullmatch(key):
                continue
            entry = entries.setdefault(key, {"severity": "", "title": ""})
            severity = cells[1].lower() if len(cells) > 1 else ""
            if severity in _SEVERITIES:
                entry["severity"] = severity
            if len(cells) > 2 and cells[2] and not entry["title"]:
                entry["title"] = cells[2].strip("`").strip()
    return entries


def render_rule_index(files: dict[str, str]) -> str:
    """渲染运行时 `<rule_index>`：本次发现的仓库规则键全集（不落盘、不入库）."""
    entries = _repo_rule_entries(files)
    if not entries:
        return ""
    lines = [
        '<rule_index note="keys discovered from repository default branch; copy verbatim">'
    ]
    for key, entry in entries.items():
        parts = [key]
        if entry["severity"]:
            parts.append(entry["severity"])
        if entry["title"]:
            parts.append(entry["title"])
        lines.append(" | ".join(parts))
    lines.append("</rule_index>")
    return "\n".join(lines)


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
    from reviewagent.config import current

    files = _fetch_all_rule_files(gitlab_client, project_id)
    if not files:
        return ""

    index = render_rule_index(files)
    budget = current().repo_context_max_lines
    if not index:
        return render_instruction_files(files, budget)
    rendered = render_instruction_files(
        files, max(1, budget - len(index.splitlines()) - 1)
    )
    return f"{index}\n{rendered}".strip() if rendered else index


# ---------- 请求级 overlay ----------
def parse_repo_overlay(
    files: dict[str, str], *, profile: Profile, base: RuleSet
) -> tuple[Rule, ...]:
    """把仓库规则文件里的 Markdown 表格行解析为请求级 `Rule` 覆盖.

    接受两类键（其余静默忽略）：

    * `base.key_patterns.is_repo(key)` — 仓库新增规则（任意键，`priority=1000`）；
    * `base.key_patterns.is_builtin(key)` 且键已存在于 `base.index_by_key`
      — 同键覆盖场景规则（只换 severity/title/priority/source，其余字段与
      正文仍走基础规则 / `{{repo_rules}}`）。

    围栏代码块（```）内的行视为示例，不参与解析。

    `profile` 只为接口稳定（后续按语言过滤仓库规则时使用），当前不参与判定。
    """
    rules: dict[str, Rule] = {}
    for path, content in files.items():
        in_fence = False
        for line in content.splitlines():
            if line.strip().startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            if not line.lstrip().startswith("|"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            key = cells[0].strip("`").strip() if cells else ""
            if not key:
                continue
            severity = (
                cells[1]
                if len(cells) > 1 and cells[1] in _SEVERITIES
                else "medium"
            )
            title = (cells[2] if len(cells) > 2 else "") or key
            source = {"kind": "repo", "path": path}
            if base.key_patterns.is_repo(key):
                rules[key] = Rule(
                    key=key,
                    title=title,
                    severity=severity,
                    priority=_REPO_RULE_PRIORITY,
                    source=source,
                )
            elif base.key_patterns.is_builtin(key) and key in base.index_by_key:
                # 同键覆盖只换元信息，weight/applies_to/body 等沿用基础规则。
                rules[key] = replace(
                    base.index_by_key[key],
                    severity=severity,
                    title=title,
                    priority=_REPO_RULE_PRIORITY,
                    source=source,
                )
    return tuple(rules.values())
