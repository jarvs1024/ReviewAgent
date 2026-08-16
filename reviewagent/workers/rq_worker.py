"""RQ worker classes used by ReviewAgent launchers."""
from __future__ import annotations

import struct
from typing import Any

from rq.worker import SpawnWorker

from reviewagent.logging_setup import logger


class ReviewAgentSpawnWorker(SpawnWorker):
    """Spawn jobs without macOS-unsafe ``fork()`` calls.

    RQ 2.10 serializes Redis connection kwargs into the spawned interpreter.
    redis-py 8.1 adds an in-memory ``himport_registry`` object whose repr is
    not valid standalone Python, so remove it only while RQ builds the child
    command and restore it immediately afterwards.
    """

    def fork_work_horse(self, job: Any, queue: Any) -> Any:
        connection_kwargs = self.connection.connection_pool.connection_kwargs
        missing = object()
        himport_registry = connection_kwargs.pop("himport_registry", missing)
        try:
            return super().fork_work_horse(job, queue)
        finally:
            if himport_registry is not missing:
                connection_kwargs["himport_registry"] = himport_registry

    def bootstrap(self, logging_level="INFO", date_format=None, log_format=None):
        # worker 主进程啟動时 (进入 work 循环前) 清一次孤儿 running 记录.
        # 进程外, 不依赖被杀的 work-horse 自救 — 根治 OOM/SIGTERM 强杀后
        # review_runs 永久停在 running 的假象.
        try:
            from reviewagent.config import config
            from reviewagent.telemetry.store import get_store

            n = get_store().sweep_orphaned_runs(
                threshold_seconds=config.rq_worker_timeout + 300
            )
            if n:
                logger.warning("worker.boot: recovered %d orphaned running runs", n)
        except Exception as e:  # noqa: BLE001 — sweep 失败绝不应阻断 worker 启动
            logger.warning("worker.boot sweep failed (non-fatal): {}", e)
        return super().bootstrap(
            logging_level=logging_level, date_format=date_format, log_format=log_format
        )

    def handle_work_horse_killed(
        self,
        job: Any,
        retpid: int,
        ret_val: int,
        rusage: struct.Struct | None,
    ) -> None:
        """Work-horse 被杀后的清理回调.

        When work-horse is terminated (SIGTERM/SIGKILL/OOM), this callback runs
        in the worker main process. We use it to:
        1. Mark any running telemetry records for this job as failed
        2. Release the per-MR chain lock if held

        This prevents:
        - review_runs stuck in "running" forever
        - chain locks held for 30 minutes (TTL) blocking next chain
        """
        # Call parent first (logs the event)
        super().handle_work_horse_killed(job, retpid, ret_val, rusage)

        # Extract project_id and mr_iid from job kwargs
        # Job kwargs are stored in job.kwargs (RQ 2.x) or job.meta
        project_id = None
        mr_iid = None
        try:
            kwargs = job.kwargs or {}
            project_id = kwargs.get("project_id")
            mr_iid = kwargs.get("mr_iid")
        except Exception:
            pass

        if not project_id or not mr_iid:
            # Not a MR chain job, nothing to clean up
            return

        logger.warning(
            "worker.horse_killed project={} mr={} job={} — cleaning up running records and lock",
            project_id, mr_iid, job.id,
        )

        # 1. Mark running records as failed
        try:
            from reviewagent.telemetry.store import get_store
            store = get_store()
            n = store.sweep_orphaned_runs_for_mr(project_id=project_id, mr_iid=mr_iid)
            if n:
                logger.warning(
                    "worker.horse_killed: recovered %d orphan running records project=%s mr=%s",
                    n, project_id, mr_iid,
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("worker.horse_killed sweep failed (non-fatal): {}", e)

        # 2. Release the chain lock (force release, ignoring owner check)
        # Why: work-horse was killed, it couldn't release the lock in finally block.
        #      The lock TTL (30min) is too long; we release immediately.
        try:
            from reviewagent.webhook.locks import locks
            lock = locks.get_lock(project_id, mr_iid)
            # Force delete the lock key (ignore owner mismatch)
            lock_key = f"reviewagent:lock:{project_id}:{mr_iid}"
            r = locks._get_redis()
            deleted = r.delete(lock_key)
            if deleted:
                logger.info(
                    "worker.horse_killed: released chain lock project=%s mr=%s",
                    project_id, mr_iid,
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("worker.horse_killed lock release failed (non-fatal): {}", e)
