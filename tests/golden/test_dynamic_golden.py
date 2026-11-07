"""Task 14 三态 golden：动态证据渲染必须与现网 prompt 逐字节一致.

覆盖四态中的三态：
* 规则启用 + refs 空 → 空模板（`chunk_full.txt` 子串）；
* 规则启用 + refs 非空 → items 模板（`chunk_partial_refs.txt` 子串）；
* provider 降级 → 强制空模板（即使有 refs）；
* 规则禁用 → 门面 chunk / batch prompt 完全无 cross-file section。
外加 `refs_by_file` 分桶 / 剥离 `review_file` 的单元测试。
"""
from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace

import pytest

from reviewagent.review.context import render_cross_file_section
from reviewagent.review.dynamic.model import DynamicEvidence
from reviewagent.review.dynamic.runner import refs_by_file
from reviewagent.scenario.dynamic import load_dynamic_rules
from reviewagent.scenario.profile import load_profile

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_ROOT = REPO_ROOT / "tests" / "golden" / "prompts" / "ssd-python-autotest"

DIFF = (
    "diff --git a/core.py b/core.py\n"
    "--- a/core.py\n+++ b/core.py\n"
    "@@ -1,3 +1,4 @@\n import os\n+import json\n def f():\n-    return 1\n+    return 2\n"
)
SOURCE = "import os\nimport json\ndef f():\n    return 2\n"
VALID = {5}
REFS = [{"file": "tests/test_core.py", "line": 12, "ident": "f", "content": "assert f() == 2"}]
RULE_KEY = "DYN-CROSS-FILE-REFS"


@pytest.fixture(scope="module")
def profile():
    return load_profile(None)


def test_empty_refs_section_byte_equal(profile):
    text = render_cross_file_section([], profile=profile, evidence=None)
    golden = (GOLDEN_ROOT / "chunk_full.txt").read_text(encoding="utf-8")
    assert text in golden
    assert "Python 端 rg 没找到" in text


def test_non_empty_refs_section_byte_equal(profile):
    text = render_cross_file_section(REFS, profile=profile, evidence=None)
    golden = (GOLDEN_ROOT / "chunk_partial_refs.txt").read_text(encoding="utf-8")
    assert text in golden
    assert "找到 1 条" in text


def test_degraded_evidence_forces_empty_template(profile):
    evidence = DynamicEvidence(
        rule_key=RULE_KEY,
        provider="cross_file_refs",
        refs=(),
        degraded=True,
        reason="timeout",
    )
    degraded = render_cross_file_section(REFS, profile=profile, evidence=evidence)
    empty = render_cross_file_section([], profile=profile)
    assert degraded == empty
    assert "Python 端 rg 没找到" in degraded


def test_declared_templates_and_params_match_legacy_profile(profile):
    ruleset = load_dynamic_rules(profile)
    rule = ruleset.rules[0]
    assert rule.empty_template == "prompts/dynamic/cross_file_refs_empty.md"
    assert rule.items_template == "prompts/dynamic/cross_file_refs_refs.md"
    assert rule.template is None
    assert rule.params == {
        "rg_file_type": "py",
        "max_symbols_per_file": 15,
        "max_refs_per_file": 6,
        "hits_per_symbol": 2,
        "timeout_seconds": 35,
        "excludes": [
            "!reviewagent/**",
            "!tests/**",
            "!.venv/**",
            "!**/__pycache__/**",
            "!.git/**",
        ],
    }
    assert rule.limits == {"max_files": 60}


# ---------------------------------------------------------------------------
# refs_by_file：分桶 + 剥离 review_file
# ---------------------------------------------------------------------------


def test_refs_by_file_buckets_and_strips_review_file():
    evidence = {
        RULE_KEY: DynamicEvidence(
            rule_key=RULE_KEY,
            provider="cross_file_refs",
            refs=(
                {"file": "caller_a.py", "line": 3, "content": "a()", "ident": "a", "review_file": "a.py"},
                {"file": "caller_b.py", "line": 9, "content": "b()", "ident": "b", "review_file": "b.py"},
                {"file": "caller_c.py", "line": 4, "content": "c()", "ident": "c", "review_file": "a.py"},
            ),
        )
    }
    out = refs_by_file(evidence, ["a.py", "b.py", "c.py"])
    assert list(out) == ["a.py", "b.py", "c.py"]
    assert out["a.py"] == [
        {"file": "caller_a.py", "line": 3, "content": "a()", "ident": "a"},
        {"file": "caller_c.py", "line": 4, "content": "c()", "ident": "c"},
    ]
    assert out["b.py"] == [
        {"file": "caller_b.py", "line": 9, "content": "b()", "ident": "b"},
    ]
    assert out["c.py"] == []
    # 原 evidence 中的 dict 不被修改（后续渲染仍可复用）。
    assert "review_file" in evidence[RULE_KEY].refs[0]


