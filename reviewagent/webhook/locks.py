"""per-MR 锁 + cooldown — 防 webhook 死循环 + 并发去重.

设计:
    - in-process 锁 + cooldown（PoC 阶段；多 worker 时需换 Redis 锁，Phase 2 处理）
    - bot 自己发的评论不再触发检视
    - description-only update 不会重新触发 /describe（除非显式 /describe 命令）
"""
from __future__ import annotations

import threading
import time

from reviewagent.config import config


class MRLockManager:
    """per-MR 互斥锁 + cooldown."""

    def __init__(self, cooldown_seconds: int | None = None):
        self.cooldown = cooldown_seconds if cooldown_seconds is not None else config.mr_cooldown_seconds
        self._locks: dict[tuple[int, int], threading.Lock] = {}
        self._last_triggered: dict[tuple[int, int, str], float] = {}
        self._meta_lock = threading.Lock()

    def _key(self, project_id: int, mr_iid: int) -> tuple[int, int]:
        return (project_id, mr_iid)

    def is_bot(self, username: str) -> bool:
        """判断是否为 bot 自己（防回环）."""
        if not username:
            return False
        return username.lower() == config.gitlab_bot_username.lower()

    def should_skip_cooldown(
        self,
        project_id: int,
        mr_iid: int,
        command: str,
    ) -> bool:
        """检查 (MR, command) 是否在 cooldown 内."""
        key = (project_id, mr_iid, command)
        with self._meta_lock:
            now = time.monotonic()
            last = self._last_triggered.get(key, 0.0)
            if now - last < self.cooldown:
                return True
            self._last_triggered[key] = now
            return False

    def get_lock(self, project_id: int, mr_iid: int) -> threading.Lock:
        """获取 per-MR 锁（不同 MR 互不阻塞）."""
        key = self._key(project_id, mr_iid)
        with self._meta_lock:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock


# 全局单例
locks = MRLockManager()