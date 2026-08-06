"""Tests for extract_action reason parsing (compatibility/usability).

Covers the user's 2026-08-06 complaint: /dismiss log and /dismiss测试 should
both work AND record reason correctly. Also covers:
  - /dismiss (no content) → no reason
  - Multi-space normalization
  - Wrapper chars stripping
  - Word boundary excludes /dismissed /dismissal /dismiss_xxx
"""
from reviewagent.commands.suggestion_actions import extract_action


def _check(body: str, expected: tuple[str, str] | None) -> bool:
    result = extract_action(body)
    if expected is None:
        return result is None
    if result is None:
        return False
    return result == expected


# ----- user's specific cases -----
def test_dismiss_log():
    """User case: /dismiss log → reason=log."""
    assert _check("/dismiss log", ("dismiss", "log"))


def test_dismiss_chinese_no_space():
    """User case: /dismiss测试 (no space, Chinese) → reason=测试."""
    assert _check("/dismiss测试", ("dismiss", "测试"))


def test_dismiss_no_content():
    """User case: /dismiss (just command) → reason=''."""
    assert _check("/dismiss", ("dismiss", ""))


# ----- additional compatibility cases -----
def test_dismiss_chinese_with_space():
    assert _check("/dismiss 中文原因", ("dismiss", "中文原因"))


def test_dismiss_full_sentence():
    assert _check("/dismiss 关闭这条理由", ("dismiss", "关闭这条理由"))


def test_dismiss_case_insensitive():
    assert _check("/Dismiss LOG", ("dismiss", "LOG"))


def test_dismiss_multi_space_normalized():
    """Multi-space between words → single space in reason."""
    assert _check("/dismiss   log   reason", ("dismiss", "log reason"))


def test_dismiss_tab_separator():
    assert _check("/dismiss\twith tab", ("dismiss", "with tab"))


def test_dismiss_newline_separator():
    assert _check("/dismiss\nmulti line", ("dismiss", "multi line"))


def test_dismiss_wrapper_stripped():
    """Trailing punctuation/dashes stripped."""
    assert _check("/dismiss -", ("dismiss", ""))
    assert _check("/dismiss —", ("dismiss", ""))
    assert _check("/dismiss (parens)", ("dismiss", "parens"))
    assert _check("/dismiss:  reason", ("dismiss", "reason"))


def test_dismiss_middle_of_text():
    """User case: /dismiss in middle of longer text, `/` artifact removed."""
    assert _check("some random /dismiss text here", ("dismiss", "some random text here"))
    assert _check("中文 /dismiss 测试", ("dismiss", "中文 测试"))


def test_adopt_mirrors_dismiss():
    """`/adopt` parses the same way."""
    assert _check("/adopt log", ("adopt", "log"))
    assert _check("/adopt测试", ("adopt", "测试"))
    assert _check("/adopt - reason", ("adopt", "reason"))


# ----- negative cases (must NOT match) -----
def test_dismissed_not_matched():
    """`/dismissed` is not the dismiss command (substring rejection)."""
    assert _check("/dismissed", None)
    assert _check("/dismissal", None)
    assert _check("/dismiss_xxx", None)  # underscore is word char
    assert _check("dismissed", None)


def test_empty_body():
    assert _check("", None)


def test_dismiss_adopt_priority():
    """/adopt with 'dismiss' in body still extracts adopt (priority)."""
    # adopt first in regex chain
    assert _check("/adopt 用 dismiss 风格重写", ("adopt", "用 dismiss 风格重写"))
