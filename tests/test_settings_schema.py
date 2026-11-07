from dataclasses import MISSING, fields
from typing import get_origin

import pytest

from reviewagent.config import Config
from reviewagent.settings.binder import UNSET, from_env, resolve_env, validate
from reviewagent.settings.schema import SCHEMA, SCHEMA_BY_NAME, FieldSpec

REQUIRED = {
    "GITLAB_URL": "http://localhost",
    "GITLAB_PERSONAL_ACCESS_TOKEN": "x",
    "GITLAB_WEBHOOK_SECRET": "x",
}

# 必填三字段：没有类默认值，构造 Config 时必须显式提供（from_env 缺失则抛错）。
REQUIRED_FIELDS = ("gitlab_url", "gitlab_pat", "gitlab_webhook_secret")


def test_schema_covers_every_config_field():
    assert {f.name for f in fields(Config)} == set(SCHEMA_BY_NAME)


def test_schema_names_are_unique():
    assert len(SCHEMA) == len(SCHEMA_BY_NAME)


def test_required_fields_have_no_dataclass_default():
    for name in REQUIRED_FIELDS:
        assert Config.__dataclass_fields__[name].default is MISSING, name


def _type_key(tp) -> str:
    """归一 `tuple[str, ...]`（字符串注解/dataclass 注解）与 schema 的裸 `tuple`."""
    if isinstance(tp, str):
        return "tuple" if tp.startswith("tuple") else tp
    if tp is tuple or get_origin(tp) is tuple:
        return "tuple"
    return getattr(tp, "__name__", str(tp))


def test_no_env_defaults_match_schema_defaults_for_every_field():
    """全量 parity：无 env 时每个非必填字段的类默认值 == SCHEMA default（含类型）."""
    cfg = Config(gitlab_url="", gitlab_pat="", gitlab_webhook_secret="")
    mismatches = []
    for f in fields(Config):
        if f.name in REQUIRED_FIELDS:
            continue
        spec = SCHEMA_BY_NAME[f.name]
        actual = getattr(cfg, f.name)
        if actual != spec.default or _type_key(f.type) != _type_key(spec.type):
            mismatches.append(
                (f.name, actual, spec.default, _type_key(f.type), _type_key(spec.type))
            )
    assert not mismatches, f"default/type drift: {mismatches}"


def test_no_env_defaults_match_production_fallbacks():
    cfg = from_env(REQUIRED)
    assert cfg.improve_review_mode == "auto"
    assert cfg.improve_priority_weight_diff == 20.0
    assert cfg.improve_priority_weight_keyword == 25.0
    assert cfg.improve_priority_weight_density == 20.0
    assert cfg.improve_priority_weight_test_feature == 15.0
    assert cfg.improve_skip_test_paths == (
        "tests/", "/tests/", "test_", "_test.", ".test.", "conftest.py",
    )
    assert cfg.improve_keyword_paths == (
        "/services/", "/api/", "/core/", "/main.", "/models/", "/handlers/", "/routers/",
    )
    for name in ("improve_review_mode", "improve_priority_weight_diff", "improve_skip_test_paths"):
        assert getattr(cfg, name) == SCHEMA_BY_NAME[name].default


def test_env_bool_and_list_semantics():
    cfg = from_env({**REQUIRED, "IMPROVE_BATCH_ENABLED": "off", "PR_COMMANDS": "describe"})
    assert cfg.improve_batch_enabled is False
    assert cfg.pr_commands == ("describe",)
    with pytest.raises(ValueError, match="IMPROVE_BATCH_ENABLED"):
        from_env({**REQUIRED, "IMPROVE_BATCH_ENABLED": "maybe"})


# ---------- binder 恢复的三项现网行为 ----------


def test_missing_required_env_raises_runtime_error_with_legacy_message():
    for env_name in REQUIRED:
        env = {k: v for k, v in REQUIRED.items() if k != env_name}
        with pytest.raises(RuntimeError, match=f"missing required env var: {env_name}"):
            from_env(env)
    # 空白值等价于缺失（旧 _env(required=True) 会 strip 后判空）
    with pytest.raises(RuntimeError, match="missing required env var: GITLAB_URL"):
        from_env({**REQUIRED, "GITLAB_URL": "   "})


def test_from_env_creates_runtime_dirs(tmp_path):
    data_dir = tmp_path / "data"
    cfg = from_env({**REQUIRED, "REVIEWAGENT_DATA_DIR": str(data_dir)})
    assert cfg.data_dir == data_dir
    for path in (cfg.data_dir, cfg.repos_dir, cfg.worktrees_dir, cfg.weekly_reports_dir):
        assert path.is_dir(), path


def test_weekly_queue_name_derives_from_queue_name():
    derived = from_env({**REQUIRED, "RQ_QUEUE_NAME": "myq"})
    assert derived.rq_weekly_queue_name == "myq-weekly"
    explicit = from_env(
        {**REQUIRED, "RQ_QUEUE_NAME": "myq", "RQ_WEEKLY_QUEUE_NAME": "weekly-explicit"}
    )
    assert explicit.rq_weekly_queue_name == "weekly-explicit"


# ---------- validate：类型归一 + 含字段名的错误 ----------


def test_validate_bool_accepts_zero_and_one():
    spec = SCHEMA_BY_NAME["improve_batch_enabled"]
    assert validate(spec, 1) is True
    assert validate(spec, 0) is False
    assert validate(spec, True) is True


def test_validate_int_rejects_float_and_bool():
    spec = SCHEMA_BY_NAME["improve_max_suggestions"]
    with pytest.raises(ValueError, match="improve_max_suggestions"):
        validate(spec, 1.5)
    with pytest.raises(ValueError, match="improve_max_suggestions"):
        validate(spec, True)


def test_validate_float_normalizes_int_and_rejects_str():
    spec = SCHEMA_BY_NAME["improve_priority_weight_diff"]
    assert validate(spec, 5) == 5.0
    assert isinstance(validate(spec, 5), float)
    with pytest.raises(ValueError, match="improve_priority_weight_diff"):
        validate(spec, "5")


def test_validate_str_rejects_non_str_with_field_name():
    spec = SCHEMA_BY_NAME["improve_review_mode"]
    with pytest.raises(ValueError, match="improve_review_mode"):
        validate(spec, 3)


# ---------- alias 覆盖顺序：canonical 优先 ----------


def test_canonical_env_wins_over_alias():
    spec = FieldSpec("demo", str, "", env="DEMO_ENV", aliases=("DEMO_ENV_OLD",))
    assert resolve_env(spec, {"DEMO_ENV": "canonical", "DEMO_ENV_OLD": "alias"}) == (
        "canonical",
        True,
    )
    assert resolve_env(spec, {"DEMO_ENV_OLD": "alias"}) == ("alias", True)
    assert resolve_env(spec, {}) == (UNSET, False)
