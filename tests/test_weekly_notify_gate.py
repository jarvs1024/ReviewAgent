"""周报外发门控契约: 真发必须同时满足"显式 --push"且"环境未开启 dry_run".

背景 (2026-11-11 真实业务验证发现):
    `run_weekly_job` 原来的门控是 `if push or not cfg.dingtalk_dry_run`。
    因为实例配置 `weekly_dingtalk_dry_run=false`(生产默认), 任何一次**手动本地**
    执行 `scripts/weekly_report.py` 都会把内部周报**真实推送**到团队钉钉群,
    与调用方是否显式要求外发无关 —— 验证/调试时属于意外外发。

    新契约:
        * 真发 = 显式 `--push`(定时任务 launchd 通过 WEEKLY_PUSH=true 透传)
          且环境未开启 `weekly_dingtalk_dry_run`(环境级总开关)。
        * 手动本地跑(无 --push)一律 dry-run: 只产出 artifact + markdown,
          不外发, 但仍走一次 notifier.send 以便日志能看到分片。
"""
from __future__ import annotations

from types import SimpleNamespace

from reviewagent.reporting.runner import _should_send_notice


def _cfg(dry_run: bool) -> SimpleNamespace:
    return SimpleNamespace(dingtalk_dry_run=dry_run)


def test_manual_run_without_push_never_sends_even_when_dry_run_disabled():
    """手动本地跑 (push=False) + 生产配置 (dry_run=False) → 不外发 (旧行为会外发)."""
    assert _should_send_notice(push=False, cfg=_cfg(dry_run=False)) is False


def test_explicit_push_sends_when_dry_run_disabled():
    """定时任务透传 --push + dry_run=False → 真发."""
    assert _should_send_notice(push=True, cfg=_cfg(dry_run=False)) is True


def test_dry_run_is_environment_level_kill_switch():
    """dry_run=true 是环境级总开关: 即使 --push 也不真发."""
    assert _should_send_notice(push=True, cfg=_cfg(dry_run=True)) is False
    assert _should_send_notice(push=False, cfg=_cfg(dry_run=True)) is False
