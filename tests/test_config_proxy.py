from reviewagent.config import apply_scenario, config


def test_proxy_sees_scenario_values_through_imported_reference():
    from reviewagent.commands.improve import config as improve_config

    apply_scenario({"improve_max_suggestions": 7})
    try:
        assert improve_config.improve_max_suggestions == 7
        assert config.improve_max_suggestions == 7
    finally:
        apply_scenario({"improve_max_suggestions": 15})


def test_apply_scenario_rejects_unknown_and_secret():
    import pytest

    with pytest.raises(KeyError):
        apply_scenario({"not_a_field": 1})
    with pytest.raises(PermissionError):
        apply_scenario({"gitlab_pat": "leak"})
