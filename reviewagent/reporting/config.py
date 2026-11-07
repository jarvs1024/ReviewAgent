"""Weekly report 配置 — 唯一 schema 的投影（spec §5.5.2）.

env 解析统一由 `settings.binder` 完成，本模块只从 `reviewagent.config.current()`
映射字段，不再直接读环境变量（T20 迁移掉最后一处配置边界例外）：

* `collectors` 的 schema 形态是逗号分隔 tuple；binder 只按逗号切，因此这里再按
  空白切一次，兼容 `telemetry merged_mrs` 这种历史写法；
* `weekly_report_title` 的 schema 默认是空串，真正生效的标题取
  `profile.reporting["weekly_title"]`（缺省 "SSD自动化代码检视周报"），
  这样 profile 才能覆盖标题；
* `DINGTALK_WEBHOOK` 短名由 schema 的 `aliases` 承接，不再手写 or 回退；
* 空串 env 由 binder 视为"未提供"并回退 schema 默认值，与旧 `_env_str` 一致。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from reviewagent.config import current


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
    cron_schedule: str = "Mon 10:30"  # systemd OnCalendar 格式, 周一 10:30 跑上周数据
    dashboard_url: str = "http://127.0.0.1:8080/code-review"  # 检视看板地址, 渲染到「本周检视概况」末尾
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_env(cls, profile: Any = None) -> WeeklyReportConfig:
        """从生效配置构造；未显式传 profile 时惰性解析激活场景包.

        env 已由 binder 在进程启动时解析进 `current()`；profile 只影响标题回退，
        没有 profile 数据时保持历史默认标题.
        """
        if profile is None:
            try:
                from reviewagent.scenario.profile import active_or_load

                profile = active_or_load()
            except Exception:  # noqa: BLE001
                profile = None
        reporting = getattr(profile, "reporting", None) or {}
        title_default = str(reporting.get("weekly_title") or "SSD自动化代码检视周报")
        cfg = current()
        return cls(
            enabled=cfg.weekly_enabled,
            target_project_id=cfg.weekly_target_project_id,
            target_branch=cfg.weekly_target_branch,
            timezone=cfg.weekly_timezone,
            # binder 只按逗号切；这里再按空白切一次，兼容 "a b" 写法。
            collectors=tuple(" ".join(cfg.weekly_collectors).replace(",", " ").split()),
            notifier=cfg.weekly_notifier,
            dingtalk_webhook_url=cfg.weekly_dingtalk_webhook_url,
            dingtalk_secret=cfg.weekly_dingtalk_secret,
            dingtalk_dry_run=cfg.weekly_dingtalk_dry_run,
            dingtalk_retry_attempts=cfg.weekly_dingtalk_retry,
            markdown_chunk_limit=cfg.weekly_md_chunk_limit,
            report_title=cfg.weekly_report_title or title_default,
            report_emoji=cfg.weekly_report_emoji,
            cron_schedule=cfg.weekly_cron_schedule,
            dashboard_url=cfg.weekly_dashboard_url,
        )


__all__ = ["WeeklyReportConfig"]
