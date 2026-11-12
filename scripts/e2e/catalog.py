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
    _live("review_profile", "cli.py:cfg.review_profile；scenario/profile.py:335；场景包选择"),
    _live("review_profile_root", "cli.py:67 / scenario/profile.py:99；场景包根目录"),
    _behavior("review_config_dir", "review_config_dir",
              "scenario/profile.py:102；用户覆盖层目录（test_scenario_wiring）"),
    # ---------- 周报 ----------
    # Why 用**符号锚点**而不是行号: 数字锚点不被 `_evidence_problem` 校验内容,
    # 改一次代码就静默漂移到无关行 (2026-11-11 实测: runner.py 的 11 个行号
    # 锚点全部漂移到无关代码, 目录却仍显示"绿"). 符号锚点会被真校验.
    _live("weekly_enabled", "reporting/runner.py:cfg.enabled"),
    _live("weekly_target_project_id", "reporting/runner.py:target_project_id"),
    _live("weekly_target_branch", "reporting/runner.py:target_branch"),
    _behavior("weekly_timezone", "weekly_timezone",
              "reporting/runner.py:_resolve_tz；周边界随时区移动（test_scenario_wiring）"),
    _live("weekly_collectors", "reporting/runner.py:cfg.collectors"),
    _live("weekly_notifier", "reporting/runner.py:_build_notifier"),
    _live("weekly_dingtalk_webhook_url", "reporting/runner.py:_build_notifier"),
    _live("weekly_dingtalk_secret", "reporting/runner.py:_build_notifier"),
    _live("weekly_dingtalk_dry_run",
          "reporting/runner.py:_should_send_notice；全量档 dry-run 断言见 flows.weekly_notify_gate"),
    _live("weekly_dingtalk_retry", "reporting/runner.py:dingtalk_retry"),
    _live("weekly_md_chunk_limit", "reporting/runner.py:markdown_chunk_limit"),
    _live("weekly_report_title", "reporting/runner.py:report_title；全量档标题断言"),
    _live("weekly_report_emoji", "reporting/runner.py:report_emoji"),
    _script("weekly_cron_schedule",
            "scripts/services_ops.sh:render_weekly_timer → systemd drop-in"),
    _live("weekly_dashboard_url", "reporting/runner.py:dashboard_url"),
    # ---------- 场景：diff / 仓库上下文 ----------
    _behavior("max_diff_chars", "diff_overflow",
              "commands/_common.py:partition_overflow；超出改走 overflow→patch"),
    _behavior("opencode_max_diff_chars", "opencode_clip",
              "opencode/client.py:_clip_text（opencode provider）"),
    _live("repo_context_rules_dir",
          "scenario/rules/repo.py:_fetch_all_rule_files；默认 .agents/rules 递归扫描，全量档校验嵌套规则文件"),
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
              "review/planner.py:cfg.improve_skip_test_paths；测试路径过滤"),
    _behavior("improve_partial_context_lines", "source_context_lines",
              "review/context.py:155；partial 上下文行数"),
    _behavior("improve_patch_context_lines", "source_context_lines",
              "review/context.py:174；patch 上下文行数"),
    _behavior("improve_max_suggestions", "suggestion_max",
              "review/validation.py:merge_chunks；全局限流"),
    _behavior("improve_max_suggestions_per_file", "suggestion_per_file_max",
              "review/validation.py:merge_chunks；单文件预算"),
    _behavior("improve_min_suggestions_per_file", "suggestion_per_file_min",
              "review/validation.py:merge_chunks；单文件下限"),
    _behavior("improve_min_score", "score_filter", "review/validation.py:merge_chunks"),
    _behavior("improve_min_severity", "severity_filter", "review/validation.py:merge_chunks"),
    _behavior("improve_review_mode", "plan_filters",
              "review/planner.py:cfg.improve_review_mode；全量档 real_config review_mode"),
    _behavior("improve_priority_weight_diff", "priority_weights", "review/planner.py:cfg.improve_priority_weight_diff"),
    _behavior("improve_priority_weight_keyword", "priority_weights", "review/planner.py:cfg.improve_priority_weight_keyword"),
    _behavior("improve_priority_weight_density", "priority_weights", "review/planner.py:cfg.improve_priority_weight_density"),
    _behavior("improve_priority_weight_test_feature", "priority_weights",
              "review/planner.py:cfg.improve_priority_weight_test_feature"),
    _behavior("improve_test_feature_keywords", "test_feature_keywords",
              "review/policies/python.py:817"),
    _behavior("review_exclude_extensions", "plan_filters", "review/planner.py:cfg.review_exclude_extensions"),
    _behavior("improve_max_files_to_review", "file_count_cap", "review/planner.py:cfg.improve_max_files_to_review"),
    _behavior("improve_new_file_full", "priority_weights", "review/planner.py:cfg.improve_new_file_full"),
    _behavior("improve_test_sample_paths", "test_sampling",
              "review/planner.py:sample_case_files；全量档 real_config test_sampling"),
    _behavior("improve_test_sample_max", "test_sampling",
              "review/planner.py:sample_case_files；全量档 real_config test_sampling"),
    _behavior("improve_batch_enabled", "batch_grouping", "review/planner.py:cfg.improve_batch_enabled"),
    _behavior("improve_batch_max_diff_lines", "batch_grouping", "review/planner.py:cfg.improve_batch_max_diff_lines"),
    _behavior("improve_batch_max_files", "batch_grouping", "review/planner.py:cfg.improve_batch_max_files"),
    _behavior("improve_batch_max_total_lines", "batch_grouping", "review/planner.py:cfg.improve_batch_max_total_lines"),
    _behavior("improve_clip_diff_lines", "full_source_budget", "review/planner.py:cfg.improve_clip_diff_lines"),
)

