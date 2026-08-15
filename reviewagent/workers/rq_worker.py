"""RQ worker classes used by ReviewAgent launchers."""
from __future__ import annotations

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
        # worker 主进程启动时 (进入 work 循环前) 清一次孤儿 running 记录.
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
