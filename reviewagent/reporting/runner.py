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
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


def _resolve_tz(name: str) -> tzinfo:
    """把 `REVIEWAGENT_WEEKLY_TIMEZONE` 解析成 tzinfo；非法值直接失败."""
    try:
        return ZoneInfo(name)
    except (ValueError, OSError, ZoneInfoNotFoundError) as exc:
        raise ValueError(
            f"invalid weekly timezone {name!r}: use an IANA name like Asia/Shanghai"
        ) from exc


def _week_bounds(
    now: datetime | None = None,
    tz: tzinfo | None = None,
    week_offset: int = 0,
) -> tuple[datetime, datetime]:
    """算本周 (或 N 周前) 的 (week_start, week_end).

    week_offset=0 = 本周, -1 = 上周.
    """
    if now is None:
        # 默认 Asia/Shanghai (UTC+8)
        tz = tz or timezone(timedelta(hours=8))
        now = datetime.now(tz)
    elif tz is not None and now.tzinfo is not None:
        now = now.astimezone(tz)
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

    # 使用 config.weekly_reports_dir 保证与 API 读取路径一致
    # 之前用 Path("data/weekly_reports") 相对路径，systemd CWD 下会写到
    # /home/workflow/ReviewAgent/data/weekly_reports/，而 API 读的是
    # config.data_dir / "weekly_reports" = /home/workflow/data/weekly_reports/
    #
    # ⚠️ **注意** (2026-11-06): 调用方 (scripts/weekly_report.py 的 --enqueue 分支)
    #    会把 output_dir 塞进 RQ job 参数, 一旦它不是 None, 下面这行 config 分支
    #    就**走不到** —— 写盘路径由"入队时"决定, 而不是"worker 运行时"决定。
    #    调用方传的是仓库内的相对路径 → 周报落进 ReviewAgent/data/, API 读
    #    /home/workflow/data/ → 差一整个目录 → 前端永远停在旧周报 (实测停在 W37, 实际 W44)。
    #    修法: 调用方改传 config 的绝对路径 (scripts/weekly_report.py 已改)。
    from reviewagent.config import config as _main_config
    output_dir = output_dir or _main_config.weekly_reports_dir
    # 兜底: 若调用方传的是**相对路径**, 纠正成 config 的绝对路径 ——
    # 否则写出来的文件 API 根本读不到, 而症状是"周报停在几周前", 极难查。
    #
    # ⚠️ 判据不能用 `Path.is_absolute()`: 在 Windows 上 Path("/tmp/x").is_absolute() 是
    #    **False** (没有盘符), 于是"显式指定的绝对目录" (e2e 用的 /tmp/...) 会被误判成
    #    相对路径而被覆盖 —— 那是另一个坑。
    #    正确判据: 相对路径 = 不含盘符、且不以分隔符开头。
    #    ⚠️ 分隔符要**两个平台都算**: Windows 上 Path("/tmp/x") 的 str 是 "\\tmp\\x"
    #    (反斜杠开头), 只查 "/" 会漏判 → 显式绝对路径又被误纠正。
    #    Unix 绝对路径 "/tmp/x" → 以 "/" 开头; Windows 绝对路径 "C:\\x" → 有盘符。
    _od = Path(output_dir)
    _s = str(output_dir)
    _is_rel = (
        (not _od.is_absolute())
        and not _od.drive
        and not _s.startswith("/")
        and not _s.startswith("\\")
    )
    if _is_rel:
        logger.warning(
            "reporting.output_dir 是相对路径 ({}) —— 纠正为 config 的 {} (与 API 读取一致)",
            output_dir, _main_config.weekly_reports_dir,
        )
        output_dir = _main_config.weekly_reports_dir

    tz = _resolve_tz(cfg.timezone)
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
        dashboard_url=cfg.dashboard_url,
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
