"""不可信内容规范化 / 围栏自适应 / 硬化开关.

覆盖 brief 断言（控制字符剥离、围栏长于内容反引号）之外：

* 干净字符串必须原样返回**同一对象**（fast path，保证 prompt 字节不变）；
* 仅 ``\\n`` / ``\\t`` 保留；``\\r``、DEL(``\\x7f``) 与其他 C0 剥离；
* ``fence_for`` 默认 3 反引号、按内容最长连续反引号 +1 自适应；
* ``normalize_untrusted=false`` 开关：context 源码块 / 跨文件 section 保留控制字符；
* 现网字节兼容：跨文件 section 与仓库指令文件围栏在正常内容下逐字节不变。
"""
from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace

import pytest

from reviewagent.review.untrusted import fence_for, normalize_untrusted
from reviewagent.scenario.profile import load_profile
from reviewagent.security import hardening_prefix, security_flag

GOLDEN_ROOT = Path(__file__).resolve().parents[1] / "golden" / "prompts" / "ssd-python-autotest"


def _profile(security: dict):
    """默认 profile 换上指定 security 段（不落盘、不影响其它用例）."""
    return dataclasses.replace(load_profile(None), security=security)


# ---------- normalize_untrusted ----------
def test_strips_control_chars_but_keeps_newline_tab():
    assert normalize_untrusted("a\x00b\n\tc") == "ab\n\tc"


def test_clean_text_returns_same_object():
    text = "def f():\n\treturn 1\n"
    assert normalize_untrusted(text) is text


def test_strips_carriage_return():
    """CR 同样属日志/prompt 注入面，必须剥离（仅保留 \\n 与 \\t）."""
    assert normalize_untrusted("a\rb") == "ab"


def test_strips_del_and_other_c0():
    assert normalize_untrusted("a\x1fb\x7f c") == "ab c"
    assert normalize_untrusted("\x0b\x0c\x0e\x08") == ""


def test_keeps_newline_tab_and_empty_string():
    text = "\n\t\n"
    assert normalize_untrusted(text) == text
    assert normalize_untrusted("") == ""


# ---------- fence_for ----------
def test_fence_longer_than_content_backticks():
    assert fence_for("````") == "`````"


def test_fence_defaults_to_three_and_adapts():
    assert fence_for("plain text") == "```"
    assert fence_for("a ``` b") == "````"
    assert fence_for("a ``` b", minimum=5) == "`````"
    assert fence_for("`````x", minimum=5) == "``````"
    assert fence_for("") == "```"


# ---------- security_flag / hardening_prefix ----------
def test_security_flag_reads_profile_dict_and_defaults():
    prof = _profile({"normalize_untrusted": False})
    assert security_flag(prof, "normalize_untrusted", default=True) is False
    assert security_flag(prof, "prompt_hardening", default=False) is False
    assert security_flag({"security": {"prompt_hardening": True}}, "prompt_hardening", default=False) is True
    assert security_flag({"worktree_containment": False}, "worktree_containment", default=True) is False
    assert security_flag(None, "prompt_hardening", default=True) is True


def test_hardening_prefix_switch():
    off = _profile({"prompt_hardening": False})
    on = _profile({"prompt_hardening": True})
    assert hardening_prefix(off) == ""
    assert hardening_prefix(None) == ""
    text = hardening_prefix(on)
    assert text.endswith("\n\n")
    assert "数据" in text


# ---------- context: 源码块 / 跨文件 section ----------
def _file_context(lines):
    from reviewagent.review.types import FileContext

    return FileContext(
        file_path="a.py", lines=tuple(lines), valid_lines=frozenset({1}), start_line=1
    )


def _cfg():
    from reviewagent.config import current

    return dataclasses.replace(
        current(),
        improve_partial_context_lines=20,
        improve_patch_context_lines=3,
        improve_full_files=20,
    )


def _build_full(lines, profile):
    from reviewagent.review.context import build_source_block
    from reviewagent.review.policies.python import PythonPolicy

    cfg = _cfg()
    policy = PythonPolicy(profile=profile, cfg=cfg, read_lines=lambda _p: [])
    return build_source_block(
        _file_context(lines), "full", cfg=cfg, policy=policy, profile=profile
    )


def test_source_block_strips_control_chars():
    prof = _profile({"normalize_untrusted": True})
    out = _build_full(["x = 1\x00", "y = 2\x07"], prof)
    assert "\x00" not in out and "\x07" not in out
    assert "x = 1" in out and "y = 2" in out


def test_source_block_keeps_control_chars_when_disabled():
    prof = _profile({"normalize_untrusted": False})
    out = _build_full(["x = 1\x00"], prof)
    assert "x = 1\x00" in out


def test_cross_file_section_byte_compatible_with_golden():
    """普通 refs（无控制字符 / 无 4+ 反引号）输出必须仍是 3 反引号硬编码形态."""
    from reviewagent.review.context import render_cross_file_section

    prof = _profile({"normalize_untrusted": True})
    refs = [{"file": "tests/test_core.py", "line": 12, "ident": "f", "content": "assert f() == 2"}]
    section = render_cross_file_section(refs, profile=prof)
    assert (
        "**`tests/test_core.py:12`** 引用 `f`:\n```\nassert f() == 2\n```\n\n"
    ) in section
    # 与 golden 逐字节一致（该 section 原样嵌在 chunk prompt 里）
    golden = (GOLDEN_ROOT / "chunk_partial_refs.txt").read_text(encoding="utf-8")
    assert section in golden


