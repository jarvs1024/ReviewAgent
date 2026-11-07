"""T20: 周报配置并入唯一 schema 后的投影契约（spec §5.5.2）.

`WeeklyReportConfig.from_env(profile=None)` 的签名与全部属性名保持不变，
但取值改由 `reviewagent.config.current()`（binder 的解析结果）投影而来：

* 每个字段都来自 `current()`——把 binder 的结果换成另一份 Config 即生效；
* `REVIEWAGENT_WEEKLY_DINGTALK_WEBHOOK_URL` 与短名别名 `DINGTALK_WEBHOOK`
  都落到 `dingtalk_webhook_url`；
* `collectors` 逗号 / 空格两种写法等价；
* 标题空值回退 `profile.reporting["weekly_title"]`（再缺省 SSD 标题）。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import reviewagent.config as config_module
from reviewagent.reporting.config import WeeklyReportConfig
from reviewagent.settings.binder import from_env

REQUIRED = {
    "GITLAB_URL": "http://localhost",
    "GITLAB_PERSONAL_ACCESS_TOKEN": "x",
    "GITLAB_WEBHOOK_SECRET": "x",
}

EXPECTED_FIELDS = {
    "enabled",
    "target_project_id",
    "target_branch",
    "timezone",
    "collectors",
    "notifier",
    "dingtalk_webhook_url",
    "dingtalk_secret",
    "dingtalk_dry_run",
    "dingtalk_retry_attempts",
    "markdown_chunk_limit",
    "report_title",
    "report_emoji",
    "cron_schedule",
    "dashboard_url",
    "extra",
}


@pytest.fixture
def bind(monkeypatch, tmp_path):
    """把 `current()` 换成 binder 按给定 env 解析出的 Config（模拟启动解析）."""

    def _bind(extra: dict[str, str] | None = None):
        env = {**REQUIRED, "REVIEWAGENT_DATA_DIR": str(tmp_path / "data")}
        env.update(extra or {})
        cfg = from_env(env)
        monkeypatch.setattr(config_module, "_current", cfg)
        return cfg

    return _bind


def test_attribute_names_and_signature_unchanged():
    cfg = WeeklyReportConfig.from_env(SimpleNamespace(reporting={}))
    assert set(cfg.__dataclass_fields__) == EXPECTED_FIELDS


def test_fields_are_projected_from_current_config(bind):
    bind(
        {
            "REVIEWAGENT_WEEKLY_ENABLED": "false",
            "REVIEWAGENT_WEEKLY_TARGET_PROJECT_ID": "34",
            "REVIEWAGENT_WEEKLY_TARGET_BRANCH": "release",
            "REVIEWAGENT_WEEKLY_TIMEZONE": "UTC",
            "REVIEWAGENT_WEEKLY_COLLECTORS": "telemetry,repo_scan",
            "REVIEWAGENT_WEEKLY_NOTIFIER": "dingtalk",
            "REVIEWAGENT_WEEKLY_DINGTALK_SECRET": "stub-secret",
            "REVIEWAGENT_WEEKLY_DINGTALK_DRY_RUN": "false",
            "REVIEWAGENT_WEEKLY_DINGTALK_RETRY": "5",
            "REVIEWAGENT_WEEKLY_MD_CHUNK_LIMIT": "9000",
            "REVIEWAGENT_WEEKLY_REPORT_EMOJI": "🧪",
            "REVIEWAGENT_WEEKLY_CRON_SCHEDULE": "Tue 09:00",
            "REVIEWAGENT_WEEKLY_DASHBOARD_URL": "http://dash.example.test/cr",
        }
    )
    cfg = WeeklyReportConfig.from_env(SimpleNamespace(reporting={}))
    assert cfg.enabled is False
    assert cfg.target_project_id == 34
    assert cfg.target_branch == "release"
    assert cfg.timezone == "UTC"
    assert cfg.collectors == ("telemetry", "repo_scan")
    assert cfg.notifier == "dingtalk"
    assert cfg.dingtalk_secret == "stub-secret"
    assert cfg.dingtalk_dry_run is False
    assert cfg.dingtalk_retry_attempts == 5
    assert cfg.markdown_chunk_limit == 9000
    assert cfg.report_emoji == "🧪"
    assert cfg.cron_schedule == "Tue 09:00"
    assert cfg.dashboard_url == "http://dash.example.test/cr"


def test_schema_defaults_when_env_absent(bind):
    bind()
    cfg = WeeklyReportConfig.from_env(SimpleNamespace(reporting={}))
    assert cfg.enabled is True
    assert cfg.target_project_id == 0
    assert cfg.target_branch == "main"
    assert cfg.timezone == "Asia/Shanghai"
    assert cfg.collectors == ("telemetry", "merged_mrs", "repo_scan")
    assert cfg.dingtalk_webhook_url == ""
    assert cfg.dingtalk_dry_run is True
    assert cfg.dingtalk_retry_attempts == 3
    assert cfg.markdown_chunk_limit == 18000
    assert cfg.cron_schedule == "Mon 10:30"
    assert cfg.dashboard_url == "http://127.0.0.1:8080/code-review"


@pytest.mark.parametrize(
    "env_key",
    ["REVIEWAGENT_WEEKLY_DINGTALK_WEBHOOK_URL", "DINGTALK_WEBHOOK"],
)
def test_webhook_canonical_env_and_legacy_alias(bind, env_key):
    bind({env_key: "https://oapi.example.test/robot/send?access_token=stub"})
    cfg = WeeklyReportConfig.from_env(SimpleNamespace(reporting={}))
    assert cfg.dingtalk_webhook_url == "https://oapi.example.test/robot/send?access_token=stub"


def test_canonical_webhook_env_wins_over_alias(bind):
    bind(
        {
            "DINGTALK_WEBHOOK": "https://legacy.example.test/hook",
            "REVIEWAGENT_WEEKLY_DINGTALK_WEBHOOK_URL": "https://canonical.example.test/hook",
        }
    )
    cfg = WeeklyReportConfig.from_env(SimpleNamespace(reporting={}))
    assert cfg.dingtalk_webhook_url == "https://canonical.example.test/hook"


def test_collectors_comma_and_space_are_equivalent(bind):
    bind({"REVIEWAGENT_WEEKLY_COLLECTORS": "telemetry,merged_mrs"})
    comma = WeeklyReportConfig.from_env(SimpleNamespace(reporting={})).collectors
    bind({"REVIEWAGENT_WEEKLY_COLLECTORS": "telemetry merged_mrs"})
    space = WeeklyReportConfig.from_env(SimpleNamespace(reporting={})).collectors
    assert comma == space == ("telemetry", "merged_mrs")


def test_empty_collectors_env_falls_back_to_legacy_default(bind):
    """`REVIEWAGENT_WEEKLY_COLLECTORS=` 经 binder 变成空 tuple，仍回退 3 段默认。"""
    bind({"REVIEWAGENT_WEEKLY_COLLECTORS": ""})
    cfg = WeeklyReportConfig.from_env(SimpleNamespace(reporting={}))
    assert cfg.collectors == ("telemetry", "merged_mrs", "repo_scan")


@pytest.mark.parametrize(
    "raw",
    ["telemetry,merged_mrs", "telemetry merged_mrs"],
)
def test_collectors_env_variants_yield_same_tuple(bind, raw):
    bind({"REVIEWAGENT_WEEKLY_COLLECTORS": raw})
    assert WeeklyReportConfig.from_env(SimpleNamespace(reporting={})).collectors == (
        "telemetry",
        "merged_mrs",
    )


def test_report_title_falls_back_to_profile_then_default(bind):
    bind()
    profile = SimpleNamespace(reporting={"weekly_title": "Profile 周报"})
    assert WeeklyReportConfig.from_env(profile).report_title == "Profile 周报"
    empty = SimpleNamespace(reporting={})
    assert WeeklyReportConfig.from_env(empty).report_title == "SSD自动化代码检视周报"


def test_report_title_env_overrides_profile(bind):
    bind({"REVIEWAGENT_WEEKLY_REPORT_TITLE": "Env 周报"})
    profile = SimpleNamespace(reporting={"weekly_title": "Profile 周报"})
    assert WeeklyReportConfig.from_env(profile).report_title == "Env 周报"


def test_empty_env_string_falls_back_to_schema_default(bind):
    """空串 env 与旧 `_env_str` 一致：视为未提供。"""
    bind(
        {
            "REVIEWAGENT_WEEKLY_TARGET_BRANCH": "",
            "REVIEWAGENT_WEEKLY_DINGTALK_WEBHOOK_URL": "",
            "REVIEWAGENT_WEEKLY_REPORT_EMOJI": "",
        }
    )
    cfg = WeeklyReportConfig.from_env(SimpleNamespace(reporting={}))
    assert cfg.target_branch == "main"
    assert cfg.dingtalk_webhook_url == ""
    assert cfg.report_emoji == "📊"
