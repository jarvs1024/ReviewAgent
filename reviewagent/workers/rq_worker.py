"""RQ worker classes used by ReviewAgent launchers."""
from __future__ import annotations

from typing import Any

from rq.worker import SpawnWorker


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
