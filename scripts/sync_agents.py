"""Sync reviewagent/prompts/*.md → ~/.config/opencode/agent/*.md.

Why:
    opencode agent config is keyed by file basename. Our reviewagent prompts
    live in repo (`reviewagent/prompts/<name>.md`) AND must also exist in
    `~/.config/opencode/agent/<name>.md` to be registered with the opencode
    serve that ReviewAgent calls.

    This script is idempotent — run it after modifying a prompt to keep both
    sides in sync.

Usage:
    python scripts/sync_agents.py
    python scripts/sync_agents.py --agent-dir /custom/path
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import frontmatter

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "reviewagent" / "prompts"
DEFAULT_AGENT_DIR = Path.home() / ".config" / "opencode" / "agent"

# Map our prompt name → opencode mode + tool overrides
# (mode=primary for simplicity; tools are explicitly listed so opencode knows
#  what to disable)
AGENT_META: dict[str, dict[str, object]] = {
    "describe": {
        "description": "生成 MR 的中文 Description（与 PR-Agent 展示一致）",
        "tools": {"write": False, "edit": False, "bash": False, "webfetch": False},
    },
    "review": {
        "description": "对 MR diff 做深度代码检视（与 PR-Agent 同款 key_issues_to_review 输出）",
        "tools": {"write": False, "edit": False, "bash": False, "webfetch": False},
    },
    "improve": {
        "description": "对 MR diff 输出可 Apply 的代码改进建议（PR-Agent code-suggestion 风格）",
        "tools": {"write": False, "edit": False, "bash": False, "webfetch": False},
    },
    "weekly_change_summary": {
        "description": "把本周合并 MR 列表汇总成有洞察的中文变更摘要（周报第二节）",
        "tools": {"write": False, "edit": False, "bash": False, "webfetch": False},
    },
    "weekly_quality_scan": {
        "description": "基于本周代码检视真实产出，自由发挥撰写有判断力的代码质量综述（周报第三节）",
        "tools": {"write": False, "edit": False, "bash": False, "webfetch": False},
    },
    "weekly_inspection_summary": {
        "description": "把本周检视聚合数据(含翻译后的中文问题类别)润色成叙事性「本周检视汇总」（周报第一节）",
        "tools": {"write": False, "edit": False, "bash": False, "webfetch": False},
    },
}


def build_agent_md(name: str, src: frontmatter.Frontmatter) -> str:
    """Build opencode agent Markdown from a frontmatter-loaded prompt."""
    meta = AGENT_META.get(name)
    if meta is None:
        raise ValueError(
            f"agent {name!r} has no entry in AGENT_META — add one before syncing"
        )

    desc = meta["description"]
    assert isinstance(meta["tools"], dict)
    tools = meta["tools"]
    tools_yaml = "\n".join(f"  {k}: {'true' if v else 'false'}" for k, v in tools.items())

    # Strip our local 'output_schema' (opencode doesn't honor it) but keep
    # any structure from the agent body itself.
    body = src.content

    return (
        "---\n"
        f"name: {name}\n"
        f"description: |\n  {desc}\n"
        f"mode: primary\n"
        f"tools:\n{tools_yaml}\n"
        "---\n\n"
        f"{body}"
    )


def sync(prompts_dir: Path, agent_dir: Path) -> list[Path]:
    """Copy each prompts/*.md into agent/*.md (overwriting). Returns list written."""
    if not prompts_dir.exists():
        raise FileNotFoundError(f"prompts dir not found: {prompts_dir}")
    agent_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for md_path in sorted(prompts_dir.glob("*.md")):
        name = md_path.stem
        if name not in AGENT_META:
            print(f"  SKIP {name} (no AGENT_META entry)")
            continue

        fm = frontmatter.load(md_path)
        out = build_agent_md(name, fm)
        target = agent_dir / f"{name}.md"
        target.write_text(out, encoding="utf-8")
        written.append(target)
        print(f"  wrote {target}  ({len(out)} bytes)")

    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--agent-dir",
        type=Path,
        default=DEFAULT_AGENT_DIR,
        help=f"opencode agent dir (default: {DEFAULT_AGENT_DIR})",
    )
    args = parser.parse_args()
    written = sync(PROMPTS_DIR, args.agent_dir)
    print(f"\n[ok] synced {len(written)} agent file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
