"""Task 7: review 类型 / LanguagePolicy 协议 / registry / Python + Generic policy.

覆盖点：

* PythonPolicy 9 个钩子的行为（评分表 / 符号提取 / 关键字 / 分类 / 片段渲染）；
* `ImproveCommand` 门面与策略模块级函数**同源**（委托不能有第二份活逻辑）；
* 规则前缀在调用时解析（monkeypatch `reviewagent.commands.improve.config` 生效）；
* registry 的注册 / 内建回退（python → PythonPolicy，其余 → GenericPolicy）；
* suggestion_actions 的关键字注入（`keywords=None` 保持旧行为）.

说明：brief 的示例断言两处与「原样迁移」冲突（`_extract_identifiers` 只提取
def/class/常量/import 名，不提取参数名；评分表上限 95 而非 100），这里按旧实现
的真实语义断言，并以门面等价性测试锁定迁移前后一致.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from reviewagent.commands.improve import ImproveCommand
from reviewagent.commands.suggestion_actions import (
    _extract_identifiers,
    _strict_token_adoption_match,
)
from reviewagent.config import current
from reviewagent.review.policies import registry
from reviewagent.review.policies.generic import GenericPolicy
from reviewagent.review.policies.python import (
    HELP_TEXT_FOOTER,
    PythonPolicy,
    find_line_by_existing_code,
    fix_indent,
    normalise_suggestion,
    safe_parse_defs,
    score_suggestion,
)
from reviewagent.review.policies.python import (
    test_feature_density as python_test_feature_density,
)
from reviewagent.review.types import FileContext, FileKind
from reviewagent.scenario.profile import load_profile


def _profile():
    return load_profile(None)


def _policy(read_lines=None):
    return PythonPolicy(
        profile=_profile(),
        cfg=current(),
        read_lines=read_lines or (lambda p: []),
    )


def _ctx(lines=("x = 1", "y = 2"), *, start_line: int = 1) -> FileContext:
    return FileContext(
        file_path="a/b.py",
        lines=tuple(lines),
        valid_lines=frozenset({start_line}),
        start_line=start_line,
    )


# ---------------------------------------------------------------------------
# PythonPolicy 钩子
# ---------------------------------------------------------------------------

def test_score_matches_legacy_table():
    """评分表与旧 _score_suggestion 一致（high 30 + security 30 + 长 rationale 20
    + 规则引用 10 + header 5 = 95，表上限即 95）."""
    sug = {
        "severity": "high",
        "label": "security",
        "rationale": "x" * 210 + " SSD-RULE-X",
        "header": "header",
    }
    assert _policy().score_suggestion(sug) == 95
    # 门面同源（同输入同分数）
    assert ImproveCommand._score_suggestion(sug) == 95


def test_extract_symbols_uses_legacy_declaration_extraction():
    """`_extract_identifiers` 只提取 def/class/常量/import 名（不提取参数名）.

    brief 示例期望 ["foo", "bar"]，但原实现从不提取函数参数；按迁移约束
    「行为不变」断言 ["foo"]，并补一个覆盖 class/常量/import 的用例.
    """
    p = _policy()
    assert p.extract_symbols("+def foo(bar):\n+    return bar") == ["foo"]
    assert p.extract_symbols("+from os import path\n+class Widget:\n+MAX_RETRIES = 3") == [
        "MAX_RETRIES",
        "Widget",
        "path",
    ]
    assert p.extract_symbols("") == []


def test_keywords_contains_python_keyword_set():
    assert {"if", "self", "None"} <= _policy().keywords()


def test_classify_file_kinds():
    p = _policy()
    assert p.classify_file("tests/test_a.py") is FileKind.TEST
    assert p.classify_file("tests/conftest.py") is FileKind.FIXTURE
    assert p.classify_file("a/fixture_data.json") is FileKind.FIXTURE
    assert p.classify_file("reviewagent/commands/improve.py") is FileKind.SOURCE


def test_test_feature_density_uses_injected_read_lines_and_keywords():
    lines = ["assert x" if i < 10 else "pass" for i in range(50)]
    cfg = SimpleNamespace(improve_test_feature_keywords=("assert",))
    p = PythonPolicy(profile=_profile(), cfg=cfg, read_lines=lambda path: lines)
    expected = python_test_feature_density(
        "t.py", 50, read_lines=lambda path: lines, keywords=("assert",)
    )
    assert p.test_feature_density("t.py", 50) == expected
    assert p.test_feature_density("t.py", 50) > 0
    assert p.test_feature_density("t.py", 0) == 0.0


def test_render_source_block_is_plain_numbered_fragment():
    """策略只做片段内行号格式化；不截断、不加块标题（context 层负责）."""
    p = _policy()
    ctx = _ctx(("x = 1", "y = 2"), start_line=9)
    assert p.render_source_block(ctx, "full", 10_000) == "   9| x = 1\n  10| y = 2"
    # budget 再小也不截断（截断由 review/context.py 决定）
    assert p.render_source_block(ctx, "partial", 1) == "   9| x = 1\n  10| y = 2"


def test_normalize_suggestion_matches_facade_and_contains_footer():
    raw = {
        "file": "a/b.py",
        "start_line": 3,
        "existing_code": "x = 1",
        "improved_code": "x = 2",
        "header": "常量替换",
        "rationale": "避免魔法数字",
        "label": "code quality",
        "severity": "medium",
    }
    pol = _policy().normalize_suggestion(raw, _ctx())
    facade = ImproveCommand._normalise_suggestion(raw)
    assert pol == facade
    assert pol["body"].endswith(HELP_TEXT_FOOTER)
    assert pol["new_line"] == 3


def test_prompt_sections_empty_for_python():
    assert _policy().prompt_sections() == {}


# ---------------------------------------------------------------------------
# 门面委托等价性（迁移后只剩一份活逻辑）
# ---------------------------------------------------------------------------

FACADE_CASES = [
    "def f(:\n    pass\n",
    "import requests\ndef f(x): return x\n",
    "",
]


def test_facade_safe_parse_defs_delegates():
    import ast

    cmd = ImproveCommand.__new__(ImproveCommand)
    for src in FACADE_CASES:
        facade_defs, facade_err = cmd._safe_parse_defs(src, ast)
        module_defs, module_err = safe_parse_defs(src, ast)
        assert facade_defs == module_defs
        assert (facade_err is None) == (module_err is None)
        if facade_err is not None and module_err is not None:
            assert (facade_err.lineno, facade_err.msg) == (module_err.lineno, module_err.msg)
    defs, err = cmd._safe_parse_defs("import json\nclass C: pass\n", ast)
    assert err is None
    assert defs == {"json", "C"}


def test_facade_find_line_fix_indent_normalise_delegate():
    lines = ["def f():", "    x = 1", "    return x"]
    assert ImproveCommand._find_line_by_existing_code(
        lines, "    x = 1", hint_line=2
    ) == find_line_by_existing_code(lines, "    x = 1", hint_line=2)
    assert ImproveCommand._find_line_by_existing_code(lines, "    x = 1", hint_line=2) == 2
    assert ImproveCommand._fix_indent("    x = 1", "x = 2") == fix_indent("    x = 1", "x = 2")
    assert ImproveCommand._fix_indent("    x = 1", "x = 2") == "    x = 2"
    raw = {"file": "a.py", "start_line": 1, "improved_code": "y = 2"}
    assert ImproveCommand._normalise_suggestion(raw) == normalise_suggestion(raw)
    assert ImproveCommand._normalise_suggestion(raw) == {
        "file": "a.py",
        "new_line": 1,
        "improved_code": "y = 2",
        "header": "建议改进",
        "rationale": "",
        "label": "enhancement",
        "severity": "medium",
        "body": (
            "**[MEDIUM]** **建议改进** — enhancement\n\n\n\n"
            "```suggestion:-0+0\ny = 2\n```" + HELP_TEXT_FOOTER
        ),
    }


def test_facade_extract_identifiers_and_remedied_symbols_delegate():
    from reviewagent.review.policies.python import (
        extract_identifiers,
        extract_remedied_symbols,
    )

    diff = "+def foo():\n+    return 1"
    assert ImproveCommand._extract_identifiers(diff) == extract_identifiers(diff)
    text = "请补 import json 并添加 logging"
    assert ImproveCommand._extract_remedied_symbols(text) == extract_remedied_symbols(text)


def test_facade_detect_apply_risk_delegates():
    from reviewagent.review.policies.python import detect_apply_risk

    cmd = ImproveCommand.__new__(ImproveCommand)
    kwargs = dict(
        file_path="a.py",
        improved_code="return missing_symbol",
        file_sources={"a.py": ["def f():", "    return 1"]},
        suggestion_text="",
    )
    assert cmd._detect_apply_risk(**kwargs) == detect_apply_risk(**kwargs)
    assert cmd._detect_apply_risk(**kwargs)[0] == "warn"


def test_facade_score_suggestion_reads_rule_prefix_at_call_time(monkeypatch):
    """门面在调用时读 config.rule_key_prefix（不再 import-time 绑定 SSD）."""
    sug = {"severity": "low", "rationale": "命中 ACME-RULE-1", "header": "x"}
    monkeypatch.setattr(
        "reviewagent.commands.improve.config",
        SimpleNamespace(rule_key_prefix="ACME"),
    )
    assert ImproveCommand._score_suggestion(sug) == score_suggestion(sug, rule_prefix="ACME")
    # 默认前缀（SSD）不再命中 ACME-RULE-*
    assert score_suggestion(sug, rule_prefix="SSD") < score_suggestion(sug, rule_prefix="ACME")


def test_facade_test_feature_density_reads_keywords_at_call_time(tmp_path, monkeypatch):
    cmd = ImproveCommand.__new__(ImproveCommand)
    cmd.ws = SimpleNamespace(worktree=tmp_path)
    (tmp_path / "sample_test.py").write_text("\n".join(["assert x"] * 10) + "\n")
    monkeypatch.setattr(
        "reviewagent.commands.improve.config",
        SimpleNamespace(improve_test_feature_keywords=("assert",)),
    )
    assert cmd._test_feature_density("sample_test.py", 10) > 0


# ---------------------------------------------------------------------------
# GenericPolicy
# ---------------------------------------------------------------------------

def test_generic_policy_hooks():
    profile = _profile()  # scoring.test_paths = ["tests/", "test_*.py"]
    pol = GenericPolicy(profile=profile, cfg=current(), read_lines=lambda p: [])
    assert pol.classify_file("tests/foo.py") is FileKind.TEST
    assert pol.classify_file("pkg/test_unit.py") is FileKind.TEST
    assert pol.classify_file("pkg/core.py") is FileKind.SOURCE
    assert pol.classify_file("") is FileKind.OTHER
    assert pol.keywords() == set()
    assert pol.test_feature_density("a.py", 100) == 0.0
    assert pol.extract_symbols("+int main() { return call_me(); }") == [
        "call_me",
        "int",
        "main",
        "return",
    ]
    assert pol.render_source_block(_ctx(start_line=4), "full", 100) == "   4| x = 1\n   5| y = 2"
    raw = {"file": "a.c", "start_line": 1, "improved_code": "x"}
    assert pol.normalize_suggestion(raw, _ctx()) is raw
    assert pol.validate_suggestion(
        raw, _ctx(), rule_set=None, file_sources={}, line_map={}
    ) == {"action": "post"}


def test_generic_policy_without_test_paths_is_conservative():
    profile = SimpleNamespace(test_paths=None, scoring={}, rule_keys={})
    pol = GenericPolicy(profile=profile, cfg=current(), read_lines=lambda p: [])
    assert pol.classify_file("tests/test_a.py") is FileKind.SOURCE


def test_generic_policy_score_has_no_rule_bonus():
    sug = {"severity": "high", "label": "security", "rationale": "SSD-RULE-X", "header": "hdr"}
    pol = GenericPolicy(profile=_profile(), cfg=current(), read_lines=lambda p: [])
    assert pol.score_suggestion(sug) == 60  # 30 + 30 + 0 (无前缀加分) + 0 (短 header)


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------

def test_registry_builtin_languages():
    kwargs = dict(profile=_profile(), cfg=current(), read_lines=lambda p: [])
    assert isinstance(registry.get("python", **kwargs), PythonPolicy)
    assert isinstance(registry.get("cpp", **kwargs), GenericPolicy)


def test_registry_custom_factory():
    calls = {}

    def factory(*, profile, cfg, read_lines):
        calls["hit"] = True
        return GenericPolicy(profile=profile, cfg=cfg, read_lines=read_lines)

    try:
        registry.register("task7-test-lang", factory)
        pol = registry.get(
            "task7-test-lang", profile=_profile(), cfg=current(), read_lines=lambda p: []
        )
        assert isinstance(pol, GenericPolicy)
        assert calls["hit"] is True
    finally:
        # 只清理本测试注册的键，避免污染进程级 registry 影响其它测试顺序.
        registry._FACTORIES.pop("task7-test-lang", None)


# ---------------------------------------------------------------------------
# suggestion_actions: 关键字注入
# ---------------------------------------------------------------------------

def test_extract_identifiers_keyword_injection():
    code = "if value return self"
    assert _extract_identifiers(code) == {"value"}  # 默认 Python 关键字集排除 if/return/self
    assert _extract_identifiers(code, set()) == {"if", "return", "self", "value"}
    assert _extract_identifiers(code, {"value"}) == {"if", "return", "self"}


def test_strict_token_match_keyword_injection_changes_token_set():
    content = "item = compute()\n"
    improved = "item = compute()"
    default_hit, default_details = _strict_token_adoption_match(
        content, content, line=1, line_end=1, improved_code=improved, coverage_floor=1
    )
    injected_hit, injected_details = _strict_token_adoption_match(
        content,
        content,
        line=1,
        line_end=1,
        improved_code=improved,
        coverage_floor=1,
        keywords={"item"},
    )
    assert default_hit is True
    assert injected_hit is True
    assert default_details["new_token_total"] == 2
    assert injected_details["new_token_total"] == 1


def test_long_help_text_footer_constant_exported():
    assert "解决主题" in HELP_TEXT_FOOTER
    assert ImproveCommand.HELP_TEXT_FOOTER is HELP_TEXT_FOOTER


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