def test_refs_by_file_skips_degraded_and_unknown_files():
    evidence = {
        "ok": DynamicEvidence(
            rule_key="ok", provider="cross_file_refs",
            refs=({"file": "x.py", "line": 1, "content": "x", "ident": "x", "review_file": "a.py"},),
        ),
        "bad": DynamicEvidence(
            rule_key="bad", provider="cross_file_refs", refs=(), degraded=True, reason="timeout",
        ),
        "elsewhere": DynamicEvidence(
            rule_key="elsewhere", provider="cross_file_refs",
            refs=({"file": "y.py", "line": 2, "content": "y", "ident": "y", "review_file": "not-in-files.py"},),
        ),
    }
    out = refs_by_file(evidence, ["a.py"])
    assert out == {"a.py": [{"file": "x.py", "line": 1, "content": "x", "ident": "x"}]}


# ---------------------------------------------------------------------------
# 门面：规则禁用（cross_file_enabled=False）→ 整段不存在
# ---------------------------------------------------------------------------


@pytest.fixture()
def active_profile(monkeypatch):
    """固定激活 profile / 规则集 / 受控 Config（与 prompt golden 同源）."""
    from reviewagent.commands import improve as improve_module
    from reviewagent.config import current
    from reviewagent.scenario import profile as profile_module
    from reviewagent.scenario.rules import resolver

    prof = load_profile("ssd-python-autotest")
    monkeypatch.setattr(profile_module, "_active", prof)
    monkeypatch.setattr(resolver, "_ACTIVE", resolver.assemble_base(prof))
    monkeypatch.setattr(
        improve_module, "config",
        dataclasses.replace(
            current(),
            improve_clip_diff_lines=800,
            improve_partial_context_lines=20,
            improve_patch_context_lines=3,
            improve_full_files=20,
        ),
    )
    return prof


def _cmd(tmp_path: Path):
    from reviewagent.commands.improve import ImproveCommand

    (tmp_path / "core.py").write_text(SOURCE, encoding="utf-8")
    (tmp_path / ".diff").write_text(DIFF, encoding="utf-8")
    cmd = ImproveCommand.__new__(ImproveCommand)
    cmd.project_id = 1
    cmd.mr_iid = 1
    cmd.ws = SimpleNamespace(worktree=tmp_path, diff_file=tmp_path / ".diff")
    cmd.repo_context = ""
    cmd._last_oc_result = None
    return cmd


def test_disabled_rule_chunk_prompt_has_no_cross_file_section(active_profile, tmp_path):
    cmd = _cmd(tmp_path)
    enabled = cmd._build_chunk_prompt(
        "core.py", DIFF, VALID, cmd.ws, cross_file_refs=[], strategy="patch",
    )
    disabled = cmd._build_chunk_prompt(
        "core.py", DIFF, VALID, cmd.ws, cross_file_refs=[], strategy="patch",
        cross_file_enabled=False,
    )
    assert "跨文件影响分析" in enabled
    assert "Python 端 rg 没找到" in enabled
    assert "跨文件影响分析" not in disabled
    assert "Python 端 rg 没找到" not in disabled


def test_disabled_rule_batch_prompt_has_no_cross_file_section(active_profile, tmp_path):
    cmd = _cmd(tmp_path)
    disabled = cmd._build_batch_prompt(
        ["core.py"], {"core.py": DIFF}, {"core.py": VALID}, cmd.ws,
        {"core.py": REFS}, {"core.py": "patch"}, {},
        cross_file_enabled=False,
    )
    assert "跨文件影响分析" not in disabled
    assert "Python 端 rg 没找到" not in disabled
    assert "找到 1 条" not in disabled


def test_enabled_rule_batch_prompt_still_renders_section(active_profile, tmp_path):
    cmd = _cmd(tmp_path)
    prompt = cmd._build_batch_prompt(
        ["core.py"], {"core.py": DIFF}, {"core.py": VALID}, cmd.ws,
        {"core.py": REFS}, {"core.py": "patch"}, {},
    )
    assert "找到 1 条" in prompt
