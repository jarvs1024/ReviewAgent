"""仓库规则目录：配置化递归扫描（默认 `.agents/rules`），不跟随任何链接."""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from reviewagent import config as config_module
from reviewagent.config import current
from reviewagent.scenario.rules.repo import (
    TRUNCATION_MARKER,
    _discover_rule_files,
    _fetch_all_rule_files,
    _normalize_rules_dir,
    render_instruction_files,
    render_rule_index,
)


class FakeGitLab:
    """最小 GitLab 客户端替身：files 映射 + 目录列举（缺失目录抛错）."""

    def __init__(self, files: dict[str, str], *, missing_dirs: set[str] | None = None):
        self.files = files
        self.missing_dirs = missing_dirs or set()
        self.calls = 0

    def _get_project(self, project_id: int):
        return SimpleNamespace(default_branch="main")

    def get_file_at_sha(self, project_id: int, path: str, ref: str):
        return self.files.get(path)

    def list_repository_tree(
        self, project_id: int, path: str, ref: str, *, recursive: bool = True
    ):
        self.calls += 1
        if path in self.missing_dirs:
            raise RuntimeError("404 Tree Not Found")
        prefix = f"{path.rstrip('/')}/" if path else ""
        hits = [p for p in self.files if p.startswith(prefix)]
        if not recursive:
            hits = [p for p in hits if "/" not in p[len(prefix):]]
        if not hits:
            raise RuntimeError("404 Tree Not Found")
        return [{"type": "blob", "path": p} for p in hits]


def _discover(files: dict[str, str], rules_dir: str = ".agents/rules"):
    return _discover_rule_files(FakeGitLab(files), 1, "main", rules_dir)


def test_scan_default_dir_recursively_and_md_only():
    files = {
        ".agents/rules/SSD-RULE-TOP.md": "### SSD-RULE-TOP 顶级\n",
        ".agents/rules/nested/SSD-RULE-NESTED.md": "### SSD-RULE-NESTED 嵌套\n",
        ".agents/rules/notes.txt": "not markdown",
        "docs/SSD-RULE-OUTSIDE.md": "### SSD-RULE-OUTSIDE 目录外\n",
        "AGENTS.md": "规则见 [`.agents/rules/`](.agents/rules/)，但入口文件不参与发现。\n",
    }
    found = _discover(files)
    assert list(found) == [
        ".agents/rules/SSD-RULE-TOP.md",
        ".agents/rules/nested/SSD-RULE-NESTED.md",
    ]


def test_scan_uses_configured_dir():
    files = {
        ".agents/rules/SSD-RULE-DEFAULT.md": "### SSD-RULE-DEFAULT\n",
        ".qoder/rules/ACME-RULE-CUSTOM.md": "### ACME-RULE-CUSTOM 自定义目录\n",
    }
    assert list(_discover(files)) == [".agents/rules/SSD-RULE-DEFAULT.md"]
    assert list(_discover(files, ".qoder/rules/")) == [".qoder/rules/ACME-RULE-CUSTOM.md"]


@pytest.mark.parametrize("rules_dir", ["", "  ", "/abs/rules", "../outside", "a/../../b"])
def test_scan_disabled_or_invalid_dir_returns_nothing(rules_dir: str):
    assert _normalize_rules_dir(rules_dir) == ""
    assert _discover({".agents/rules/SSD-RULE-A.md": "x"}, rules_dir) == {}


def test_missing_directory_is_tolerated():
    client = FakeGitLab({}, missing_dirs={".agents/rules"})
    assert _discover_rule_files(client, 1, "main", ".agents/rules") == {}


def test_scan_caps_discovered_file_count():
    files = {
        f".agents/rules/SSD-RULE-{i:03d}.md": f"### SSD-RULE-{i:03d}\n"
        for i in range(65)
    }
    found = _discover(files)
    assert len(found) == 60
    assert list(found)[0] == ".agents/rules/SSD-RULE-000.md"


def test_scan_skips_oversized_file():
    files = {
        ".agents/rules/SSD-RULE-BIG.md": "x" * (512 * 1024 + 1),
        ".agents/rules/SSD-RULE-OK.md": "### SSD-RULE-OK\n",
    }
    assert list(_discover(files)) == [".agents/rules/SSD-RULE-OK.md"]


def test_fetch_reads_configured_dir_and_caches(monkeypatch):
    from reviewagent.scenario.rules import repo

    files = {".qoder/rules/ACME-RULE-A.md": "### ACME-RULE-A 配置目录\n"}
    client = FakeGitLab(files)
    repo._files_cache.clear()
    repo._files_cache_ts.clear()
    configured = replace(current(), repo_context_rules_dir=".qoder/rules/")
    monkeypatch.setattr(config_module, "_current", configured)

    assert list(_fetch_all_rule_files(client, 7)) == [".qoder/rules/ACME-RULE-A.md"]
    assert _fetch_all_rule_files(client, 7) == {
        path: content.rstrip() for path, content in files.items()
    }
    assert client.calls == 1, "TTL 缓存命中后不得重复打 GitLab"


def test_fetch_disabled_dir_skips_gitlab(monkeypatch):
    from reviewagent.scenario.rules import repo

    client = FakeGitLab({".agents/rules/SSD-RULE-A.md": "x"})
    repo._files_cache.clear()
    repo._files_cache_ts.clear()
    monkeypatch.setattr(config_module, "_current", replace(current(), repo_context_rules_dir=""))
    assert _fetch_all_rule_files(client, 7) == {}
    assert client.calls == 0


def test_runtime_rule_index_follows_configured_prefix(monkeypatch):
    files = {
        ".agents/rules/a.md": "### SSD-RULE-A 标题 A\n正文\n",
        ".agents/rules/b.md": "| 规则键 | 严重度 | 摘要 |\n|---|---|---|\n"
        "| `SSD-RULE-B` | critical | 标题 B |\n"
        "| `ZZZ-RULE-C` | high | 其他仓库前缀 |\n",
    }
    monkeypatch.setattr(
        config_module, "_current", replace(current(), rule_key_prefix="SSD")
    )
    index = render_rule_index(files)
    assert "SSD-RULE-A | 标题 A" in index
    assert "SSD-RULE-B | critical | 标题 B" in index
    assert "ZZZ-RULE-C" not in index
    assert "R-LOG" not in index


def test_runtime_rule_index_auto_prefix_accepts_any_uppercase_prefix(monkeypatch):
    monkeypatch.setattr(
        config_module, "_current", replace(current(), rule_key_prefix="auto")
    )
    index = render_rule_index({"x.md": "### ZZZ-RULE-C 其他前缀\n"})
    assert "ZZZ-RULE-C | 其他前缀" in index


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
        ".agents/rules/first.md": "\n".join(f"line{i}" for i in range(40)),
        ".agents/rules/second.md": "second-a\nsecond-b\n",
    }
    text = render_instruction_files(files, max_lines=30)
    # 第一个文件超预算被截断，但第二个文件仍必须出现（不能静默丢弃规则文件）
    assert TRUNCATION_MARKER in text
    assert '<file path=".agents/rules/second.md"' in text
    assert "second-b" in text
