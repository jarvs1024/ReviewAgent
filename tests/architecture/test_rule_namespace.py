"""规则命名空间守卫（spec §7.5 第 3 条）.

当前 profile 只能装配匹配自身 ``rule_keys.builtin_pattern`` /
``rule_keys.repo_prefix`` 的规则包；跨场景（如 C++ 的 ``CPP-R-*``）一律拒绝。

负向 fixture ``tests/fixtures/architecture_violations/cpp_pack.md`` 是一个
**格式合法**的 structured 规则包，只是键不在 Python 场景命名空间内——用它证明
拒绝发生在命名空间校验，而不是解析失败。
"""
from __future__ import annotations

import dataclasses
import shutil
from pathlib import Path

import pytest

from reviewagent.scenario.profile import load_profile
from reviewagent.scenario.rules.parser import parse_pack
from reviewagent.scenario.rules.resolver import assemble_base

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "architecture_violations"
CPP_PACK = FIXTURES / "cpp_pack.md"


@pytest.fixture()
def default_profile():
    return load_profile("ssd-python-autotest")


def test_cross_namespace_fixture_is_a_valid_rule_pack():
    """fixture 本身必须能被解析——否则负向测试证明不了命名空间校验."""
    pack = parse_pack(CPP_PACK, language="cpp", root=FIXTURES)
    assert pack.render == "structured"
    assert "CPP-R-001" in pack.index_by_key
    assert pack.index_by_key["CPP-R-001"].severity == "high"


def test_python_profile_rejects_cpp_pack(default_profile, tmp_path):
    shutil.copy(CPP_PACK, tmp_path / "cpp_pack.md")
    profile = dataclasses.replace(
        default_profile,
        root=tmp_path,
        rules=("cpp_pack.md",),
    )
    with pytest.raises(ValueError, match="outside profile namespace"):
        assemble_base(profile)


def test_default_profile_assembles_within_its_namespace(default_profile):
    rule_set = assemble_base(default_profile)
    assert rule_set.rules
    for rule in rule_set.rules:
        assert rule_set.key_patterns.is_builtin(rule.key) or rule_set.key_patterns.is_repo(
            rule.key
        )
        assert not rule.key.startswith("CPP-")
