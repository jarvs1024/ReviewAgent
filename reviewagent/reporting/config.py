"""Weekly report 配置 — env override + dataclass.

参考 pr-agent: WeeklyReportConfig (frozen dataclass + env 覆盖).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class WeeklyReportConfig:
    """周报运行时配置."""
    enabled: bool = True
    target_project_id: int = 0
    target_branch: str = "main"
    timezone: str = "Asia/Shanghai"
    collectors: tuple[str, ...] = ("telemetry",)
    notifier: str = "dingtalk"
    dingtalk_webhook_url: str = ""
    dingtalk_secret: str = ""
    dingtalk_dry_run: bool = True
    dingtalk_retry_attempts: int = 3
    markdown_chunk_limit: int = 18000
    report_title: str = "SSD自动化代码检视周报"
    report_emoji: str = "📊"
    cron_schedule: str = "Mon 09:00"  # systemd OnCalendar 格式, 用户可自定义
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "WeeklyReportConfig":
        return cls(
            enabled=_env_bool("REVIEWAGENT_WEEKLY_ENABLED", True),
            target_project_id=_env_int("REVIEWAGENT_WEEKLY_TARGET_PROJECT_ID", 0),
            target_branch=_env_str("REVIEWAGENT_WEEKLY_TARGET_BRANCH", "main"),
            timezone=_env_str("REVIEWAGENT_WEEKLY_TIMEZONE", "Asia/Shanghai"),
            collectors=tuple(
                # 默认全 3 段 (对齐 pr_agent), 用户可通过 env 覆盖
                _env_str("REVIEWAGENT_WEEKLY_COLLECTORS", "telemetry,merged_mrs,repo_scan")
                .replace(",", " ").split()
            ),
            notifier=_env_str("REVIEWAGENT_WEEKLY_NOTIFIER", "dingtalk"),
            # 兼容两种 env 命名: 命名空间前缀 vs 短名 (向后兼容 .env 老配置)
            dingtalk_webhook_url=(
                _env_str("REVIEWAGENT_WEEKLY_DINGTALK_WEBHOOK_URL", "")
                or _env_str("DINGTALK_WEBHOOK", "")
            ),
            dingtalk_secret=_env_str("REVIEWAGENT_WEEKLY_DINGTALK_SECRET", ""),
            dingtalk_dry_run=_env_bool("REVIEWAGENT_WEEKLY_DINGTALK_DRY_RUN", True),
            dingtalk_retry_attempts=_env_int("REVIEWAGENT_WEEKLY_DINGTALK_RETRY", 3),
            markdown_chunk_limit=_env_int("REVIEWAGENT_WEEKLY_MD_CHUNK_LIMIT", 18000),
            cron_schedule=_env_str("REVIEWAGENT_WEEKLY_CRON_SCHEDULE", "Mon 09:00"),
            report_title=_env_str("REVIEWAGENT_WEEKLY_REPORT_TITLE", "SSD自动化代码检视周报"),
            report_emoji=_env_str("REVIEWAGENT_WEEKLY_REPORT_EMOJI", "📊"),
        )


__all__ = ["WeeklyReportConfig"]
