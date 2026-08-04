"""Materialise reviewagent/prompts/*.md as .qoder/agents/*.md for QoderCLI ACP.

The QoderCLI ACP server reads subagent definitions from the project-level
`.qoder/agents/<name>.md` files at startup. We translate our frontmatter
`tools: {write:false, ...}` (whitelist-negative) into QoderCLI's
`disallowedTools: [...]` (blacklist) and always pin read-only tools +
safe defaults (maxTurns, model, permissionMode).

This script is invoked at worker bootstrap and is idempotent: files are
only rewritten when the source mtime is newer than the dest mtime.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import frontmatter


# Tools we always disable — ReviewAgent prompts must be read-only against
# the workdir and must not perform web fetches.
_HARD_DENY = ["Write", "Edit", "Bash", "WebFetch", "WebSearch"]
# Tools we always allow when nothing else is specified.
_HARD_ALLOW = ["Read", "Grep", "Glob", "Agent"]


# Canonical case for each tool name we care about. frontmatter lowercases
# keys, so we map explicitly to keep the rendered output stable.
_TOOL_CASE = {
    "write": "Write",
    "edit": "Edit",
    "bash": "Bash",
    "webfetch": "WebFetch",
    "websearch": "WebSearch",
    "read": "Read",
    "grep": "Grep",
    "glob": "Glob",
    "agent": "Agent",
}


def _canonical(name: str) -> str:
    return _TOOL_CASE.get(str(name).lower(), str(name).capitalize())


def _render_disallowed(tools: dict) -> list[str]:
    """Return the list of tools to deny. Falls back to _HARD_DENY when the
    frontmatter did not declare any tool (so an empty frontmatter still
    blocks the dangerous set)."""
    declared = {k.lower(): v for k, v in (tools or {}).items()}
    out = [_canonical(k) for k, v in declared.items() if v is False]
    for tool in _HARD_DENY:
        if tool not in out:
            out.append(tool)
    return out


def _render_allowed(tools: dict) -> list[str]:
    """Return the list of tools to allow. If the prompt explicitly enables
    some tools, honour that set; otherwise fall back to _HARD_ALLOW."""
    declared = {k.lower(): v for k, v in (tools or {}).items()}
    positives = [_canonical(k) for k, v in declared.items() if v is True]
    return positives if positives else list(_HARD_ALLOW)


def _should_rewrite(src: Path, dst: Path) -> bool:
    if not dst.exists():
        return True
    return src.stat().st_mtime_ns > dst.stat().st_mtime_ns


def _render_agent(name: str, description: str, body: str, tools: dict) -> str:
    disallowed = _render_disallowed(tools)
    allowed = _render_allowed(tools)
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"tools: [{', '.join(allowed)}]\n"
        f"disallowedTools: [{', '.join(disallowed)}]\n"
        "permissionMode: default\n"
        "maxTurns: 3\n"
        "model: inherit\n"
        "---\n\n"
        f"{body.rstrip()}\n"
    )


def sync_qoder_agents(prompts_dir: Path, agents_dir: Path) -> list[Path]:
    """Mirror every non-private prompt into `agents_dir/<stem>.md`.

    Returns the list of absolute paths actually written (skips entries
    that did not need rewriting). Private blocks (filename starting with
    `_`) are excluded by design.
    """
    agents_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for src in sorted(prompts_dir.glob("*.md")):
        if src.stem.startswith("_"):
            continue
        meta = frontmatter.load(src)
        name = str(meta.get("name") or src.stem)
        description = str(meta.get("description") or "").strip()
        tools = dict(meta.get("tools") or {})
        dst = agents_dir / f"{src.stem}.md"
        if not _should_rewrite(src, dst):
            continue
        dst.write_text(_render_agent(name, description, meta.content, tools))
        written.append(dst.resolve())
    return written


def main() -> None:  # pragma: no cover - thin CLI wrapper
    from reviewagent.config import config
    from reviewagent.prompts.loader import PROMPTS_DIR

    prompts_dir = PROMPTS_DIR
    agents_dir = Path.cwd() / ".qoder" / "agents"
    paths = sync_qoder_agents(prompts_dir, agents_dir)
    print(f"synced {len(paths)} qoder agents to {agents_dir}")


if __name__ == "__main__":
    main()
