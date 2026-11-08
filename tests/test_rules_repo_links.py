"""规则文件动态发现：跟随 AGENTS.md 链接，不绑定目录/前缀/条数."""
from __future__ import annotations

from types import SimpleNamespace

from reviewagent.scenario.rules.repo import (
    TRUNCATION_MARKER,
    _discover_rule_files,
    render_instruction_files,
    render_rule_index,
)


class FakeGitLab:
    """最小 GitLab 客户端替身：files 映射 + 目录列举."""

    def __init__(self, files: dict[str, str]):
        self.files = files

    def _get_project(self, project_id: int):
        return SimpleNamespace(default_branch="main")

    def get_file_at_sha(self, project_id: int, path: str, ref: str):
        return self.files.get(path)

    def list_repository_tree(self, project_id: int, path: str, ref: str, *, recursive: bool = True):
        prefix = f"{path.rstrip('/')}/" if path else ""
        hits = [p for p in self.files if p.startswith(prefix)]
        if not recursive:
            hits = [p for p in hits if "/" not in p[len(prefix):]]
        return [{"type": "blob", "path": p} for p in hits]


def _discover(files: dict[str, str], entries=("AGENTS.md",), explicit_dir: str = ""):
    client = FakeGitLab(files)
    return _discover_rule_files(client, 1, "main", tuple(entries), explicit_dir)


def test_discovery_follows_agents_link_to_rules_index_and_files():
    files = {
        "AGENTS.md": "规则见 [`.agents/rules/`](.agents/rules/index.md)。\n",
        ".agents/rules/index.md": "| 规则键 | 严重度 | One-line summary |\n|---|---|---|\n"
        "| `SSD-RULE-NO-BARE-PRINT` | high | 禁止裸 print |\n",
        ".agents/rules/SSD-RULE-NO-BARE-PRINT.md": "### SSD-RULE-NO-BARE-PRINT 禁止裸 print\n正文\n",
        ".agents/rules/note.txt": "not markdown",
    }
    found = _discover(files)
    assert list(found) == [
        "AGENTS.md",
        ".agents/rules/index.md",
        ".agents/rules/SSD-RULE-NO-BARE-PRINT.md",
    ]


def test_discovery_supports_renamed_rules_directory():
    """目录从 .agents/rules 改成 .qoder/rules 时无需改代码."""
    files = {
        "AGENTS.md": "规则见 [`.qoder/rules/`](.qoder/rules/index.md)。\n",
        ".qoder/rules/index.md": "[规则一](ACME-RULE-ONE.md)\n",
        ".qoder/rules/ACME-RULE-ONE.md": "### ACME-RULE-ONE 规则一\n",
    }
    found = _discover(files)
    assert ".qoder/rules/index.md" in found
    assert ".qoder/rules/ACME-RULE-ONE.md" in found


def test_discovery_expands_directory_link_one_level():
    files = {
        "AGENTS.md": "见 [规则目录](.agents/rules/)。\n",
        ".agents/rules/index.md": "index\n",
        ".agents/rules/SSD-RULE-A.md": "### SSD-RULE-A A\n",
    }
    found = _discover(files)
    assert list(found) == ["AGENTS.md", ".agents/rules/index.md", ".agents/rules/SSD-RULE-A.md"]


def test_discovery_rejects_external_absolute_and_traversal_links():
    files = {
        "AGENTS.md": (
            "[外链](https://evil.example/x.md) "
            "[绝对](/etc/passwd.md) "
            "[越界](../../secret.md) "
            "[协议](mailto:a@b.c) "
            "[同目录](docs/rules.md)"
        ),
        "docs/rules.md": "### SSD-RULE-OK 正常\n",
        "secret.md": "### SSD-RULE-EVIL 越界\n",
    }
    found = _discover(files)
    assert list(found) == ["AGENTS.md", "docs/rules.md"]


def test_discovery_depth_is_capped_and_missing_links_tolerated():
    files = {
        "AGENTS.md": "[b](b.md) [missing](missing.md)\n",
        "b.md": "[c](c.md)\n",
        "c.md": "[d](d.md)\n",
        "d.md": "### SSD-RULE-DEEP 太深\n",
    }
    found = _discover(files)
    assert list(found) == ["AGENTS.md", "b.md", "c.md"]


def test_discovery_supports_explicit_rules_dir_override():
    files = {
        "AGENTS.md": "无链接\n",
        "legacy/rules/SSD-RULE-LEGACY.md": "### SSD-RULE-LEGACY 旧规则\n",
    }
    found = _discover(files, explicit_dir="legacy/rules")
    assert list(found) == ["AGENTS.md", "legacy/rules/SSD-RULE-LEGACY.md"]


def test_runtime_rule_index_extracts_keys_severity_and_titles():
    files = {
        ".agents/rules/SSD-RULE-A.md": "### SSD-RULE-A 标题 A\n正文\n",
        ".agents/rules/SSD-RULE-B.md": "| 规则键 | 严重度 | 摘要 |\n|---|---|---|\n"
        "| `SSD-RULE-B` | critical | 标题 B |\n",
        ".agents/rules/plain.md": "| `R-LOG` | high | 通用规则不进仓库索引 |\n",
    }
    index = render_rule_index(files)
    assert "<rule_index" in index
    assert "SSD-RULE-A | 标题 A" in index
    assert "SSD-RULE-B | critical | 标题 B" in index
    assert "R-LOG" not in index


def test_runtime_rule_index_ignores_fenced_tables():
    files = {
        "index.md": "```markdown\n| `SSD-RULE-FENCED` | high | 示例 |\n```\n"
        "| `SSD-RULE-REAL` | low | 真规则 |\n"
    }
    index = render_rule_index(files)
    assert "SSD-RULE-FENCED" not in index
    assert "SSD-RULE-REAL | low | 真规则" in index


def test_render_instruction_files_keeps_later_files_within_fair_budget():
    files = {
        "AGENTS.md": "\n".join(f"line{i}" for i in range(40)),
        "rules/SECOND.md": "second-a\nsecond-b\n",
    }
    text = render_instruction_files(files, max_lines=30)
    # 第一个文件超预算被截断，但第二个文件仍必须出现（不能静默丢弃规则文件）
    assert TRUNCATION_MARKER in text
    assert '<file path="rules/SECOND.md"' in text
    assert "second-b" in text
