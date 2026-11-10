"""配置字段的唯一默认值来源。纯数据，不 import 上层模块。

`SCHEMA` 里的每个 `default` 就是现网 `Config.from_env()` 的回退值（逐字对齐，
包括逗号字符串解析后的 tuple 形态，例如 `improve_test_feature_keywords` 里的
`"assert "` 在旧 `_env_tuple` 里会被 strip 成 `"assert"`）。改这里就是改现网默认行为。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

Scope = Literal["instance", "scenario"]


@dataclass(frozen=True)
class FieldSpec:
    name: str
    type: type
    default: Any
    env: str | None = None
    scope: Scope = "scenario"
    choices: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    secret: bool = False
    restart_required: bool = True
    doc: str = ""
    aliases: tuple[str, ...] = ()


def profiles_root() -> str:
    """仓库根 `profiles/` 目录（公开名，供 config 的默认工厂引用）."""
    return str(Path(__file__).resolve().parents[2] / "profiles")


SCHEMA: tuple[FieldSpec, ...] = (
    FieldSpec("gitlab_url", str, "", env="GITLAB_URL", scope="instance", secret=True, doc="GitLab 地址"),
    FieldSpec("gitlab_pat", str, "", env="GITLAB_PERSONAL_ACCESS_TOKEN", scope="instance", secret=True),
    FieldSpec("gitlab_webhook_secret", str, "", env="GITLAB_WEBHOOK_SECRET", scope="instance", secret=True),
    FieldSpec("gitlab_bot_username", str, "review-agent", env="GITLAB_BOT_USERNAME", scope="instance"),
    FieldSpec("gitlab_disable_bot_loop_check", bool, False, env="GITLAB_DISABLE_BOT_LOOP_CHECK", scope="instance"),
    FieldSpec("opencode_url", str, "http://localhost:4096", env="OPENCODE_URL", scope="instance"),
    FieldSpec("opencode_username", str, "opencode", env="OPENCODE_USERNAME", scope="instance"),
    FieldSpec("opencode_password", str, "", env="OPENCODE_PASSWORD", scope="instance", secret=True),
    FieldSpec("opencode_model", str, "deepseek/deepseek-v4-flash", env="OPENCODE_MODEL", scope="instance"),
    FieldSpec("opencode_timeout", int, 900, env="OPENCODE_TIMEOUT", scope="instance", minimum=1),
    # 注: llm_provider 不做 choices 强校验 —— 现网/既有测试存在 LLM_PROVIDER=stub 之类的
    # 桩值（多个测试模块 import 期 os.environ.setdefault），未知 provider 由 llm 适配层
    # get_client() 抛 "unknown LLM_PROVIDER"（tests/test_llm_adapter.py 已覆盖）。
    FieldSpec("llm_provider", str, "qodercli", env="LLM_PROVIDER", scope="instance"),
    FieldSpec("qodercli_node_path", str, "", env="QODERCLI_NODE_PATH", scope="instance"),
    FieldSpec("qodercli_js_path", str, "", env="QODERCLI_JS_PATH", scope="instance"),
    FieldSpec("qodercli_model", str, "DeepSeek-V4-Flash", env="QODERCLI_MODEL", scope="instance"),
    FieldSpec("qodercli_timeout", int, 600, env="QODERCLI_TIMEOUT", scope="instance", minimum=1),
    FieldSpec("qodercli_permission_mode", str, "", env="QODERCLI_PERMISSION_MODE", scope="instance"),
    FieldSpec("qodercli_max_turns", int, 20, env="QODERCLI_MAX_TURNS", scope="instance", minimum=0),
    FieldSpec("qodercli_fallback_model", str, "", env="QODERCLI_FALLBACK_MODEL", scope="instance"),
    FieldSpec("qoder_sdk_pat", str, "", env="QODER_SDK_PAT", scope="instance", secret=True),
    FieldSpec("qoder_sdk_edition", str, "intl", env="QODER_SDK_EDITION", scope="instance", choices=("intl", "cn")),
    FieldSpec("qoder_sdk_cli_path", str, "", env="QODER_SDK_CLI_PATH", scope="instance"),
    FieldSpec("qoder_sdk_model", str, "", env="QODER_SDK_MODEL", scope="instance"),
    FieldSpec("qoder_sdk_fallback_model", str, "", env="QODER_SDK_FALLBACK_MODEL", scope="instance"),
    FieldSpec("qoder_sdk_timeout", int, 600, env="QODER_SDK_TIMEOUT", scope="instance", minimum=1),
    FieldSpec("qoder_sdk_max_turns", int, 0, env="QODER_SDK_MAX_TURNS", scope="instance", minimum=0),
    FieldSpec("qoder_sdk_permission_mode", str, "", env="QODER_SDK_PERMISSION_MODE", scope="instance"),
    FieldSpec("qoder_sdk_disallowed_tools", tuple, ("write", "edit", "bash", "webfetch", "websearch"),
              env="QODER_SDK_DISALLOWED_TOOLS", scope="instance"),
    FieldSpec("redis_url", str, "redis://localhost:6379/0", env="REDIS_URL", scope="instance"),
    FieldSpec("rq_queue_name", str, "review", env="RQ_QUEUE_NAME", scope="instance"),
    FieldSpec("rq_weekly_queue_name", str, "review-weekly", env="RQ_WEEKLY_QUEUE_NAME", scope="instance"),
    FieldSpec("rq_worker_timeout", int, 1200, env="RQ_WORKER_TIMEOUT", scope="instance", minimum=1),
    FieldSpec("rq_worker_count", int, 3, env="RQ_WORKER_COUNT", scope="instance", minimum=1),
    FieldSpec("pr_commands", tuple, ("describe", "improve"), env="PR_COMMANDS", scope="instance"),
    FieldSpec("push_commands", tuple, ("describe", "improve"), env="PUSH_COMMANDS", scope="instance"),
    FieldSpec("data_dir", Path, Path("./data"), env="REVIEWAGENT_DATA_DIR", scope="instance"),
    FieldSpec("log_level", str, "INFO", env="REVIEWAGENT_LOG_LEVEL", scope="instance"),
    FieldSpec("log_format", str, "text", env="LOG_FORMAT", scope="instance", choices=("text", "json")),
    FieldSpec("log_rotation", str, "single", env="LOG_ROTATION", scope="instance", choices=("single", "none")),
    FieldSpec("review_profile", str, "", env="REVIEW_PROFILE", scope="instance"),
    FieldSpec("review_profile_root", str, profiles_root(), env="REVIEW_PROFILE_ROOT", scope="instance"),
    # 空=与 profiles/ 同级（<profile_root>/../config），无需部署时再配一次。
    FieldSpec("review_config_dir", str, "", env="REVIEW_CONFIG_DIR", scope="instance"),
    FieldSpec("mr_cooldown_seconds", int, 60, env="MR_COOLDOWN_SECONDS", scope="instance", minimum=0),
    FieldSpec("max_review_calls_per_mr", int, 30, env="MAX_REVIEW_CALLS_PER_MR", scope="instance", minimum=0),
    FieldSpec("max_diff_chars", int, 50000, env="MAX_DIFF_CHARS", scope="scenario", minimum=1),
    FieldSpec("opencode_max_diff_chars", int, 20000, env="OPENCODE_MAX_DIFF_CHARS", scope="scenario", minimum=1),
    FieldSpec("repo_context_rules_dir", str, ".agents/rules", env="REPO_CONTEXT_RULES_DIR", scope="scenario"),
    FieldSpec("rule_key_prefix", str, "auto", env="RULE_KEY_PREFIX", scope="scenario"),
    FieldSpec("repo_context_max_lines", int, 2000, env="REPO_CONTEXT_MAX_LINES", scope="scenario", minimum=1),
    FieldSpec("improve_parallel_workers", int, 3, env="IMPROVE_PARALLEL_WORKERS", scope="scenario", minimum=1),
    FieldSpec("improve_full_files", int, 20, env="IMPROVE_FULL_FILES", scope="scenario", minimum=0),
    FieldSpec("improve_full_source_max_bytes", int, 0, env="IMPROVE_FULL_SOURCE_MAX_BYTES", scope="scenario"),
    FieldSpec("improve_keyword_paths", tuple, (
        "/services/", "/api/", "/core/", "/main.", "/models/", "/handlers/", "/routers/",
    ), env="IMPROVE_KEYWORD_PATHS", scope="scenario"),
    FieldSpec("improve_skip_test_paths", tuple, (
        "tests/", "/tests/", "test_", "_test.", ".test.", "conftest.py",
    ), env="IMPROVE_SKIP_TEST_PATHS", scope="scenario"),
    FieldSpec("improve_partial_context_lines", int, 20, env="IMPROVE_PARTIAL_CONTEXT_LINES", scope="scenario", minimum=0),
    FieldSpec("improve_patch_context_lines", int, 3, env="IMPROVE_PATCH_CONTEXT_LINES", scope="scenario", minimum=0),
    FieldSpec("improve_max_suggestions", int, 15, env="IMPROVE_MAX_SUGGESTIONS", scope="scenario", minimum=0),
    FieldSpec("improve_max_suggestions_per_file", int, 5, env="IMPROVE_MAX_SUGGESTIONS_PER_FILE", scope="scenario", minimum=0),
    FieldSpec("improve_min_suggestions_per_file", int, 2, env="IMPROVE_MIN_SUGGESTIONS_PER_FILE", scope="scenario", minimum=0),
    FieldSpec("improve_min_score", int, 0, env="IMPROVE_MIN_SCORE", scope="scenario", minimum=0),
    FieldSpec("improve_min_severity", str, "", env="IMPROVE_MIN_SEVERITY", scope="scenario",
              choices=("", "critical", "high", "medium", "low")),
    FieldSpec("improve_review_mode", str, "auto", env="IMPROVE_REVIEW_MODE", scope="scenario",
              choices=("auto", "business", "test")),
    FieldSpec("improve_priority_weight_diff", float, 20.0, env="IMPROVE_PRIORITY_WEIGHT_DIFF", scope="scenario", minimum=0),
    FieldSpec("improve_priority_weight_keyword", float, 25.0, env="IMPROVE_PRIORITY_WEIGHT_KEYWORD", scope="scenario", minimum=0),
    FieldSpec("improve_priority_weight_density", float, 20.0, env="IMPROVE_PRIORITY_WEIGHT_DENSITY", scope="scenario", minimum=0),
    FieldSpec("improve_priority_weight_test_feature", float, 15.0, env="IMPROVE_PRIORITY_WEIGHT_TEST_FEATURE", scope="scenario", minimum=0),
    FieldSpec("improve_test_feature_keywords", tuple, (
        "pytest.fixture", "fixture(", "@fixture", "mock.patch", "MagicMock", "patch(",
        "parametrize", "@pytest.mark", "setup_module", "teardown_module", "setup_class",
        "teardown_class", "setUp", "tearDown", "assert", "yield fixture", "pytest.raises",
        "contextmanager", "tmp_path", "caplog", "monkeypatch",
    ), env="IMPROVE_TEST_FEATURE_KEYWORDS", scope="scenario"),
    FieldSpec("review_exclude_extensions", tuple, (
        ".md", ".doc", ".docx", ".txt", ".rst", ".csv", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
    ), env="REVIEW_EXCLUDE_EXTENSIONS", scope="scenario"),
    FieldSpec("improve_max_files_to_review", int, 60, env="IMPROVE_MAX_FILES_TO_REVIEW", scope="scenario", minimum=1),
    FieldSpec("improve_new_file_full", bool, True, env="IMPROVE_NEW_FILE_FULL", scope="scenario"),
    FieldSpec("improve_test_sample_paths", tuple, (), env="IMPROVE_TEST_SAMPLE_PATHS", scope="scenario"),
    FieldSpec("improve_test_sample_max", int, 0, env="IMPROVE_TEST_SAMPLE_MAX", scope="scenario", minimum=0),
    FieldSpec("improve_batch_enabled", bool, True, env="IMPROVE_BATCH_ENABLED", scope="scenario"),
    FieldSpec("improve_batch_max_diff_lines", int, 60, env="IMPROVE_BATCH_MAX_DIFF_LINES", scope="scenario", minimum=1),
    FieldSpec("improve_batch_max_files", int, 5, env="IMPROVE_BATCH_MAX_FILES", scope="scenario", minimum=1),
    FieldSpec("improve_batch_max_total_lines", int, 300, env="IMPROVE_BATCH_MAX_TOTAL_LINES", scope="scenario", minimum=1),
    FieldSpec("improve_clip_diff_lines", int, 800, env="IMPROVE_CLIP_DIFF_LINES", scope="scenario", minimum=0),
    # ---- 周报（instance 平面；周报进程独立于检视链，配置不随场景包切换）----
    # 迁移自 `reporting/config.py` 的 16 个 env 读取（spec §5.5.2）；`weekly_report_title`
    # 默认空串，由 `WeeklyReportConfig.from_env` 回退到 `profile.reporting.weekly_title`。
    FieldSpec("weekly_enabled", bool, True, env="REVIEWAGENT_WEEKLY_ENABLED", scope="instance"),
    FieldSpec("weekly_target_project_id", int, 0, env="REVIEWAGENT_WEEKLY_TARGET_PROJECT_ID", scope="instance"),
    FieldSpec("weekly_target_branch", str, "main", env="REVIEWAGENT_WEEKLY_TARGET_BRANCH", scope="instance"),
    FieldSpec("weekly_timezone", str, "Asia/Shanghai", env="REVIEWAGENT_WEEKLY_TIMEZONE", scope="instance"),
    FieldSpec("weekly_collectors", tuple, ("telemetry", "merged_mrs", "repo_scan"),
              env="REVIEWAGENT_WEEKLY_COLLECTORS", scope="instance"),
    FieldSpec("weekly_notifier", str, "dingtalk", env="REVIEWAGENT_WEEKLY_NOTIFIER", scope="instance"),
    FieldSpec("weekly_dingtalk_webhook_url", str, "", env="REVIEWAGENT_WEEKLY_DINGTALK_WEBHOOK_URL",
              scope="instance", aliases=("DINGTALK_WEBHOOK",)),
    FieldSpec("weekly_dingtalk_secret", str, "", env="REVIEWAGENT_WEEKLY_DINGTALK_SECRET",
              scope="instance", secret=True),
    FieldSpec("weekly_dingtalk_dry_run", bool, True, env="REVIEWAGENT_WEEKLY_DINGTALK_DRY_RUN", scope="instance"),
    FieldSpec("weekly_dingtalk_retry", int, 3, env="REVIEWAGENT_WEEKLY_DINGTALK_RETRY", scope="instance", minimum=0),
    FieldSpec("weekly_md_chunk_limit", int, 18000, env="REVIEWAGENT_WEEKLY_MD_CHUNK_LIMIT", scope="instance", minimum=1),
    FieldSpec("weekly_report_title", str, "", env="REVIEWAGENT_WEEKLY_REPORT_TITLE", scope="instance",
              doc="空 = 用 profile.reporting.weekly_title"),
    FieldSpec("weekly_report_emoji", str, "📊", env="REVIEWAGENT_WEEKLY_REPORT_EMOJI", scope="instance"),
    FieldSpec("weekly_cron_schedule", str, "Mon 10:30", env="REVIEWAGENT_WEEKLY_CRON_SCHEDULE", scope="instance"),
    FieldSpec("weekly_dashboard_url", str, "http://127.0.0.1:8080/code-review",
              env="REVIEWAGENT_WEEKLY_DASHBOARD_URL", scope="instance"),
)

SCHEMA_BY_NAME: dict[str, FieldSpec] = {spec.name: spec for spec in SCHEMA}
