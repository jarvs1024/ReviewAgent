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
from reviewagent.logging_setup import logger
from reviewagent.webhook.locks import locks


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
) -> str:
    """入队 /adopt 或 /dismiss 命令 (针对单个 inline suggestion)."""
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
        job_timeout=120,
        result_ttl=3600,
        failure_ttl=86400,
        retry=Retry(max=2, interval=10),
    )
    return job.id


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
) -> dict[str, Any]:
    """处理 /adopt 或 /dismiss — 不调用 opencode, 直接 resolve discussion + record telemetry."""
    from reviewagent.commands.suggestion_actions import process_adopt, process_dismiss
    logger.info(
        "worker.run_suggestion_action action={} project={} mr={} discussion={} actor={}",
        action, project_id, mr_iid, suggestion_note_id, actor_username,
    )
    if action == "adopt":
        return process_adopt(
            project_id=project_id,
            mr_iid=mr_iid,
            suggestion_note_id=suggestion_note_id,
            actor_username=actor_username,
            reason=reason,
        )
    elif action == "dismiss":
        return process_dismiss(
            project_id=project_id,
            mr_iid=mr_iid,
            suggestion_note_id=suggestion_note_id,
            actor_username=actor_username,
            reason=reason,
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
    """
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
