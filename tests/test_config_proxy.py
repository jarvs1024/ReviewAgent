from dataclasses import asdict, fields, replace

import pytest

from reviewagent.config import apply_scenario, config, current


def test_proxy_sees_scenario_values_through_imported_reference():
    from reviewagent.commands.improve import config as improve_config

    apply_scenario({"improve_max_suggestions": 7})
    try:
        assert improve_config.improve_max_suggestions == 7
        assert config.improve_max_suggestions == 7
    finally:
        apply_scenario({"improve_max_suggestions": 15})


def test_apply_scenario_rejects_unknown_and_secret():
    with pytest.raises(KeyError):
        apply_scenario({"not_a_field": 1})
    with pytest.raises(PermissionError):
        apply_scenario({"gitlab_pat": "leak"})


# ---------- _ConfigProxy 与 dataclasses 兼容层 ----------


def test_replace_chain_keeps_both_overrides():
    """链式 replace 必须保留两层覆盖（旧空 fields 实现会丢掉内层改动）."""
    chained = replace(replace(config, improve_max_suggestions=3), improve_patch_context_lines=9)
    assert chained.improve_max_suggestions == 3
    assert chained.improve_patch_context_lines == 9


def test_replace_without_changes_keeps_real_values():
    snapshot = replace(config)
    assert snapshot.improve_max_suggestions == config.improve_max_suggestions
    assert snapshot.data_dir == config.data_dir


def test_asdict_and_fields_return_real_values():
    data = asdict(config)
    assert data, "asdict(config) 不应为空"
    assert set(data) == {f.name for f in fields(config)}
    assert data["improve_max_suggestions"] == config.improve_max_suggestions
    assert data["data_dir"] == config.data_dir


# ---------- 场景平面空值语义：None=继承，""/[]=显式空 ----------


def test_apply_scenario_none_inherits_and_empty_string_is_explicit():
    original = current().improve_min_severity
    try:
        apply_scenario({"improve_min_severity": "high"})
        assert current().improve_min_severity == "high"

        # "" 是显式空字符串，不得回退默认值（默认也是 ""，先设成 high 再置空才有区分度）
        apply_scenario({"improve_min_severity": ""})
        assert current().improve_min_severity == ""

        # None = 继承：保留当前值（""），不会被重置成 schema 默认值
        apply_scenario({"improve_min_severity": None})
        assert current().improve_min_severity == ""
    finally:
        apply_scenario({"improve_min_severity": original})


def test_apply_scenario_preserves_explicit_empty_tuple():
    original = current().improve_test_sample_paths
    try:
        apply_scenario({"improve_test_sample_paths": ["a.py"]})
        assert current().improve_test_sample_paths == ("a.py",)

        apply_scenario({"improve_test_sample_paths": []})
        assert current().improve_test_sample_paths == ()

        # None 继承当前空 tuple，不回退默认值（默认同样是 ()，用显式值先区分）
        apply_scenario({"improve_test_sample_paths": ["b.py", "c.py"]})
        apply_scenario({"improve_test_sample_paths": None})
        assert current().improve_test_sample_paths == ("b.py", "c.py")
    finally:
        apply_scenario({"improve_test_sample_paths": list(original)})


def test_apply_scenario_rejects_instance_scope_with_field_name():
    before = current().rq_queue_name
    for name, value in (
        ("data_dir", "/tmp/other-data"),
        ("redis_url", "redis://localhost:6379/1"),
        ("rq_queue_name", "other"),
        ("review_profile", "cpp"),
        ("gitlab_url", "http://other"),
    ):
        with pytest.raises(PermissionError, match=name):
            apply_scenario({name: value})
    # 拒绝后当前配置保持不变
    assert current().rq_queue_name == before


# ---------- 场景平面类型校验 ----------


def test_apply_scenario_bool_accepts_zero_and_one():
    original = current().improve_batch_enabled
    try:
        apply_scenario({"improve_batch_enabled": 0})
        assert current().improve_batch_enabled is False
        apply_scenario({"improve_batch_enabled": 1})
        assert current().improve_batch_enabled is True
    finally:
        apply_scenario({"improve_batch_enabled": original})


def test_apply_scenario_rejects_float_for_int():
    original = current().improve_max_suggestions
    with pytest.raises(ValueError, match="improve_max_suggestions"):
        apply_scenario({"improve_max_suggestions": 1.5})
    assert current().improve_max_suggestions == original


# ---------- 保留的测试旋钮：shadow 覆盖层优先于 scenario ----------


def test_object_setattr_shadow_overrides_scenario_view():
    """`object.__setattr__` 是 test_improve_strategy 依赖的测试旋钮.

    语义：shadow 写在代理实例 __dict__ 上，优先于 scenario 解析出的 `_current`；
    scenario 仍然作用于真实 Config（`current()` 可见）。
    """
    original = current().improve_min_score
    object.__setattr__(config, "improve_min_score", 9)
    try:
        apply_scenario({"improve_min_score": 3})
        assert config.improve_min_score == 9, "shadow 覆盖层应优先于 scenario"
        assert current().improve_min_score == 3
    finally:
        apply_scenario({"improve_min_score": original})
        del config.__dict__["improve_min_score"]
        assert config.improve_min_score == original
