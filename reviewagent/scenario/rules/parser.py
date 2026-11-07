"""规则包 Markdown 解析器（spec §6.3）.

`verbatim` 模式正文逐字节保留（只建键索引），`structured` 模式按
`### <规则键> [标题]` 段落与 `bad`/`good` 围栏解析；规则表两种模式共用，
现有四列表格（键 / 类别 / 反模式 / 修复方向）原样兼容。
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import frontmatter

from reviewagent.scenario.rules.model import Rule, RulePack

SUPPORTED_SCHEMA = 1
_ALLOWED = {"schema_version", "id", "title", "priority", "render", "applies_to", "aliases", "actions"}
RULE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*$")


def _require_str(fm: dict[str, Any], name: str) -> str:
    value = fm.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"rule pack missing '{name}'")
    return value.strip()


def _table_rules(body: str, pack_id: str, priority: int) -> tuple[Rule, ...]:
    rules: list[Rule] = []
    for line in body.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not cells:
            continue
        key = cells[0].strip("`")
        if not RULE_KEY_RE.match(key):
            continue
        title = cells[1] if len(cells) > 1 else key
        bad = cells[2] if len(cells) > 2 else ""
        fix = cells[3] if len(cells) > 3 else ""
        rules.append(Rule(key=key, title=title, body=f"{bad}\n\n{fix}".strip(),
                          priority=priority, source={"kind": "profile", "pack_id": pack_id}))
    return tuple(rules)


def _structured_rules(body: str, pack_id: str, priority: int, fm: dict[str, Any]) -> tuple[Rule, ...]:
    rules: list[Rule] = []
    # `### R-LOG 日志规范`：键为首个空白分隔 token，尾随标题只作展示。
    parts = re.split(r"(?m)^###\s+(\S+)(?:\s+.*)?$", body)
    if len(parts) == 1:
        return _table_rules(body, pack_id, priority)
    for idx in range(1, len(parts), 2):
        key, section = parts[idx].strip("`"), parts[idx + 1]
        if not RULE_KEY_RE.match(key):
            continue
        examples = []
        for kind, code in re.findall(r"```(bad|good)\n(.*?)```", section, flags=re.S):
            examples.append({"kind": kind, "code": code.strip("\n"), "lang": "text"})
        rules.append(Rule(
            key=key, title=key, body=section.strip(), examples=tuple(examples), priority=priority,
            applies_to=dict(fm.get("applies_to") or {}),
            aliases=tuple(fm.get("aliases") or ()), actions=tuple(fm.get("actions") or ()),
            source={"kind": "profile", "pack_id": pack_id},
        ))
    return tuple(rules)


def parse_pack(path: Path, *, language: str, root: Path) -> RulePack:
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError(f"rule pack escapes profile root: {path}")
    md = frontmatter.load(resolved)
    fm = dict(md.metadata)
    unknown = set(fm) - _ALLOWED
    if unknown:
        raise ValueError(f"unknown frontmatter fields {sorted(unknown)} in {path}")
    if fm.get("schema_version") != SUPPORTED_SCHEMA:
        raise ValueError(f"unsupported rule pack schema_version {fm.get('schema_version')!r} in {path}")
    pack_id = _require_str(fm, "id")
    render = fm.get("render", "structured")
    if render not in ("verbatim", "structured"):
        raise ValueError(f"unknown render mode {render!r} in {path}")
    priority = int(fm.get("priority", 800))
    if not 0 <= priority <= 1000:
        raise ValueError(f"priority out of range in {path}: {priority}")
    langs = (fm.get("applies_to") or {}).get("languages")
    if langs and language not in langs:
        rules: tuple[Rule, ...] = ()
    elif render == "verbatim":
        rules = _table_rules(md.content, pack_id, priority)
    else:
        rules = _structured_rules(md.content, pack_id, priority, fm)
    index = {r.key: r for r in rules}
    if len(index) != len(rules):
        raise ValueError(f"duplicate rule key in {path}")
    return RulePack(
        id=pack_id, title=fm.get("title", pack_id), priority=priority, render=render,
        body=md.content, rules=rules, index_by_key=index, path=str(resolved),
        fingerprint=hashlib.sha256(resolved.read_bytes()).hexdigest(),
    )
