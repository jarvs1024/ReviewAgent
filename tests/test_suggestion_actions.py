"""Unit tests for /adopt and /dismiss handlers."""
from __future__ import annotations

import pytest

from reviewagent.commands.suggestion_actions import (
    _target_region_changed,
    extract_action,
)


# ---------- extract_action ----------

def test_extract_dismiss_with_reason():
    action, reason = extract_action("/dismiss 误报")
    assert action == "dismiss"
    assert reason == "误报"


def test_extract_dismiss_no_reason():
    action, reason = extract_action("/dismiss")
    assert action == "dismiss"
    assert reason == ""


def test_extract_dismiss_no_slash():
    action, reason = extract_action("dismiss 忽略")
    assert action == "dismiss"
    assert reason == "忽略"


def test_extract_adopt_with_reason():
    action, reason = extract_action("/adopt 手动改了")
    assert action == "adopt"
    assert reason == "手动改了"


def test_extract_adopt_takes_priority_over_dismiss():
    # "/adopt 用 dismiss 风格重写" — adopt wins
    action, reason = extract_action("/adopt 用 dismiss 风格重写")
    assert action == "adopt"


def test_extract_no_command():
    assert extract_action("just a normal comment") is None
    assert extract_action("") is None


def test_extract_dismisses_inside_word_ignored():
    # "dismissed" 内嵌的 dismiss 不算 (前后是字母数字)
    assert extract_action("the state was dismissed") is None


# ---------- _target_region_changed ----------

def test_target_changed_true():
    """目标行确实被修改."""
    posted = "def f():\n    return open(p).read()\n"
    current = "def f():\n    with open(p) as f:\n        return f.read()\n"
    assert _target_region_changed(posted, current, line=2, line_end=2) is True


def test_target_changed_false_when_unchanged():
    """目标行没动."""
    posted = "def f():\n    return open(p).read()\n"
    current = "def f():\n    return open(p).read()\n"
    assert _target_region_changed(posted, current, line=2, line_end=2) is False


def test_target_changed_false_other_lines_changed():
    """其他行改了, 目标行没动."""
    posted = "def f():\n    return open(p).read()\n\ndef g(): pass\n"
    current = "def f():\n    return open(p).read()\n\ndef g():\n    return 1\n"
    assert _target_region_changed(posted, current, line=2, line_end=2) is False


def test_target_changed_empty_content():
    """空内容返回 False (无法判断)."""
    assert _target_region_changed("", "x", line=1, line_end=1) is False
    assert _target_region_changed("x", "", line=1, line_end=1) is False


def test_target_changed_multiline_region():
    """多行目标区段被替换."""
    posted = "def f():\n    x = 1\n    y = 2\n    return x + y\n"
    current = "def f():\n    x = 10\n    y = 20\n    return x + y\n"
    assert _target_region_changed(posted, current, line=2, line_end=3) is True
