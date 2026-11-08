"""配置 witness 目录门禁：schema / 快速档 check / Profile 段 都必须有登记.

新增配置字段却不登记生效证据 → 本文件测试变红，强制补 witness。
"""
from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from reviewagent.scenario.profile import Profile
from reviewagent.settings import binder
from reviewagent.settings.schema import SCHEMA, FieldSpec
from scripts.e2e import catalog
from scripts.e2e import quick_witnesses

SCHEMA_NAMES = {spec.name for spec in SCHEMA}


def test_catalog_covers_every_schema_field_exactly_once():
    assert set(catalog.CATALOG) == SCHEMA_NAMES
    assert len(catalog.CATALOG) == len(SCHEMA), "同一字段登记了多次"


def test_witness_kinds_are_known():
    for name, item in catalog.CATALOG.items():
        assert item.kind in {"startup", "behavior", "live", "script"}, name
        assert item.evidence.strip(), f"{name} 缺少证据说明"


def test_behavior_witnesses_have_unique_checks_for_their_field():
    for name, item in catalog.CATALOG.items():
        if item.kind != "behavior":
            assert item.check == "", f"{name}: 非 behavior witness 不应带 check id"
            continue
        assert item.check, f"{name}: behavior witness 必须指定 check id"
        assert item.check in catalog.BEHAVIOR_CHECKS


def test_every_behavior_check_has_a_quick_witness():
    assert set(catalog.BEHAVIOR_CHECKS) == set(quick_witnesses.QUICK)


def test_profile_sections_are_covered():
    sections = {f.name for f in dataclasses.fields(Profile)} - catalog.PROFILE_BOOKKEEPING
    assert sections == set(catalog.PROFILE_SECTIONS) | set(catalog.PROFILE_RESERVED)


def _invalid_value(spec: FieldSpec) -> Any:
    """构造一个必然被 validate 拒绝的值."""
    t = spec.type
    if t is bool:
        return "not-a-bool"
    if t is int:
        return "not-an-int"
    if t is float:
        return "not-a-float"
    # str 拒绝非字符串；tuple 拒绝标量；Path 拒绝既非 str 也非 Path
    return 12345


@pytest.mark.parametrize("spec", SCHEMA, ids=lambda spec: spec.name)
def test_invalid_type_fails_fast_with_field_name(spec: FieldSpec) -> None:
    with pytest.raises(ValueError) as exc:
        binder.validate(spec, _invalid_value(spec))
    assert spec.name in str(exc.value), str(exc.value)


def _choice_violation(spec: FieldSpec) -> str | None:
    if not spec.choices:
        return None
    return "__not_a_choice__"


@pytest.mark.parametrize(
    "spec", [spec for spec in SCHEMA if spec.choices], ids=lambda spec: spec.name
)
def test_invalid_choice_fails_fast(spec: FieldSpec) -> None:
    with pytest.raises(ValueError) as exc:
        binder.validate(spec, _choice_violation(spec))
    assert spec.name in str(exc.value)


@pytest.mark.parametrize(
    "spec", [spec for spec in SCHEMA if spec.minimum is not None], ids=lambda spec: spec.name
)
def test_below_minimum_fails_fast(spec: FieldSpec) -> None:
    with pytest.raises(ValueError):
        binder.validate(spec, spec.minimum - 1)


def test_required_fields_are_declared_in_schema():
    for name in binder.REQUIRED_FIELDS:
        spec = next(spec for spec in SCHEMA if spec.name == name)
        assert spec.scope == "instance"
        assert spec.env


def test_every_field_has_env_name():
    missing = [spec.name for spec in SCHEMA if not spec.env]
    assert not missing, f"字段缺少 env 名称: {missing}"