def test_cross_file_section_adapts_fence_and_strips_control_chars():
    from reviewagent.review.context import render_cross_file_section

    prof = _profile({"normalize_untrusted": True})
    refs = [{"file": "a.py", "line": 1, "ident": "f", "content": "```\nclosing\x00"}]
    out = render_cross_file_section(refs, profile=prof)
    assert "````\n```\nclosing\n````" in out
    assert "\x00" not in out


def test_cross_file_section_keeps_control_chars_when_disabled():
    from reviewagent.review.context import render_cross_file_section

    prof = _profile({"normalize_untrusted": False})
    refs = [{"file": "a.py", "line": 1, "ident": "f", "content": "x = 1\x00"}]
    out = render_cross_file_section(refs, profile=prof)
    assert "x = 1\x00" in out


# ---------- scenario/rules/repo.py: 指令文件 ----------
@pytest.fixture()
def active_profile(monkeypatch):
    from reviewagent.scenario import profile as profile_module

    def _activate(security: dict | None = None):
        prof = load_profile(None) if security is None else _profile(security)
        monkeypatch.setattr(profile_module, "_active", prof)
        return prof

    return _activate


def test_instruction_files_keep_five_backtick_fence(active_profile):
    from reviewagent.scenario.rules.repo import render_instruction_files

    active_profile()
    text = render_instruction_files({"AGENTS.md": "# hi\nrule\n"})
    assert "`````markdown" in text
    assert "\n`````\n</file>" in text


def test_instruction_files_strip_control_chars(active_profile):
    from reviewagent.scenario.rules.repo import render_instruction_files

    active_profile()
    text = render_instruction_files({"AGENTS.md": "# hi\x00\nrule\n"})
    assert "\x00" not in text
    assert "# hi\nrule" in text


def test_instruction_files_widen_fence_for_backticks(active_profile):
    from reviewagent.scenario.rules.repo import render_instruction_files

    active_profile()
    text = render_instruction_files({"AGENTS.md": "x\n`````\ny\n"})
    assert "``````markdown" in text


def test_instruction_files_keep_control_chars_when_disabled(active_profile):
    from reviewagent.scenario.rules.repo import render_instruction_files

    active_profile({"normalize_untrusted": False})
    text = render_instruction_files({"AGENTS.md": "# hi\x00\n"})
    assert "\x00" in text


# ---------- chunk prompt: 硬化段接入 ----------
def test_chunk_prompt_prepends_hardening_when_enabled(monkeypatch, tmp_path):
    from reviewagent.commands import improve as improve_module  # noqa: F401  (import 期 env)
    from reviewagent.commands.improve import ImproveCommand
    from reviewagent.scenario import profile as profile_module
    from reviewagent.scenario.rules import resolver

    prof = _profile(
        {"prompt_hardening": True, "normalize_untrusted": True, "worktree_containment": True}
    )
    monkeypatch.setattr(profile_module, "_active", prof)
    monkeypatch.setattr(resolver, "_ACTIVE", resolver.assemble_base(prof))

    (tmp_path / "core.py").write_text("x = 1\x00\n", encoding="utf-8")
    cmd = object.__new__(ImproveCommand)
    cmd.ws = SimpleNamespace(worktree=tmp_path)
    cmd.repo_context = ""
    diff = "diff --git a/core.py b/core.py\n--- a/core.py\n+++ b/core.py\n@@ -1 +1 @@\n+x = 2\n"

    prompt = cmd._build_chunk_prompt(
        "core.py", diff, {1}, cmd.ws, cross_file_refs=[], strategy="patch"
    )
    prefix = hardening_prefix(prof)
    assert prefix.startswith("## 安全约束")
    assert prompt.startswith(prefix)
    assert "\x00" not in prompt  # 源码行同时按 normalize_untrusted 清理


def test_chunk_prompt_has_no_hardening_prefix_by_default(monkeypatch, tmp_path):
    from reviewagent.commands.improve import ImproveCommand
    from reviewagent.scenario import profile as profile_module
    from reviewagent.scenario.rules import resolver

    prof = _profile({"prompt_hardening": False})
    monkeypatch.setattr(profile_module, "_active", prof)
    monkeypatch.setattr(resolver, "_ACTIVE", resolver.assemble_base(prof))

    (tmp_path / "core.py").write_text("x = 1\n", encoding="utf-8")
    cmd = object.__new__(ImproveCommand)
    cmd.ws = SimpleNamespace(worktree=tmp_path)
    cmd.repo_context = ""
    diff = "diff --git a/core.py b/core.py\n--- a/core.py\n+++ b/core.py\n@@ -1 +1 @@\n+x = 2\n"

    prompt = cmd._build_chunk_prompt(
        "core.py", diff, {1}, cmd.ws, cross_file_refs=[], strategy="patch"
    )
    assert hardening_prefix(prof) == ""
    assert "## 安全约束" not in prompt
