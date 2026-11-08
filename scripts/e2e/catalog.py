"""配置生效性 witness 目录 —— schema 每个字段必须登记一种生效证据.

`tests/test_config_catalog.py` 断言本目录与 `schemas.SCHEMA` 完全对齐：
新增字段不登记即测试失败；登记了不存在的行为 witness 同样失败。

kind 语义（由弱到强）：
    startup  —— 仅配置平面：类型/取值校验 + 进入 Config；无独立行为断言
    behavior —— 快速档进程内行为 delta（check 指定 `BEHAVIOR_WITNESSES` 的 id）
    live     —— 全量档真实链路证明（GitLab / LLM / 队列 / 周报 / 日志）
    script   —— 由 shell / systemd 消费（Python 侧不读）
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Kind = Literal["startup", "behavior", "live", "script"]


@dataclass(frozen=True)
class Witness:
    field: str
    kind: Kind
    evidence: str
    check: str = ""


def _behavior(field: str, check: str, evidence: str) -> Witness:
    return Witness(field, "behavior", evidence, check)


def _live(field: str, evidence: str) -> Witness:
    return Witness(field, "live", evidence)


def _script(field: str, evidence: str) -> Witness:
    return Witness(field, "script", evidence)


_WITNESSES: tuple[Witness, ...] = (
    # ---------- GitLab / webhook ----------
    _live("gitlab_url", "gitlab/client.py:24；全量档所有 API 调用依赖"),
    _live("gitlab_pat", "gitlab/client.py:25；全量档鉴权"),
    _live("gitlab_webhook_secret", "webhook/auth.py:18；全量档 webhook 签名校验"),
    _live("gitlab_bot_username", "webhook/locks.py:267；机器人回环识别"),
    _live("gitlab_disable_bot_loop_check", "webhook/locks.py:261；机器人回环开关"),
    _live("mr_cooldown_seconds", "webhook/locks.py:176；冷却期拦截"),
    _live("max_review_calls_per_mr", "webhook/router.py:203；调用次数上限"),
    _live("pr_commands", "webhook/router.py:162；MR 打开时的命令链"),
    _live("push_commands", "webhook/router.py:151；push 时的命令链"),
    # ---------- LLM ----------
    _live("opencode_url", "opencode/client.py:66（LLM_PROVIDER=opencode 时）"),
    _live("opencode_username", "opencode/client.py:72（LLM_PROVIDER=opencode 时）"),
    _live("opencode_password", "opencode/client.py:73（LLM_PROVIDER=opencode 时）"),
    _live("opencode_model", "opencode/client.py:69（LLM_PROVIDER=opencode 时）"),
    _live("opencode_timeout", "opencode/client.py:67（LLM_PROVIDER=opencode 时）"),
    _live("llm_provider", "llm/client.py:31；provider 选择决定真实调用"),
    _live("qodercli_node_path", "llm/qodercli_subprocess.py:72"),
    _live("qodercli_js_path", "llm/qodercli_subprocess.py:59"),
    _live("qodercli_model", "llm/qodercli_subprocess.py:74"),
    _live("qodercli_timeout", "llm/qodercli_subprocess.py:391"),
    _live("qodercli_permission_mode", "llm/qodercli_subprocess.py:386"),
    _live("qodercli_max_turns", "llm/qodercli_subprocess.py:387"),
    _live("qodercli_fallback_model", "llm/qodercli_provider.py:97"),
    _live("qoder_sdk_pat", "llm/qoder_sdk_provider.py:553"),
    _live("qoder_sdk_edition", "llm/qoder_sdk_provider.py:565"),
    _live("qoder_sdk_cli_path", "llm/qoder_sdk_provider.py:560"),
    _live("qoder_sdk_model", "llm/qoder_sdk_provider.py:571；全量档 telemetry 记录 model"),
    _live("qoder_sdk_fallback_model", "llm/qoder_sdk_provider.py:576"),
    _live("qoder_sdk_timeout", "llm/qoder_sdk_provider.py:621"),
    _live("qoder_sdk_max_turns", "llm/qoder_sdk_provider.py:578"),
    _live("qoder_sdk_permission_mode", "llm/qoder_sdk_provider.py:579"),
    _live("qoder_sdk_disallowed_tools", "llm/qoder_sdk_provider.py:580"),
    # ---------- 基础设施 ----------
    _live("redis_url", "workers/tasks.py:39；队列/锁/计数全部依赖"),
    _live("rq_queue_name", "workers/tasks.py:44；主队列名"),
    _live("rq_weekly_queue_name", "workers/tasks.py:53；周报队列名"),
    _live("rq_worker_timeout", "workers/tasks.py:95；job 超时"),
    _script("rq_worker_count", "scripts/restart_local.sh:25 / services_ops.sh:30 直接读 env"),
    _live("data_dir", "logging_setup.py:427 / telemetry/store.py:169 / git/workspace.py:64"),
    _live("log_level", "logging_setup.py:419"),
    _live("log_format", "logging_setup.py:404"),
    _live("log_rotation", "logging_setup.py:431"),
    _live("review_profile", "cli.py:66 / scenario/profile.py:335；场景包选择"),
    _live("review_profile_root", "cli.py:67 / scenario/profile.py:99；场景包根目录"),
    _behavior("review_config_dir", "review_config_dir",
              "scenario/profile.py:102；用户覆盖层目录（test_scenario_wiring）"),
    # ---------- 周报 ----------
    _live("weekly_enabled", "reporting/runner.py:115"),
    _live("weekly_target_project_id", "reporting/runner.py:180"),
    _live("weekly_target_branch", "reporting/runner.py:183"),
    _behavior("weekly_timezone", "weekly_timezone",
              "reporting/runner.py:_resolve_tz；周边界随时区移动（test_scenario_wiring）"),
    _live("weekly_collectors", "reporting/runner.py:187"),
    _live("weekly_notifier", "reporting/runner.py:84"),
    _live("weekly_dingtalk_webhook_url", "reporting/runner.py:86"),
    _live("weekly_dingtalk_secret", "reporting/runner.py:87"),
    _live("weekly_dingtalk_dry_run", "reporting/runner.py:222；全量档 dry-run 断言"),
    _live("weekly_dingtalk_retry", "reporting/runner.py:88"),
    _live("weekly_md_chunk_limit", "reporting/runner.py:225"),
    _live("weekly_report_title", "reporting/runner.py:205,227；全量档标题断言"),
    _live("weekly_report_emoji", "reporting/runner.py:206,227"),
    _script("weekly_cron_schedule",
            "scripts/services_ops.sh:render_weekly_timer → systemd drop-in"),
    _live("weekly_dashboard_url", "reporting/runner.py:207 → renderer.py:358"),
    # ---------- 场景：diff / 仓库上下文 ----------
    _behavior("max_diff_chars", "diff_overflow",
              "commands/_common.py:partition_overflow；超出改走 overflow→patch"),
    _behavior("opencode_max_diff_chars", "opencode_clip",
              "opencode/client.py:_clip_text（opencode provider）"),
    _live("repo_context_files",
          "scenario/rules/repo.py:_fetch_all_rule_files；全量档真实仓库拉取 AGENTS.md"),
    _live("repo_context_rules_dir",
          "scenario/rules/repo.py:_fetch_all_rule_files；全量档真实仓库 rules_dir 拉取"),
    _behavior("rule_key_prefix", "rule_key_prefix",
              "scenario/rules/keys.py + review/validation.py:396；规则键命名空间"),
    _behavior("repo_context_max_lines", "repo_context_max_lines",
              "scenario/rules/repo.py:render_instruction_files 行数预算"),
    _behavior("improve_parallel_workers", "worker_count",
              "review/runner.py:worker_count；并发 chunk 数"),
    # ---------- 场景：improve 规划 ----------
    _behavior("improve_full_files", "file_strategy_full_quota",
              "review/planner.py:assign_strategy full 配额；全量档 real_config full_files"),
    _behavior("improve_full_source_max_bytes", "full_source_budget",
              "review/planner.py:full_source_budget_bytes 三态"),
    _behavior("improve_keyword_paths", "priority_weights",
              "review/planner.py:priority_score 关键路径加分；全量档 real_config priority_paths"),
    _behavior("improve_skip_test_paths", "plan_filters",
              "review/planner.py:522；测试路径过滤"),
    _behavior("improve_partial_context_lines", "source_context_lines",
              "review/context.py:155；partial 上下文行数"),
    _behavior("improve_patch_context_lines", "source_context_lines",
              "review/context.py:174；patch 上下文行数"),
    _behavior("improve_max_suggestions", "suggestion_max",
              "review/validation.py:479；全局限流"),
    _behavior("improve_max_suggestions_per_file", "suggestion_per_file_max",
              "review/validation.py:440；单文件预算"),
    _behavior("improve_min_suggestions_per_file", "suggestion_per_file_min",
              "review/validation.py:441；单文件下限"),
    _behavior("improve_min_score", "score_filter", "review/validation.py:411"),
    _behavior("improve_min_severity", "severity_filter", "review/validation.py:388"),
    _behavior("improve_review_mode", "plan_filters",
              "review/planner.py:521；全量档 real_config review_mode"),
    _behavior("improve_priority_weight_diff", "priority_weights", "review/planner.py:359"),
    _behavior("improve_priority_weight_keyword", "priority_weights", "review/planner.py:363"),
    _behavior("improve_priority_weight_density", "priority_weights", "review/planner.py:366"),
    _behavior("improve_priority_weight_test_feature", "priority_weights",
              "review/planner.py:368"),
    _behavior("improve_test_feature_keywords", "test_feature_keywords",
              "review/policies/python.py:817"),
    _behavior("review_exclude_extensions", "plan_filters", "review/planner.py:503"),
    _behavior("improve_max_files_to_review", "file_count_cap", "review/planner.py:677"),
    _behavior("improve_new_file_full", "priority_weights", "review/planner.py:371"),
    _behavior("improve_test_sample_paths", "test_sampling",
              "review/planner.py:sample_case_files；全量档 real_config test_sampling"),
    _behavior("improve_test_sample_max", "test_sampling",
              "review/planner.py:sample_case_files；全量档 real_config test_sampling"),
    _behavior("improve_batch_enabled", "batch_grouping", "review/planner.py:691"),
    _behavior("improve_batch_max_diff_lines", "batch_grouping", "review/planner.py:694"),
    _behavior("improve_batch_max_files", "batch_grouping", "review/planner.py:695"),
    _behavior("improve_batch_max_total_lines", "batch_grouping", "review/planner.py:696"),
    _behavior("improve_clip_diff_lines", "full_source_budget", "review/planner.py:203"),
)

CATALOG: dict[str, Witness] = {w.field: w for w in _WITNESSES}

BEHAVIOR_CHECKS: frozenset[str] = frozenset(
    w.check for w in _WITNESSES if w.kind == "behavior"
)

# profile 段里没有消费点的键（不进 schema，因此不属于 CATALOG；单独登记以免再次漂移）。
PROFILE_RESERVED: dict[str, str] = {
    "capabilities": "载入 Profile 但全仓无消费点；保留给未实现的能力协商",
}

# `Profile` 数据段的生效证据（不进 schema；`capabilities` 见 PROFILE_RESERVED）。
# key = Profile 字段，value = 覆盖该段的测试文件/用例。
PROFILE_SECTIONS: dict[str, str] = {
    "prompts": "tests/test_scenario_prompts.py",
    "rules": "tests/test_rules_resolver.py",
    "dynamic_rules": "tests/test_dynamic_rules.py",
    "rule_keys": "tests/test_rules_keys.py",
    "rule_key_promotions": "tests/test_rules_resolver.py::test_normalize_keys_promotion_when_any",
    "rule_overrides": "tests/test_rules_resolver.py::test_override_applies_only_severity_and_weight",
    "disable": "tests/test_rules_resolver.py::test_disable_keeps_verbatim_body_but_drops_index",
    "test_paths": "tests/test_policy_python.py",
    "security": "tests/security/test_untrusted_content.py",
    "reporting": "tests/test_weekly_config_schema.py",
}

# Profile 的簿记字段：不是配置段，由加载器自己维护。
PROFILE_BOOKKEEPING: frozenset[str] = frozenset(
    {"name", "language", "root", "resolved", "sources", "fingerprint"}
)
