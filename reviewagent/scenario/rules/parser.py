"""规则包 Markdown 解析器（spec §6.3）.

`verbatim` 模式正文逐字节保留（只建键索引）；`structured` 模式把规则表与
`### <规则键> [标题]` 段落**合并**为同一套 `Rule`（表格供元数据、段落供正文
与正反例）。规则表按表头列名映射字段，同时兼容现代
`| 规则键 | 严重度 | 摘要 |` 与既有四列
`| 键 | 类别 | 反模式（命中条件）| 修复方向 |`。

语言过滤在合并之前完成：`applies_to.languages` 不包含当前语言时返回
`rules=()` 且 `body=""`（id/title/priority/render/fingerprint/path 保留），
空正文 + 空规则即表示「本包不适用于该语言」。
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import frontmatter

from reviewagent.scenario.rules.model import Rule, RulePack

SUPPORTED_SCHEMA = 1
_ALLOWED = {"schema_version", "id", "title", "priority", "render", "applies_to", "aliases", "actions"}
RULE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*$")
_HEADING_RE = re.compile(r"^###\s+(\S+)(?:[ \t]+(.*))?$")
_FENCE_RE = re.compile(r"^\s*```")
_SEPARATOR_RE = re.compile(r"^:?-+:?$")
_SEVERITIES = {"critical", "high", "medium", "low"}
_EXAMPLE_RE = re.compile(r"```(bad|good)\n(.*?)```", flags=re.S)

# 规范化字段 -> 可接受的表头列名。
_HEADER_ALIASES: dict[str, frozenset[str]] = {
    "key": frozenset({"规则键", "键", "key"}),
    "severity": frozenset({"严重度", "severity"}),
    "title": frozenset({"类别", "摘要", "title"}),
    "bad": frozenset({"反模式", "反模式（命中条件）", "bad"}),
    "fix": frozenset({"修复方向", "fix"}),
    "aliases": frozenset({"aliases"}),
    "actions": frozenset({"actions"}),
    "detect_hints": frozenset({"detect_hints"}),
}


def _require_str(fm: dict[str, Any], name: str) -> str:
    value = fm.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"rule pack missing '{name}'")
    return value.strip()


def _normalize_cell(cell: str) -> str:
    return cell.strip().strip("`").strip()


def _split_multi(cell: str) -> tuple[str, ...]:
    parts = re.split(r"[,;，；]", cell)
    return tuple(item.strip().strip("`").strip() for item in parts if _normalize_cell(item))


def _is_separator_row(cells: list[str]) -> bool:
    return bool(cells) and all(_SEPARATOR_RE.match(cell.strip()) for cell in cells)


def _iter_table_blocks(body: str) -> Iterator[list[list[str]]]:
    """按连续 ``|`` 行切分表格块；围栏代码块内的 ``|`` 行不参与。"""
    block: list[list[str]] = []
    in_fence = False
    for line in body.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            if block:
                yield block
                block = []
            continue
        if in_fence:
            continue
        stripped = line.strip()
        if len(stripped) > 1 and stripped.startswith("|"):
            block.append([cell.strip() for cell in stripped.strip("|").split("|")])
        elif block:
            yield block
            block = []
    if block:
        yield block


def _column_map(header: list[str]) -> dict[str, int]:
    cols: dict[str, int] = {}
    for idx, cell in enumerate(header):
        name = _normalize_cell(cell)
        for field, aliases in _HEADER_ALIASES.items():
            if name in aliases and field not in cols:
                cols[field] = idx
                break
    return cols


def _cell(row: list[str], cols: dict[str, int], field: str) -> str:
    idx = cols.get(field)
    return row[idx].strip() if idx is not None and idx < len(row) else ""


def _table_rules(
    body: str, pack_id: str, priority: int, applies_to: dict[str, Any]
) -> tuple[Rule, ...]:
    rules: list[Rule] = []
    seen: set[str] = set()
    for block in _iter_table_blocks(body):
        if not block:
            continue
        header = block[0]
        cols = _column_map(header)
        if "key" not in cols:
            # 非规则表（散文表格），静默跳过。
            continue
        title_header = _normalize_cell(header[cols["title"]]) if "title" in cols else None
        legacy = title_header == "类别"
        start = 1
        if start < len(block) and _is_separator_row(block[start]):
            start += 1
        for row in block[start:]:
            key = _normalize_cell(_cell(row, cols, "key"))
            if not RULE_KEY_RE.match(key):
                continue
            if key in seen:
                raise ValueError(f"duplicate rule key {key!r} in table of pack {pack_id}")
            seen.add(key)
            severity = _normalize_cell(_cell(row, cols, "severity"))
            if severity not in _SEVERITIES:
                severity = "medium"
            title = _cell(row, cols, "title") or key
            if legacy:
                bad = _cell(row, cols, "bad")
                fix = _cell(row, cols, "fix")
                rule_body = f"{bad}\n\n{fix}".strip()
            else:
                rule_body = ""
            rules.append(Rule(
                key=key,
                title=title,
                body=rule_body,
                severity=severity,
                priority=priority,
                applies_to=dict(applies_to),
                aliases=_split_multi(_cell(row, cols, "aliases")),
                actions=_split_multi(_cell(row, cols, "actions")),
                detect_hints=_split_multi(_cell(row, cols, "detect_hints")),
                source={"kind": "profile", "pack_id": pack_id},
            ))
    return tuple(rules)


def _iter_sections(body: str) -> Iterator[tuple[str, str | None, str]]:
    """逐行扫描，仅在围栏代码块外按 ``### <规则键> [标题]`` 切段。"""
    in_fence = False
    current_key: str | None = None
    current_title: str | None = None
    current_lines: list[str] = []

    def flush() -> tuple[str, str | None, str] | None:
        if current_key is None:
            return None
        return current_key, current_title, "\n".join(current_lines)

    for line in body.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            if current_key is not None:
                current_lines.append(line)
            continue
        if not in_fence:
            match = _HEADING_RE.match(line)
            if match:
                key = match.group(1).strip("`")
                if RULE_KEY_RE.match(key):
                    section = flush()
                    if section is not None:
                        yield section
                    current_key = key
                    current_title = (match.group(2) or "").strip() or None
                    current_lines = []
                    continue
                # 非规则键标题：结束当前段落，其后的正文视为包级散文。
                section = flush()
                if section is not None:
                    yield section
                current_key = None
                current_title = None
                current_lines = []
                continue
        if current_key is not None:
            current_lines.append(line)
    section = flush()
    if section is not None:
        yield section


