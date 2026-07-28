"""RQ 任务 — 把 webhook 入的 job 实际执行.

启动方式（另起终端）:
    rq worker review-v2 --url redis://127.0.0.1:63790/2

支持的命令:
    /describe  → update_mr_title + description
    /review    → MR summary 评论 + 行内 key_issues
    /improve   → MR summary 评论 + 行内可 Apply 的代码建议

每个命令的具体逻辑在 `reviewagent/commands/<name>.py`；本模块只负责：
    1. 入队（按 Note Hook / MR Hook 命令字符串分发）
    2. RQ 任务函数 — 调命令的 run() 并把异常转换成 failed telemetry
"""
from __future__ import annotations

from typing import Any

import redis
from rq import Queue

from reviewagent.config import config
from reviewagent.logging_setup import logger


# ---------- Redis / Queue ----------
_redis_conn: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _redis_conn
    if _redis_conn is None:
        _redis_conn = redis.from_url(config.redis_url)
    return _redis_conn


def get_queue() -> Queue:
    return Queue(config.rq_queue_name, connection=get_redis())


# ---------- 入队辅助（按命令） ----------
def _enqueue(
    command: str,
    *,
    project_id: int,
    mr_iid: int,
    triggered_by: str,
    actor_username: str,
) -> str:
    """内部 — 把命令对应的 rq 函数入队."""
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


def enqueue_review(
    *, project_id: int, mr_iid: int, triggered_by: str, actor_username: str,
) -> str:
    return _enqueue(
        "review",
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
    "review": enqueue_review,
    "improve": enqueue_improve,
}


def enqueue_command_from_note(
    *,
    command: str,
    project_id: int,
    mr_iid: int,
    triggered_by: str,
    actor_username: str,
) -> str:
    """Note Hook 入队 — 按 command 字符串分发."""
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
    elif command == "review":
        from reviewagent.commands.review import ReviewCommand, ReviewError
        CommandCls, ErrCls = ReviewCommand, ReviewError
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


def run_review(**kw) -> dict[str, Any]:
    return _run_command("review", **kw)


def run_improve(**kw) -> dict[str, Any]:
    return _run_command("improve", **kw)
