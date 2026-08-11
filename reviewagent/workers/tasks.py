"""RQ 任务 — 把 webhook 入的 job 实际执行.

启动方式（另起终端）:
    rq worker review-v2 --url redis://127.0.0.1:63790/2

支持的命令:
    /describe  → update_mr_title + description
    /improve   → MR summary 评论 + 行内可 Apply 的代码建议

每个命令的具体逻辑在 `reviewagent/commands/<name>.py`；本模块只负责：
    1. 入队（按 Note Hook / MR Hook 命令字符串分发）
    2. RQ 任务函数 — 调命令的 run() 并把异常转换成 failed telemetry
"""
from __future__ import annotations

from typing import Any

import redis
from rq import Queue, Retry

from reviewagent.config import config
from reviewagent.logging_setup import logger, setup_logging
from reviewagent.webhook.locks import locks

# RQ worker 进程启动时配置日志 (文件轮转 + stderr)
# main.py 只覆盖 webhook (uvicorn) 路径; worker 走这里
setup_logging()


# ---------- Redis / Queue ----------
_redis_conn: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _redis_conn
    if _redis_conn is None:
        _redis_conn = redis.from_url(config.redis_url)
    return _redis_conn


def get_queue() -> Queue:
    return Queue(config.rq_queue_name, connection=get_redis())


def get_weekly_queue() -> Queue:
    """周报专用队列 — 与 review 命令队列 (improve/describe/suggestion) 物理隔离, 互不阻塞.

    周报 job 含两次 opencode LLM 调用, 可能跑数分钟; 放进独立队列 + 独立 worker
    后, 即使周报卡住也不会拖慢其他功能模块. Redis / opencode / 模型 / SQLite 共享.
    """
    return Queue(config.rq_weekly_queue_name, connection=get_redis())


# ---------- 入队辅助（按命令） ----------
def _enqueue(
    command: str,
    *,
    project_id: int,
    mr_iid: int,
    triggered_by: str,
    actor_username: str,
) -> str:
    """内部 — 把命令对应的 rq 函数入队 (含重试)."""
    q = get_queue()
    job = q.enqueue(
        f"reviewagent.workers.tasks.run_{command}",
        project_id=project_id,
        mr_iid=mr_iid,
        triggered_by=triggered_by,
        actor_username=actor_username,
        job_timeout=config.rq_worker_timeout,
        result_ttl=3600,
        failure_ttl=86400,
        retry=Retry(max=2, interval=10),
    )
    return job.id


def enqueue_describe(
    *, project_id: int, mr_iid: int, triggered_by: str, actor_username: str,
) -> str:
    return _enqueue(
        "describe",
        project_id=project_id, mr_iid=mr_iid,
        triggered_by=triggered_by, actor_username=actor_username,
    )


def enqueue_improve(
    *, project_id: int, mr_iid: int, triggered_by: str, actor_username: str,
) -> str:
    return _enqueue(
        "improve",
        project_id=project_id, mr_iid=mr_iid,
        triggered_by=triggered_by, actor_username=actor_username,
    )


# 命令 → 入队函数 映射表（供 router 集中调度，避免重复 if/elif）
COMMAND_ENQUEUERS = {
    "describe": enqueue_describe,
    "improve": enqueue_improve,
}


def enqueue_suggestion_action(
    *,
    action: str,
    project_id: int,
    mr_iid: int,
    suggestion_note_id: str,
    actor_username: str,
    reason: str,
    file_path: str = "",
    target_line: int = 0,
) -> str:
    """入队 /adopt 或 /dismiss 命令 (针对单个 inline suggestion).

    file_path / target_line: 可选, 给 process_adopt / process_dismiss 用作
    note_id 查不到的 fallback (Fix C). 当 webhook 的 DiffNote 同时给出
    position (discussion 是行内 thread 时), 用它们做兜底匹配.
    """
    if action not in ("adopt", "dismiss"):
        raise ValueError(f"unsupported action: {action}")
    q = get_queue()
    job = q.enqueue(
        "reviewagent.workers.tasks.run_suggestion_action",
        action=action,
        project_id=project_id,
        mr_iid=mr_iid,
        suggestion_note_id=suggestion_note_id,
        actor_username=actor_username,
        reason=reason,
        file_path=file_path,
        target_line=target_line,
        job_timeout=120,
        result_ttl=3600,
        failure_ttl=86400,
        retry=Retry(max=2, interval=10),
    )
    return job.id


