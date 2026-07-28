"""RQ 任务 — 把 webhook 入的 job 实际执行.

启动方式（另起终端）:
    rq worker review --url redis://localhost:6379/0

PoC 阶段只实现 /describe；Phase 2 加 /review / /improve.
"""
from __future__ import annotations

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


# ---------- 入队辅助 ----------
def enqueue_describe(
    *,
    project_id: int,
    mr_iid: int,
    triggered_by: str,
    actor_username: str,
) -> str:
    """入队一个 /describe 任务."""
    q = get_queue()
    job = q.enqueue(
        "reviewagent.workers.tasks.run_describe",
        project_id=project_id,
        mr_iid=mr_iid,
        triggered_by=triggered_by,
        actor_username=actor_username,
        job_timeout=config.rq_worker_timeout,
        result_ttl=3600,
        failure_ttl=86400,
    )
    return job.id


def enqueue_command_from_note(
    *,
    command: str,
    project_id: int,
    mr_iid: int,
    triggered_by: str,
    actor_username: str,
) -> str:
    """入队一个 Note Hook 命令（PoC 阶段只支持 describe）。"""
    if command == "describe":
        return enqueue_describe(
            project_id=project_id,
            mr_iid=mr_iid,
            triggered_by=triggered_by,
            actor_username=actor_username,
        )
    # Phase 2: review / improve
    logger.warning("workers.unsupported_command cmd={}", command)
    raise NotImplementedError(f"command not yet implemented: {command}")


# ---------- 任务执行 ----------
def run_describe(
    *,
    project_id: int,
    mr_iid: int,
    triggered_by: str,
    actor_username: str,
) -> dict:
    """RQ worker 调用的入口: 实际执行 /describe.

    失败抛异常 → RQ 标记 failed；不入 SQLite 失败表（事件流由 telemetry 处理）.
    """
    from reviewagent.commands.describe import DescribeCommand, DescribeError

    logger.info("worker.run_describe project={} mr={}", project_id, mr_iid)
    try:
        return DescribeCommand(
            project_id=project_id,
            mr_iid=mr_iid,
            triggered_by=triggered_by,
            actor_username=actor_username,
        ).run()
    except DescribeError as e:
        logger.error("worker.run_describe failed project={} mr={} err={}",
                     project_id, mr_iid, e)
        raise