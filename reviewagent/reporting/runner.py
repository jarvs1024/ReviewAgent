"""周报主入口 — run_weekly_job.

参考 pr-agent: pr_agent/reporting/scheduler.run_weekly_job

流程:
    1. 算 week_start / week_end (默认本周)
    2. 实例化 collector(s) 拉数据 -> sections
    3. 构造 WeeklyArtifact
    4. JSON 落盘
    5. 渲染 markdown
    6. 实例化 notifier 推送 (默认 dry_run)

失败隔离: 任何 collector 抛错都被 scheduler 包成 status='failed' 的 SectionResult;
         notifier 推送失败不会影响 artifact 落盘.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from reviewagent.logging_setup import logger

from .artifact import (
    WeeklyArtifact,
    build_artifact,
    iso_week_label,
    write_artifact,
)
from .collectors import Collector, CollectorContext, SectionResult
from .collectors.telemetry import TelemetryCollector
from .config import WeeklyReportConfig
from .notifiers import DeliveryResult, Notifier
from .notifiers.dingtalk import DingTalkNotifier
from .renderer import render_markdown, split_markdown


def _week_bounds(
    now: datetime | None = None,
    tz: timezone | None = None,
    week_offset: int = 0,
) -> tuple[datetime, datetime]:
    """算本周 (或 N 周前) 的 (week_start, week_end).

    week_offset=0 = 本周, -1 = 上周.
    """
    if now is None:
        # 默认 Asia/Shanghai (UTC+8)
        tz = tz or timezone(timedelta(hours=8))
        now = datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    monday = now - timedelta(days=now.weekday())
    monday = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = monday + timedelta(weeks=week_offset)
    week_end = week_start + timedelta(days=7)
    return week_start, week_end


def _build_collector(name: str) -> Collector | None:
    # Local imports 避免启动期循环引用
    from .collectors.merged_mrs import MergedMrsCollector
    from .collectors.repo_scan import RepoScanCollector
    table: dict[str, type] = {
        "telemetry": TelemetryCollector,
        "merged_mrs": MergedMrsCollector,
        "repo_scan": RepoScanCollector,
    }
    cls = table.get(name)
    if cls is None:
        logger.warning("reporting.unknown_collector name={}", name)
        return None
    return cls()


def _build_notifier(cfg: WeeklyReportConfig, *, dry_run: bool | None = None) -> Notifier:
    """构造 notifier.

    Args:
        dry_run: 显式覆盖 cfg.dingtalk_dry_run (None = 用配置值)。
                 CLI `--push` 需要传 False, 否则 notifier 内部仍会 dry_run 空跑。
    """
    if cfg.notifier == "dingtalk":
        return DingTalkNotifier(
            webhook_url=cfg.dingtalk_webhook_url,
            secret=cfg.dingtalk_secret,
            retry_attempts=cfg.dingtalk_retry_attempts,
            dry_run=cfg.dingtalk_dry_run if dry_run is None else dry_run,
        )
    raise RuntimeError(f"unsupported notifier: {cfg.notifier!r}")


def run_weekly_job(
    cfg: WeeklyReportConfig | None = None,
    *,
    now: datetime | None = None,
    week_offset: int = 0,
    output_dir: Path | None = None,
    push: bool = False,
) -> dict[str, Any]:
    """跑一次完整周报 job.

    Args:
        cfg: 运行时配置 (None = 从 env 读)
        now: 测试注入 (默认 datetime.now)
        week_offset: 0=本周, -1=上周
        output_dir: artifact/md 输出目录 (默认 data/weekly_reports/)
        push: 是否真实推送 (False = 走 notifier 的 dry_run)

    Returns:
        dict 含 artifact_path / markdown_path / delivery_result
    """
    cfg = cfg or WeeklyReportConfig.from_env()
    if not cfg.enabled:
        logger.info("reporting.run skipped (REVIEWAGENT_WEEKLY_ENABLED=false)")
        return {"skipped": True, "reason": "disabled"}

    output_dir = output_dir or Path("data/weekly_reports")

    tz = timezone(timedelta(hours=8))  # Asia/Shanghai
    week_start, week_end = _week_bounds(now=now, tz=tz, week_offset=week_offset)

    logger.info(
        "reporting.run start week={} ({}) range={} ~ {} push={}",
        iso_week_label(week_start), week_offset, week_start.isoformat(),
        week_end.isoformat(), push,
    )

    # 加载上周 artifact 用于环比趋势 (LLM prompt + telemetry delta)
    prev_data: dict[str, Any] = {}
    prev_label = iso_week_label(week_start - timedelta(weeks=1))
    prev_path = output_dir / f"weekly-{prev_label}.json"
    if prev_path.exists():
        try:
            prev_artifact = WeeklyArtifact.from_dict(json.loads(prev_path.read_text(encoding="utf-8")))
            prev_data = {n: (sr.data or {}) for n, sr in prev_artifact.sections.items()}
            logger.info("reporting.prev_week loaded label={} sections={}", prev_label, list(prev_data.keys()))
        except Exception as e:
            logger.warning("reporting.prev_week load failed label={}: {}", prev_label, e)

    # 1. collectors
    ctx = CollectorContext(
        target_project_id=cfg.target_project_id,
        data_dir=str(output_dir),
        timezone=cfg.timezone,
        target_branch=cfg.target_branch,
        prev_data=prev_data,
    )
    sections: dict[str, SectionResult] = {}
    for name in cfg.collectors:
        c = _build_collector(name)
        if c is None:
            continue
        try:
            sections[name] = c.collect(week_start=week_start, week_end=week_end, ctx=ctx)
            logger.info("reporting.collector ok name={} status={}", name, sections[name].status)
        except Exception as e:
            logger.exception("reporting.collector crash name={}: {}", name, e)
            sections[name] = SectionResult(status="failed", error=str(e), data=None)

    # 2. artifact
    artifact = build_artifact(
        project_id=cfg.target_project_id or 0,
        week_start=week_start,
        week_end=week_end,
        timezone=cfg.timezone,
        sections=sections,
        report_title=cfg.report_title,
        report_emoji=cfg.report_emoji,
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    artifact_path = output_dir / f"weekly-{artifact.week_label}.json"
    write_artifact(artifact, artifact_path)

    # 3. render md
    md_text = render_markdown(artifact)
    md_path = output_dir / f"weekly-{artifact.week_label}.md"
    md_path.write_text(md_text, encoding="utf-8")
    logger.info("reporting.markdown written path={} bytes={}", md_path, len(md_text.encode("utf-8")))

    # 4. notify
    delivery: dict[str, Any] = {"skipped": True}
    if push or not cfg.dingtalk_dry_run:
        # 显式关掉 dry_run: --push 时即使 env 配置了 DRY_RUN=true 也要真发
        notifier = _build_notifier(cfg, dry_run=False)
        chunks = split_markdown(md_text, chunk_limit=cfg.markdown_chunk_limit)
        result = notifier.send(
            title=f"{cfg.report_emoji} {cfg.report_title} ({artifact.week_label})",
            markdown_chunks=chunks,
        )
        delivery = {
            "notifier": notifier.name,
            "success": result.success,
            "chunks_sent": result.chunks_sent,
            "chunks_total": result.chunks_total,
            "error": result.error,
            "meta": result.meta,
        }
    else:
        # 总是走一次 notifier.send (dry_run), 让 log 看到分片
        notifier = _build_notifier(cfg, dry_run=True)
        chunks = split_markdown(md_text, chunk_limit=cfg.markdown_chunk_limit)
        result = notifier.send(
            title=f"{cfg.report_emoji} {cfg.report_title} ({artifact.week_label})",
            markdown_chunks=chunks,
        )
        delivery = {
            "notifier": notifier.name,
            "dry_run": True,
            "chunks_total": result.chunks_total,
        }

    return {
        "week_label": artifact.week_label,
        "artifact_path": str(artifact_path),
        "markdown_path": str(md_path),
        "delivery": delivery,
        "sections": {n: sr.status for n, sr in sections.items()},
    }


if __name__ == "__main__":
    # 周报 CLI 统一入口在 scripts/weekly_report.py, 这里仅保留模块可执行兜底
    print("Use: python scripts/weekly_report.py [--week-offset 0] [--push] [--enqueue]")
