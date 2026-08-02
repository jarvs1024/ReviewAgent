"""ReviewAgent 周报 CLI 入口 (thin wrapper over reviewagent.reporting.runner).

实际逻辑在 reviewagent/reporting/runner.py:
    1. collectors 拉数据 -> sections
    2. WeeklyArtifact 构造
    3. JSON 落盘 + markdown 渲染 + xlsx 生成
    4. notifier 推送 (默认 dry_run)

用法:
    python scripts/weekly_report.py                       # 本周, dry_run
    python scripts/weekly_report.py --week-offset -1      # 上周
    python scripts/weekly_report.py --push                # 真实推送到钉钉
    python scripts/weekly_report.py --project-id 34      # 覆盖 project
    python scripts/weekly_report.py --output-dir /tmp/w   # 自定义输出

数据源: reviewagent telemetry.db (SQLite)
设计: 参考 pr-agent/pr_agent/reporting/scheduler.run_weekly_job
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewagent.logging_setup import setup_logging  # noqa: E402
from reviewagent.reporting.config import WeeklyReportConfig  # noqa: E402
from reviewagent.reporting.runner import run_weekly_job  # noqa: E402

DEFAULT_OUTPUT_DIR = REPO_ROOT / "data" / "weekly_reports"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--week-offset", type=int, default=0,
                   help="0=本周, -1=上周 (默认 0)")
    p.add_argument("--push", action="store_true",
                   help="真实推送 (默认走 notifier.dry_run=True)")
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
                   help=f"输出目录 (默认: {DEFAULT_OUTPUT_DIR})")
    p.add_argument("--project-id", type=int, default=None,
                   help="覆盖 REVIEWAGENT_WEEKLY_TARGET_PROJECT_ID")
    p.add_argument("--enqueue", action="store_true",
                   help="只入 RQ 队列（由 worker 异步执行，含 opencode LLM 调用），fire-and-forget")
    return p.parse_args()


def main() -> int:
    setup_logging()
    args = parse_args()

    # 队列模式: 只把整份周报 job 入队, 真正的采集 + LLM + 推送在 worker 内跑
    if args.enqueue:
        from reviewagent.workers.tasks import enqueue_weekly_report
        job_id = enqueue_weekly_report(
            week_offset=args.week_offset,
            output_dir=str(DEFAULT_OUTPUT_DIR),
            push=args.push,
            project_id=args.project_id,
        )
        print(f"[ok] enqueued weekly_report job={job_id} (worker will run it)")
        return 0

    cfg = WeeklyReportConfig.from_env()
    if args.project_id is not None:
        cfg = WeeklyReportConfig(
            enabled=cfg.enabled,
            target_project_id=args.project_id,
            target_branch=cfg.target_branch,
            timezone=cfg.timezone,
            collectors=cfg.collectors,
            notifier=cfg.notifier,
            dingtalk_webhook_url=cfg.dingtalk_webhook_url,
            dingtalk_secret=cfg.dingtalk_secret,
            dingtalk_dry_run=cfg.dingtalk_dry_run,
            dingtalk_retry_attempts=cfg.dingtalk_retry_attempts,
            markdown_chunk_limit=cfg.markdown_chunk_limit,
            cron_schedule=cfg.cron_schedule,
        )

    result = run_weekly_job(
        cfg=cfg,
        week_offset=args.week_offset,
        output_dir=args.output_dir,
        push=args.push,
    )

    # CLI 友好输出
    print()
    print(f"[ok] week_label   : {result['week_label']}")
    print(f"[ok] artifact     : {result['artifact_path']}")
    print(f"[ok] markdown     : {result['markdown_path']}")
    print(f"[ok] sections     : {result['sections']}")
    delivery = result['delivery']
    if delivery.get('dry_run'):
        print(f"[ok] delivery     : dry_run, chunks={delivery.get('chunks_total')}")
    else:
        print(f"[ok] delivery     : notifier={delivery.get('notifier')} "
              f"success={delivery.get('success')} sent={delivery.get('chunks_sent')}/{delivery.get('chunks_total')}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