CATALOG: dict[str, Witness] = {w.field: w for w in _WITNESSES}

BEHAVIOR_CHECKS: frozenset[str] = frozenset(
    w.check for w in _WITNESSES if w.kind == "behavior"
)


# ---------------------------------------------------------------------------
# live 字段的断言绑定（防漏测护栏）
# ---------------------------------------------------------------------------
#
# Why 需要这一层: behavior 类有 `BEHAVIOR_CHECKS` 双向绑定 —— 登记了但不实现
# witness，`tests/test_config_catalog.py` 就会红。但 **live 类没有任何绑定**:
# 新增一个 live 字段后，全量档不写任何断言，这个目录仍然全绿，
# 整个 witness 体系的「防漏测」承诺在 live 类上是空的。
#
# 补法: 每个 live 字段必须落进下面两张表之一 ——
#   LIVE_CHECKS —— 已有专属断言（全量档跑一次就能证伪该字段）
#   LIVE_GAPS   —— 尚无专属断言，**显式记账**（含原因），不允许静默遗漏
#
# 契约由 `tests/test_config_catalog.py::test_live_fields_are_bound_or_accounted` 守住:
# 新增 live 字段却既不绑定也不记账 → 测试红。
LIVE_CHECKS: dict[str, tuple[str, ...]] = {
    # ---------- 冷却 / 限流 / 命令链 ----------
    "mr_cooldown_seconds": ("cooldown",),
    "max_review_calls_per_mr": ("rate_limit",),
    "pr_commands": ("auto_chain",),
    "push_commands": ("push_dispatch",),
    # ---------- 仓库规则上下文 ----------
    "repo_context_rules_dir": ("config_real:repo_context",),
    "review_profile_root": ("config_real:repo_context",),
    # ---------- 场景包驱动规划 ----------
    "review_profile": ("config_real:review_mode",),
    # ---------- LLM ----------
    "llm_provider": ("telemetry:review_runs",),
    "qoder_sdk_model": ("telemetry:review_runs",),
    # ---------- 周报 ----------
    "weekly_dingtalk_dry_run": ("weekly_notify_gate",),
    "weekly_collectors": ("weekly_report",),
    "weekly_enabled": ("weekly_report",),
    "weekly_target_project_id": ("weekly_report",),
    "weekly_target_branch": ("weekly_report",),
    "data_dir": ("weekly_report",),
}

