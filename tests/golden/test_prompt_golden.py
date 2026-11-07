"""prompt golden：默认场景的渲染结果必须与重构前逐字节一致.

回归护栏（Task 0 基线 / Task 9 装配迁移）：

* manifest 哈希 — golden 文件本身不得被改动；
* profile 的 improve / describe prompt 与旧 `reviewagent/prompts/*.md` 字节相同；
* 六个捕获场景（chunk_full / chunk_partial_refs / chunk_patch_prior /
  chunk_repo_ctx / batch / user_prompt）用当前门面 + assembly 重新渲染，
  与 golden 字节比对；
* 模板渲染 smoke：无残留占位符、规则块结尾换行符合旧实现的 `...\\n\\n`。

渲染前固定 profile / 规则集 / Config 默认值（Task 0 carry-forward 重要项 1），
不依赖运行环境变量；user_prompt 用隔离的空 telemetry store，绝不写仓库 `data/`。
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_ROOT = REPO_ROOT / "tests" / "golden" / "prompts" / "ssd-python-autotest"
MANIFEST = json.loads((REPO_ROOT / "tests" / "golden" / "manifest.json").read_text())

DIFF = (
    "diff --git a/core.py b/core.py\n"
    "--- a/core.py\n+++ b/core.py\n"
    "@@ -1,3 +1,4 @@\n import os\n+import json\n def f():\n-    return 1\n+    return 2\n"
)
SOURCE = "import os\nimport json\ndef f():\n    return 2\n"
VALID = {3, 5, 6}
REFS = [{"file": "tests/test_core.py", "line": 12, "ident": "f", "content": "assert f() == 2"}]
PRIOR = [{"severity": "high", "header": "资源未关闭", "label": "potential bug"}]

# 渲染使用的受控 Config 值（= settings/schema 的文档默认值）。
_RENDER_CONFIG = {
    "improve_partial_context_lines": 20,
    "improve_patch_context_lines": 3,
    "improve_full_files": 20,
    "improve_clip_diff_lines": 800,
    "improve_full_source_max_bytes": 0,
}


def _section(name: str) -> bytes:
    path = GOLDEN_ROOT / name
    assert path.exists(), f"missing golden: {path}"
    return path.read_bytes()


def _test_config():
    from reviewagent.config import current

    return dataclasses.replace(current(), **_RENDER_CONFIG)


@pytest.fixture()
def golden_env(monkeypatch, tmp_path):
    """固定 profile / 规则集 / Config / telemetry store，返回默认 profile."""
    from reviewagent.commands import improve as improve_module
    from reviewagent.scenario import profile as profile_module
    from reviewagent.scenario.profile import load_profile
    from reviewagent.scenario.rules import resolver
    from reviewagent.telemetry import store as store_module
    from reviewagent.telemetry.store import Store

    profile = load_profile("ssd-python-autotest")
    monkeypatch.setattr(profile_module, "_active", profile)
    monkeypatch.setattr(resolver, "_ACTIVE", resolver.assemble_base(profile))
    monkeypatch.setattr(improve_module, "config", _test_config())
    # 空 store：user_prompt 的「已发过的建议」段不得读取仓库 data/。
    monkeypatch.setattr(store_module, "_store", Store(tmp_path / "telemetry.db"))
    return profile


def _cmd(worktree: Path, repo_context: str = ""):
    from reviewagent.commands.improve import ImproveCommand

    (worktree / "core.py").write_text(SOURCE)
    (worktree / "tests").mkdir(exist_ok=True)
    (worktree / "tests" / "test_core.py").write_text("from core import f\nassert f() == 2\n")
    (worktree / ".diff").write_text(DIFF)
    cmd = ImproveCommand.__new__(ImproveCommand)
    cmd.project_id = 1
    cmd.mr_iid = 1
    cmd.ws = SimpleNamespace(worktree=worktree, diff_file=worktree / ".diff")
    cmd.repo_context = repo_context
    cmd._last_oc_result = None
    return cmd


def test_goldens_exist_and_are_stable():
    for name, expected in MANIFEST.items():
        assert hashlib.sha256(_section(name)).hexdigest() == expected


def test_profile_prompts_are_byte_equal_to_legacy():
    from reviewagent.scenario.profile import load_profile

    profile = load_profile(None)
    for name in ("improve", "describe"):
        legacy = (REPO_ROOT / "reviewagent" / "prompts" / f"{name}.md").read_bytes()
        current = (profile.root / profile.prompts[name]).read_bytes()
        assert current == legacy


def test_render_config_is_schema_defaults():
    """渲染用 Config 值必须与 schema 默认值一致（防环境/预设漂移）."""
    from reviewagent.settings.schema import SCHEMA_BY_NAME

    for name, value in _RENDER_CONFIG.items():
        assert SCHEMA_BY_NAME[name].default == value


def test_rendered_prompts_match_goldens(golden_env, tmp_path):
    cmd = _cmd(tmp_path)
    cases = {
        "chunk_full.txt": cmd._build_chunk_prompt(
            "core.py", DIFF, VALID, cmd.ws, cross_file_refs=[], strategy="full",
        ),
        "chunk_partial_refs.txt": cmd._build_chunk_prompt(
            "core.py", DIFF, VALID, cmd.ws, cross_file_refs=REFS, strategy="partial",
        ),
        "chunk_patch_prior.txt": cmd._build_chunk_prompt(
            "core.py", DIFF, VALID, cmd.ws, cross_file_refs=[],
            strategy="patch", prior_issues=PRIOR,
        ),
        "batch.txt": cmd._build_batch_prompt(
            ["core.py"], {"core.py": DIFF}, {"core.py": VALID}, cmd.ws,
            {"core.py": REFS}, {"core.py": "patch"}, {"core.py": PRIOR},
        ),
        "user_prompt.txt": cmd._build_user_prompt(),
    }
    repo_cmd = _cmd(tmp_path, repo_context="## AGENTS.md\n\n- 必须使用 with\n")
    cases["chunk_repo_ctx.txt"] = repo_cmd._build_chunk_prompt(
        "core.py", DIFF, VALID, repo_cmd.ws, cross_file_refs=[], strategy="full",
    )
    for name, text in cases.items():
        actual = text.encode("utf-8")
        expected = _section(name)
        assert actual == expected, f"golden mismatch: {name}"


def test_template_render_smoke(golden_env):
    from reviewagent.review.assembly import (
        ChunkPromptParts,
        build_chunk_prompt,
        build_rules_block,
    )

    general = "### 通用规则\n\n- R-1"
    assert build_rules_block("", general, profile=golden_env) == general + "\n\n"
    repo_block = build_rules_block("## AGENTS.md", general, profile=golden_env)
    assert repo_block.startswith("## 🔴 优先 2")
    assert "## AGENTS.md" in repo_block
    assert repo_block.endswith(general + "\n\n")

    prompt = build_chunk_prompt(
        ChunkPromptParts(
            cross_file_section="",
            rules_block="",
            prior_section="",
            file_path="a.py",
            file_diff="+x",
            source_block="SRC",
            valid_lines="a.py: [1]",
        ),
        profile=golden_env,
    )
    assert "{{" not in prompt
    assert "## 本次检视文件: `a.py`" in prompt
    assert prompt.endswith('只输出本文件的 suggestions。')


def test_general_rules_text_falls_back_to_loader(monkeypatch):
    """resolver 异常时回退旧 loader；两者都失败则照常抛出（不静默空规则块）."""
    from reviewagent.commands.improve import ImproveCommand

    def _boom():
        raise RuntimeError("resolver down")

    monkeypatch.setattr("reviewagent.scenario.rules.resolver.active_rule_set", _boom)
    monkeypatch.setattr(
        "reviewagent.prompts.loader.load_block",
        lambda name: f"fallback:{name}",
    )
    assert (
        ImproveCommand._general_rules_text()
        == "fallback:_general_rules_block"
    )

    def _boom2(name):
        raise RuntimeError("loader down")

    monkeypatch.setattr("reviewagent.prompts.loader.load_block", _boom2)
    with pytest.raises(RuntimeError, match="loader down"):
        ImproveCommand._general_rules_text()