def _parse_examples(section: str) -> tuple[dict[str, str], ...]:
    return tuple(
        {"kind": kind, "code": code.strip("\n"), "lang": "text"}
        for kind, code in _EXAMPLE_RE.findall(section)
    )


def _structured_rules(
    body: str,
    pack_id: str,
    priority: int,
    fm: dict[str, Any],
    applies_to: dict[str, Any],
) -> tuple[Rule, ...]:
    table_rules = {rule.key: rule for rule in _table_rules(body, pack_id, priority, applies_to)}
    sections: dict[str, tuple[str | None, str]] = {}
    for key, title, text in _iter_sections(body):
        if key in sections:
            raise ValueError(f"duplicate rule section {key!r} in pack {pack_id}")
        sections[key] = (title, text)

    pack_aliases = tuple(fm.get("aliases") or ())
    pack_actions = tuple(fm.get("actions") or ())

    rules: list[Rule] = []
    for key in [*table_rules, *(k for k in sections if k not in table_rules)]:
        table = table_rules.get(key)
        section = sections.get(key)
        if section is not None:
            section_title, section_text = section
            section_body = section_text.strip()
            examples = _parse_examples(section_text)
        else:
            section_title, section_body, examples = None, "", ()
        title = section_title or (table.title if table else None) or key
        # 无段落时回退到表格正文：现代表为空字符串，legacy 表为「反模式 + 修复方向」。
        rule_body = section_body if section is not None else (table.body if table else "")
        rules.append(Rule(
            key=key,
            title=title,
            body=rule_body,
            severity=table.severity if table else "medium",
            priority=priority,
            applies_to=dict(applies_to),
            examples=examples,
            aliases=(table.aliases if table and table.aliases else pack_aliases),
            actions=(table.actions if table and table.actions else pack_actions),
            detect_hints=table.detect_hints if table else (),
            source={"kind": "profile", "pack_id": pack_id},
        ))
    return tuple(rules)


