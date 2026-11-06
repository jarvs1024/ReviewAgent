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
import dataclasses
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewagent.logging_setup import setup_logging  # noqa: E402
from reviewagent.reporting.config import WeeklyReportConfig  # noqa: E402
from reviewagent.reporting.runner import run_weekly_job  # noqa: E402

# ⚠️ 输出目录的**唯一事实源**是 config.weekly_reports_dir (= config.data_dir / "weekly_reports"),
#    不是这个仓库里的 ./data/weekly_reports —— 两者在 systemd 部署下**不是同一个目录**:
#      · API (reviewagent/api/router.py) 读的是 config.weekly_reports_dir
#        = REVIEWAGENT_DATA_DIR (默认 /home/workflow/data) / "weekly_reports"
#      · 之前这里硬编码 REPO_ROOT/"data"/"weekly_reports"
#        = /home/workflow/ReviewAgent/data/weekly_reports  ← systemd CWD 下的相对路径
#    结果: 定时器生成的周报写进了 A 目录, API 去 B 目录读 → 前端永远只看到旧周报
#    (2026-11-06 实测: W39~W44 全落在 ReviewAgent/data/, API 只看得到 W37)
#
#    所以这里**不再**自己拼路径, 统一从 config 取 (config 读同一个 env,
#    API 和 worker 因此指向同一个绝对路径)。
def _default_output_dir() -> Path:
    """默认输出目录 = config.weekly_reports_dir (与 API 读取路径一致)."""
    from reviewagent.config import config as _config
    return _config.weekly_reports_dir


DEFAULT_OUTPUT_DIR = _default_output_dir()


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
            # ⚠️ 这里传 config 的绝对路径, **不要**传仓库内的相对路径:
            #    worker 侧的 run_weekly_job 里是 `output_dir or _main_config.weekly_reports_dir`
            #    —— 只要 output_dir 不是 None, 那个 config 分支就**永远走不到**,
            #    于是写盘路径由**入队时**决定, 而不是由 worker 当时的 config 决定。
            #    (2026-11-06 实测: 之前硬塞 REPO_ROOT/"data"/"weekly_reports",
            #     job 落进 /home/workflow/ReviewAgent/data/, 而 API 读 /home/workflow/data/,
            #     差了一整个目录 → 前端停在 W37, 实际最新是 W44)
            output_dir=str(DEFAULT_OUTPUT_DIR),
            push=args.push,
            project_id=args.project_id,
        )
        print(f"[ok] enqueued weekly_report job={job_id} (worker will run it)")
        print(f"[ok] output_dir={DEFAULT_OUTPUT_DIR}")
        return 0

    cfg = WeeklyReportConfig.from_env()
    if args.project_id is not None:
        cfg = dataclasses.replace(cfg, target_project_id=args.project_id)

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
    if result.get("xlsx_path"):
        print(f"[ok] xlsx         : {result['xlsx_path']}")
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