#: live 字段中**尚无专属 E2E 断言**的台账（field -> 原因）。
#: 台账只应随新断言落地而缩小，不应新增。
LIVE_GAPS: dict[str, str] = {
    # ---------- GitLab / webhook ----------
    "gitlab_url": "全量档全程依赖，但没有『URL 配错会红』的专属断言",
    "gitlab_pat": "同上：鉴权失败会中断，但没有独立的断言面",
    "gitlab_webhook_secret": "同上：鉴权有单元测试，E2E 侧无专属断言",
    "gitlab_bot_username": "机器人回环在真实 E2E 里不会被触发（E2E 客户端是真人账号）",
    "gitlab_disable_bot_loop_check": "同上，且该开关默认关闭",
    # ---------- LLM: opencode provider（本部署未启用） ----------
    "opencode_url": "本部署 LLM_PROVIDER 非 opencode，无 live 断言面",
    "opencode_username": "本部署 LLM_PROVIDER 非 opencode，opencode 分支从未执行",
    "opencode_password": "本部署 LLM_PROVIDER 非 opencode，opencode 分支从未执行",
    "opencode_model": "本部署 LLM_PROVIDER 非 opencode，opencode 分支从未执行",
    "opencode_timeout": "本部署 LLM_PROVIDER 非 opencode，opencode 分支从未执行",
    # ---------- LLM: qodercli fallback provider（本部署未启用） ----------
    "qodercli_node_path": "本部署走 qoder_sdk，qodercli 仅兜底路径未触发",
    "qodercli_js_path": "本部署走 qoder_sdk，qodercli 兜底路径未触发",
    "qodercli_model": "本部署走 qoder_sdk，qodercli 兜底路径未触发",
    "qodercli_timeout": "本部署走 qoder_sdk，qodercli 兜底路径未触发",
    "qodercli_permission_mode": "本部署走 qoder_sdk，qodercli 兜底路径未触发",
    "qodercli_max_turns": "本部署走 qoder_sdk，qodercli 兜底路径未触发",
    "qodercli_fallback_model": "本部署走 qoder_sdk，qodercli 兜底路径未触发",
    # ---------- LLM: qoder_sdk ----------
    "qoder_sdk_pat": "凭证类字段，E2E 断言只能间接体现为『调用成功』",
    "qoder_sdk_edition": "同上：edition 只决定加载哪个品牌 SDK",
    "qoder_sdk_cli_path": "CLI 路径只在特定部署形态下生效，本机走 SDK 内置",
    "qoder_sdk_fallback_model": "仅在主模型失败时触发，E2E 未构造该场景",
    "qoder_sdk_timeout": "超时场景未在 E2E 构造",
    "qoder_sdk_max_turns": "轮次上限场景未在 E2E 构造",
    "qoder_sdk_permission_mode": "权限模式场景未在 E2E 构造",
    "qoder_sdk_disallowed_tools": "禁用工具列表未在 E2E 验证",
    # ---------- 队列 ----------
    "redis_url": "队列可用性由 E2E 全流程隐式覆盖，没有独立断言面",
    "rq_queue_name": "队列可用性由全流程隐式覆盖，无独立断言面",
    "rq_weekly_queue_name": "周报队列未被 E2E 触发（周报走同步路径）",
    "rq_worker_timeout": "超时场景未在 E2E 构造",
    # ---------- 日志 ----------
    "log_level": "日志内容未纳入 E2E 断言",
    "log_format": "日志格式未纳入 E2E 断言",
    "log_rotation": "轮转是时间/体积触发，E2E 难以构造",
    # ---------- 周报 ----------
    "weekly_notifier": "notify 实现由 weekly_notify_gate 覆盖，但 notifier 名本身未断言",
    "weekly_dingtalk_webhook_url": "凭证类字段；真发被安全闸挡住，E2E 刻意不验证外发",
    "weekly_dingtalk_secret": "凭证类字段；真发被安全闸挡住，E2E 刻意不验证外发",
    "weekly_dingtalk_retry": "重试只在真发失败时触发，E2E 不做真发",
    "weekly_md_chunk_limit": "分片数只体现在 CLI 输出的 chunks，未作为断言面",
    "weekly_report_title": "标题未纳入 E2E 断言（周报 flow 只断言产物存在）",
    "weekly_report_emoji": "emoji 未纳入 E2E 断言（周报 flow 只断言产物存在）",
    "weekly_dashboard_url": "dashboard 链接未纳入 E2E 断言",
}

#: 带前缀的断言名里，允许出现的后缀全集（防拼错）。
E2E_ASSERTION_SUFFIXES: dict[str, frozenset[str]] = {
    "telemetry:": frozenset({
        "mr_activity", "review_runs", "suggestions", "actions", "file_state", "api_params",
    }),
    "config_real:": frozenset({
        "review_mode", "priority_paths", "full_files", "test_sampling", "repo_context",
    }),
    "config:": frozenset({
        "suggestions_present", "suggestions_within_limit", "no_below_min_severity",
        "conftest_skip_policy_respected", "rule_key_cited",
    }),
}

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
    "rule_actions": "tests/test_publishing_module.py",
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