def _validate_applies_to(fm: dict[str, Any], path: Path) -> dict[str, Any]:
    applies_to = fm.get("applies_to")
    if applies_to is None:
        return {}
    if not isinstance(applies_to, dict):
        raise ValueError(f"applies_to must be a mapping in {path}")
    paths = applies_to.get("paths")
    if paths is not None:
        if not isinstance(paths, (list, tuple)):
            raise ValueError(f"applies_to.paths must be a list in {path}")
        for pattern in paths:
            if not isinstance(pattern, str) or not pattern.strip():
                raise ValueError(f"applies_to.paths entry must be non-empty str in {path}: {pattern!r}")
            if pattern.startswith("/"):
                raise ValueError(
                    f"applies_to.paths must be repo-relative POSIX glob in {path}: {pattern!r}"
                )
            if "\\" in pattern:
                raise ValueError(
                    f"applies_to.paths must use POSIX separators in {path}: {pattern!r}"
                )
            if ".." in pattern.split("/"):
                raise ValueError(
                    f"applies_to.paths must not contain '..' segments in {path}: {pattern!r}"
                )
    return dict(applies_to)


def parse_pack(path: Path, *, language: str, root: Path) -> RulePack:
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError(f"rule pack escapes profile root: {path}")
    raw = resolved.read_bytes()
    md = frontmatter.loads(raw.decode("utf-8"))
    fm = dict(md.metadata)
    unknown = set(fm) - _ALLOWED
    if unknown:
        raise ValueError(f"unknown frontmatter fields {sorted(unknown)} in {path}")
    if fm.get("schema_version") != SUPPORTED_SCHEMA:
        raise ValueError(f"unsupported rule pack schema_version {fm.get('schema_version')!r} in {path}")
    pack_id = _require_str(fm, "id")
    title = fm.get("title")
    if not isinstance(title, str) or not title.strip():
        title = pack_id
    else:
        title = title.strip()
    render = fm.get("render", "structured")
    if render not in ("verbatim", "structured"):
        raise ValueError(f"unknown render mode {render!r} in {path}")
    try:
        priority = int(fm.get("priority", 800))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid priority {fm.get('priority')!r} in {path}") from exc
    if not 0 <= priority <= 1000:
        raise ValueError(f"priority out of range in {path}: {priority}")
    applies_to = _validate_applies_to(fm, path)
    langs = applies_to.get("languages")
    if langs and language not in langs:
        # 空正文 + 空规则表示该包不适用于当前语言。
        body: str = ""
        rules: tuple[Rule, ...] = ()
    elif render == "verbatim":
        body = md.content
        rules = _table_rules(md.content, pack_id, priority, applies_to)
    else:
        body = md.content
        rules = _structured_rules(md.content, pack_id, priority, fm, applies_to)
    index = {rule.key: rule for rule in rules}
    if len(index) != len(rules):
        raise ValueError(f"duplicate rule key in {path}")
    return RulePack(
        id=pack_id,
        title=title,
        priority=priority,
        render=render,
        body=body,
        rules=rules,
        index_by_key=index,
        path=str(resolved),
        fingerprint=hashlib.sha256(raw).hexdigest(),
        applies_to=dict(fm.get("applies_to") or {}),
    )
