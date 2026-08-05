"""per-MR 锁 + cooldown — Redis 分布式实现，多 worker 安全.

设计:
    - cooldown: Redis SET NX + EX (原子操作，跨 worker 共享)
    - per-MR 锁: redis.lock.Lock (可重入，带超时)
    - bot 自己发的评论不再触发检视
    - Redis 不可用时 fail-open (不阻塞主流程)
"""
from __future__ import annotations

import time
from typing import Any

from reviewagent.config import config
from reviewagent.logging_setup import logger


class MRLockManager:
    """per-MR 互斥锁 + cooldown (Redis-backed)."""

    def __init__(self, cooldown_seconds: int | None = None):
        self.cooldown = cooldown_seconds if cooldown_seconds is not None else config.mr_cooldown_seconds
        self._redis: Any = None

    def _get_redis(self):
        """Lazy-init Redis 连接 (复用)."""
        if self._redis is None:
            import redis as _redis_mod
            self._redis = _redis_mod.from_url(config.redis_url, decode_responses=True)
        return self._redis

    def check_diff_head_changed(self, project_id: int, mr_iid: int, head_sha: str) -> bool:
        """检查 MR 的 diff head SHA 是否变化（判断是否有新 commit）.

        Redis key: reviewagent:diff_head:{project_id}:{mr_iid}
        首次调用时存储并返回 True；后续调用比较是否变化.
        Redis 不可用时 fail-open (返回 True，不阻塞).
        """
        if not head_sha:
            return False
        key = f"reviewagent:diff_head:{project_id}:{mr_iid}"
        try:
            r = self._get_redis()
            prev = r.get(key)
            if prev is None:
                # 首次存储
                r.set(key, head_sha)
                return True
            if prev == head_sha:
                return False
            # SHA 变了 → 新 commit
            r.set(key, head_sha)
            return True
        except Exception as e:
            logger.warning("locks.diff_head redis failed (fail-open): {}", e)
            return True

    def is_bot(self, username: str) -> bool:
        """判断是否为 bot 自己 (防回环). 支持 "名字@工号" 格式.

        - GITLAB_DISABLE_BOT_LOOP_CHECK=true → 永远 False（测试/临时场景显式跳过）
        - 否则按 username @后部分 与 GITLAB_BOT_USERNAME 比对
        """
        if config.gitlab_disable_bot_loop_check:
            return False
        if not username:
            return False
        # 兼容 "中文名字@工号" 格式: 取 @ 后的部分比较
        bare = username.rsplit("@", 1)[-1] if "@" in username else username
        return bare.lower() == config.gitlab_bot_username.lower()

    def should_skip_max_review_calls(
        self,
        project_id: int,
        mr_iid: int,
        commands: tuple[str, ...] | list[str],
        max_calls: int | None = None,  # None = 读 config; 单测可 override
    ) -> tuple[bool, int]:
        """检查 MR 的 review 次数是否已达上限.

        Returns:
            (should_skip, current_count): should_skip=True 时表示已达上限

        Why: 没有上限的话, 每次 push / Apply 都触发一次 chain → 用户每次
        都能看到 V{N} 递增; 即使代码已修复完所有问题, 也会无止境轮转.
        加上限 (默认 8 次) 后, 达到上限 → skip + 在 MR 评论里发一次性
        "无更多检视 (已达 N 次上限)" 提示, 避免用户以为 bot 还在跑.

        Count 范围: 所有 commands (describe / improve / review) 合并计数,
        这样 pr_commands=(describe,improve) 一次 chain 也只算 1 轮 review,
        而不是 2 个 (避免描述 + 检视被算成 2 次).
        """
        from reviewagent.config import config as _config
        # 默认从 config 读, 调用方可 override (用于单测)
        if max_calls is None:
            max_calls = _config.max_review_calls_per_mr
        if max_calls <= 0:
            return False, 0  # 0 = 不限
        try:
            from reviewagent.telemetry.store import get_store
            store = get_store()
            # 取 command IN (commands) 的总计数
            placeholders = ",".join("?" for _ in commands)
            with store._conn() as conn:
                row = conn.execute(
                    f"SELECT COUNT(*) AS n FROM review_runs "
                    f"WHERE project_id=? AND mr_iid=? "
                    f"AND command IN ({placeholders})",
                    (project_id, mr_iid, *commands),
                ).fetchone()
            current = int(row["n"])
            return current >= max_calls, current
        except Exception as e:
            logger.warning("locks.max_review_calls redis/sqlite failed (fail-open): {}", e)
            return False, 0

    def should_skip_cooldown(
        self,
        project_id: int,
        mr_iid: int,
        command: str,
    ) -> bool:
        """检查 (MR, command) 是否在 cooldown 内.

        使用 Redis SET NX EX 原子操作，跨 worker 共享 cooldown 状态.
        Redis 不可用时 fail-open (返回 False，不阻塞).
        """
        key = f"reviewagent:cooldown:{project_id}:{mr_iid}:{command}"
        try:
            r = self._get_redis()
            result = r.set(key, str(int(time.time())), ex=self.cooldown, nx=True)
            return result is None  # None = key 已存在 → 在 cooldown 内
        except Exception as e:
            logger.warning("locks.cooldown redis failed (fail-open): {}", e)
            return False

    def get_lock(self, project_id: int, mr_iid: int):
        """获取 per-MR Redis 分布式锁（不同 MR 互不阻塞）.

        返回 redis.lock.Lock 上下文管理器，用法:
            with locks.get_lock(project_id, mr_iid):
                ...
        """
        key = f"reviewagent:lock:{project_id}:{mr_iid}"
        r = self._get_redis()
        return r.lock(key, timeout=600, thread_local=False)


# 全局单例
locks = MRLockManager()
