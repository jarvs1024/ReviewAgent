from dataclasses import fields

import pytest

from reviewagent.config import Config
from reviewagent.settings.binder import from_env
from reviewagent.settings.schema import SCHEMA, SCHEMA_BY_NAME

REQUIRED = {
    "GITLAB_URL": "http://localhost",
    "GITLAB_PERSONAL_ACCESS_TOKEN": "x",
    "GITLAB_WEBHOOK_SECRET": "x",
}


def test_schema_covers_every_config_field():
    assert {f.name for f in fields(Config)} == set(SCHEMA_BY_NAME)


def test_schema_names_are_unique():
    assert len(SCHEMA) == len(SCHEMA_BY_NAME)


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