# ---------- 周报（含 opencode LLM 调用） ----------
def enqueue_weekly_report(
    *,
    week_offset: int = 0,
    output_dir: str | None = None,
    push: bool = False,
    project_id: int | None = None,
) -> str:
    """把整份周报（含 opencode LLM 变更摘要 / 质量扫描）作为 1 个 RQ job 入队.

    像 improve 一样: cron 只负责 fire-and-forget, 真正的采集 + LLM 调用 + 推送
    都发生在 RQ worker 内 (worker 已能调 opencode). 带 Retry, 失败自动重试 2 次.

    入队到独立的周报队列 (get_weekly_queue), 不与 improve/describe 主队列争抢.
    """
    q = get_weekly_queue()
    job = q.enqueue(
        "reviewagent.workers.tasks.run_weekly_report_job",
        week_offset=week_offset,
        output_dir=output_dir,
        push=push,
        project_id=project_id,
        job_timeout=config.rq_worker_timeout * 2,  # 含两次 LLM 调用
        result_ttl=3600,
        failure_ttl=86400,
        retry=Retry(max=2, interval=30),
    )
    logger.info(
        "weekly_report.enqueued job={} week_offset={} push={}",
        job.id, week_offset, push,
    )
    return job.id


def run_weekly_report_job(
    *,
    week_offset: int = 0,
    output_dir: str | None = None,
    push: bool = False,
    project_id: int | None = None,
) -> dict[str, Any]:
    """RQ job 体 — 在 worker 内跑整份周报 (含 opencode LLM 调用).

    不直接 pickle WeeklyReportConfig, 而是按 env 重建 (与 improve worker 一致),
    支持可选的 project_id 覆盖.
    """
    from pathlib import Path

    from reviewagent.reporting.config import WeeklyReportConfig
    from reviewagent.reporting.runner import run_weekly_job

    cfg = WeeklyReportConfig.from_env()
    if project_id is not None:
        cfg = WeeklyReportConfig(
            enabled=cfg.enabled,
            target_project_id=project_id,
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

    logger.info(
        "worker.run_weekly_report_job week_offset={} push={} project_id={}",
        week_offset, push, project_id,
    )
    return run_weekly_job(
        cfg=cfg,
        week_offset=week_offset,
        output_dir=Path(output_dir) if output_dir else None,
        push=push,
    )


# ---------- MR 命令链 ----------
def enqueue_mr_chain(
    *,
    commands: tuple[str, ...],
    project_id: int,
    mr_iid: int,
    triggered_by: str,
    actor_username: str,
) -> list[str]:
    """把 command 列表变成单个 RQ chain job（串行执行，避免并发竞态）.

    设计:
    - 单 job 内串行执行所有命令 (describe → improve)
    - 失败隔离: 某命令失败不影响后续命令
    - 不同 MR 并行: 多 worker 各自处理不同 MR 的 chain job
    - 重试: 瞬态失败自动重试 2 次 (interval=10s)

    返回: [job_id] (单元素列表)
    """
    q = get_queue()
    job = q.enqueue(
        "reviewagent.workers.tasks.run_mr_chain",
        commands=list(commands),
        project_id=project_id,
        mr_iid=mr_iid,
        triggered_by=triggered_by,
        actor_username=actor_username,
        job_timeout=config.rq_worker_timeout * len(commands),
        result_ttl=3600,
        failure_ttl=86400,
        retry=Retry(max=2, interval=10),
    )
    logger.info(
        "chain.enqueued commands={} project={} mr={} job={}",
        list(commands), project_id, mr_iid, job.id,
    )
    return [job.id]


def enqueue_command_from_note(
    *,
    command: str,
    project_id: int,
    mr_iid: int,
    triggered_by: str,
    actor_username: str,
) -> str:
    """Note Hook 入队 — 单个命令，不串链."""
    fn = COMMAND_ENQUEUERS.get(command)
    if fn is None:
        logger.warning("workers.unsupported_command cmd={}", command)
        raise NotImplementedError(f"command not yet implemented: {command}")
    return fn(
        project_id=project_id, mr_iid=mr_iid,
        triggered_by=triggered_by, actor_username=actor_username,
    )


# ---------- RQ 任务执行（worker 直接 invoke） ----------
def _run_command(
    command: str,
    *,
    project_id: int,
    mr_iid: int,
    triggered_by: str,
    actor_username: str,
) -> dict[str, Any]:
    """共用的 RQ job 体，按 command 字符串 import + invoke."""
    if command == "describe":
        from reviewagent.commands.describe import DescribeCommand, DescribeError
        CommandCls, ErrCls = DescribeCommand, DescribeError
    elif command == "improve":
        from reviewagent.commands.improve import ImproveCommand, ImproveError
        CommandCls, ErrCls = ImproveCommand, ImproveError
    else:
        raise NotImplementedError(f"command not yet implemented: {command}")

    logger.info("worker.run_{} project={} mr={}", command, project_id, mr_iid)
    try:
        return CommandCls(
            project_id=project_id,
            mr_iid=mr_iid,
            triggered_by=triggered_by,
            actor_username=actor_username,
        ).run()
    except ErrCls as e:
        logger.error(
            "worker.run_{} failed project={} mr={} err={}",
            command, project_id, mr_iid, e,
        )
        raise


def run_describe(**kw) -> dict[str, Any]:
    return _run_command("describe", **kw)


def run_improve(**kw) -> dict[str, Any]:
    return _run_command("improve", **kw)


def run_suggestion_action(
    *,
    action: str,
    project_id: int,
    mr_iid: int,
    suggestion_note_id: str,
    actor_username: str,
    reason: str,
    file_path: str = "",
    target_line: int = 0,
) -> dict[str, Any]:
    """处理 /adopt 或 /dismiss — 不调用 opencode, 直接 resolve discussion + record telemetry.

    file_path / target_line 透传给 process_*, 让其在 note_id 查不到时降级到
    file:line 兜底 (Fix C).
    """
    from reviewagent.commands.suggestion_actions import process_adopt, process_dismiss
    logger.info(
        "worker.run_suggestion_action action={} project={} mr={} discussion={} actor={} file={} line={}",
        action, project_id, mr_iid, suggestion_note_id, actor_username,
        file_path or "-", target_line or 0,
    )
    if action == "adopt":
        return process_adopt(
            project_id=project_id,
            mr_iid=mr_iid,
            suggestion_note_id=suggestion_note_id,
            actor_username=actor_username,
            reason=reason,
            file_path=file_path,
            target_line=target_line,
        )
    elif action == "dismiss":
        return process_dismiss(
            project_id=project_id,
            mr_iid=mr_iid,
            suggestion_note_id=suggestion_note_id,
            actor_username=actor_username,
            reason=reason,
            file_path=file_path,
            target_line=target_line,
        )
    else:
        raise NotImplementedError(f"unsupported action: {action}")


def run_mr_chain(
    *,
    commands: list[str],
    project_id: int,
    mr_iid: int,
    triggered_by: str,
    actor_username: str,
) -> list[dict[str, Any]]:
    """串行执行命令链 — 单 RQ job 内按顺序跑完所有命令.

    失败隔离: 某命令失败不影响后续命令执行.
    返回: 每个命令的执行结果列表.

    并发保护: 同 MR 的多个 chain job (来自多次 Apply / push) 共享一把 Redis
    锁, 强制串行执行. 避免:
      - V{N} 版本号 race condition (基于 runs 计数, 并发读会跳号或重复)
      - inline_posted / summary placeholder 互相覆盖 (都发到 GitLab 同一 MR 评论)
      - dedup_at_line 读到中间态 head_sha (前一个 chain 还没 finish_run, 但已
        emit_run_started; 后一个 chain 算 V{N} 时把前一个当已完成)
    """
    # 阻塞锁: 等到拿到锁才执行. blocking_timeout=600 (10 分钟) 兜底防 worker 卡死.
    # 不同 MR 并行: 锁 key 包含 project_id + mr_iid, 互不阻塞.
    # redis-py 8.x 签名: acquire(blocking=True, blocking_timeout=...)
    lock = locks.get_lock(project_id, mr_iid)
    if not lock.acquire(blocking=True, blocking_timeout=600):
        logger.warning(
            "chain.lock_timeout project={} mr={} — 释放锁失败/超时, 放弃本次 chain",
            project_id, mr_iid,
        )
        return [{"command": "lock", "status": "failed", "error": "lock_timeout"}]
    try:
        logger.info(
            "chain.lock_acquired project={} mr={} commands={}",
            project_id, mr_iid, list(commands),
        )
        results: list[dict[str, Any]] = []
        for cmd in commands:
            try:
                result = _run_command(
                    cmd,
                    project_id=project_id,
                    mr_iid=mr_iid,
                    triggered_by=triggered_by,
                    actor_username=actor_username,
                )
                results.append({"command": cmd, "status": "success", "result": result})
            except Exception as e:
                logger.error(
                    "chain.run_{} failed project={} mr={} err={}",
                    cmd, project_id, mr_iid, e,
                )
                results.append({"command": cmd, "status": "failed", "error": str(e)})
        return results
    finally:
        try:
            lock.release()
        except Exception as e:
            logger.warning("chain.lock_release failed project={} mr={} err={}",
                           project_id, mr_iid, e)
