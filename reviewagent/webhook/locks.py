"""per-MR 锁 + cooldown — Redis 分布式实现，多 worker 安全.

设计:
    - cooldown: Redis SET NX + EX (原子操作，跨 worker 共享)
    - per-MR 链锁: 自定义 owner-check fence (compare-and-delete 释放)
    - diff_head SHA 记录: 带 TTL (24h) 避免无限累积
    - bot 自己发的评论不再触发检视
    - Redis 不可用时 fail-open (不阻塞主流程)
"""
from __future__ import annotations

import os
import socket
import time
import uuid
from typing import Any

from reviewagent.config import config
from reviewagent.logging_setup import logger


# ---------- 内置常量 ----------
# diff_head SHA key 的 TTL: 24h. 足够让"MR 短暂 closed 重新 open"复用,
# 又不会无限增长 Redis 内存. MR 长期 merged 后这条也无所谓了.
_DIFF_HEAD_TTL_SECONDS = 24 * 3600

# chain lock 默认 TTL (Redis SET EX): 30 分钟. 超过这个时间未 release
# 说明 worker 卡死 / SIGKILL, Redis 自动释放, 不影响下一个 worker 接力.
# 跟 rq 任务超时 (默认 10 分钟) 留 3x 安全余量.
_CHAIN_LOCK_TTL_SECONDS = 1800


def _get_metric():
    """延迟导入 metrics — 避免 import 顺序 / 缺包导致连环失败."""
    try:
        from reviewagent.metrics import inc as _metric_inc
        return _metric_inc
    except Exception:  # noqa: BLE001
        return None


def _host_pid_token() -> str:
    """生成唯一 owner token — 跨 worker / 进程可靠区分."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class _ChainLock:
    """Per-MR 链锁 — 内置 owner token + compare-and-delete 释放.

    用法 (跟 contextlib 兼容):
        lock = locks.get_lock(project_id, mr_iid)
        if lock.acquire(blocking=True, blocking_timeout=N):
            try:
                ...
            finally:
                lock.release()

        # or with `with`:
        with locks.get_lock(project_id, mr_iid) as lock:
            ...
    """

    def __init__(self, redis: Any, key: str, ttl: int) -> None:
        self._redis = redis
        self._key = key
        self._ttl = ttl
        self._owner: str | None = None

    @property
    def owner(self) -> str | None:
        return self._owner

    @property
    def key(self) -> str:
        return self._key

    def acquire(self, *, blocking: bool = True, blocking_timeout: float = 600) -> bool:
        """获取锁.

        Args:
            blocking: True=阻塞等锁; False=拿不到立刻返回 False.
            blocking_timeout: 阻塞等待的最长秒数.

        Returns:
            bool: 是否成功拿到锁.
        """
        owner = _host_pid_token()
        if not blocking:
            ok = self._redis.set(self._key, owner, nx=True, ex=self._ttl)
            if ok:
                self._owner = owner
                inc = _get_metric()
                if inc:
                    inc(
                        "reviewagent_lock_chain_total",
                        kind="acquired",
                        project_id=self._key,
                    )
            return bool(ok)

        # blocking path
        deadline = time.monotonic() + blocking_timeout
        sleep_s = 0.05
        attempts = 0
        while True:
            ok = self._redis.set(self._key, owner, nx=True, ex=self._ttl)
            if ok:
                self._owner = owner
                attempts += 1
                inc = _get_metric()
                if inc:
                    inc(
                        "reviewagent_lock_chain_total",
                        kind="acquired",
                        key=self._key,
                        attempts=str(attempts),
                    )
                return True
            if time.monotonic() >= deadline:
                inc = _get_metric()
                if inc:
                    inc(
                        "reviewagent_lock_chain_total",
                        kind="timeout",
                        key=self._key,
                    )
                return False
            time.sleep(min(sleep_s, 0.5))
            sleep_s = min(sleep_s * 1.5, 0.5)

    def release(self) -> bool:
        """compare-and-delete 释放. 仅当 token 匹配时才删 key.

        Returns:
            bool: 成功释放返回 True; token 不匹配 (说明已被别人接管) 返回 False.
        """
        if not self._owner:
            return False
        owner = self._owner
        # Lua: 比较 value 后删除 — 原子
        script = (
            "if redis.call('get', KEYS[1]) == ARGV[1] then "
            "return redis.call('del', KEYS[1]) "
            "else return 0 end"
        )
        try:
            result = self._redis.eval(script, 1, self._key, owner)
            released = int(result or 0) > 0
            self._owner = None
            inc = _get_metric()
            if inc:
                inc(
                    "reviewagent_lock_chain_total",
                    kind="released" if released else "mismatch",
                    key=self._key,
                )
            return released
        except Exception as e:  # noqa: BLE001
            logger.warning("chain_lock.release failed key={} err={}", self._key, e)
            return False

    # 兼容旧 contextlib 风格
    def __enter__(self) -> "_ChainLock":
        if not self.acquire(blocking=True, blocking_timeout=600):
            raise RuntimeError(f"acquire lock timed out: {self._key}")
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()


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

    # ---------- diff_head 锁 ----------
    def check_diff_head_changed(
        self, project_id: int, mr_iid: int, head_sha: str
    ) -> bool:
        """检查 MR 的 diff head SHA 是否变化 (判断是否有新 commit).

        Redis key: reviewagent:diff_head:{project_id}:{mr_iid}
        首次调用时存储并返回 True；后续调用比较是否变化.
        Redis 不可用时 fail-open (返回 True，不阻塞).

        B1 新增:
            - 写入带 EX TTL (24h), 防止 Redis 长期累积 stale keys
            - 操作计入 metric `reviewagent_lock_diff_head_total{kind=acquired|contended}`
        """
        if not head_sha:
            return False
        key = f"reviewagent:diff_head:{project_id}:{mr_iid}"
        inc = _get_metric()
        try:
            r = self._get_redis()
            prev = r.get(key)
            if prev is None:
                # 首次存储 — 写 SET NX EX 原子操作 (带 TTL 防 unbounded growth)
                acquired = r.set(key, head_sha, nx=True, ex=_DIFF_HEAD_TTL_SECONDS)
                if acquired:
                    if inc:
                        inc(
                            "reviewagent_lock_diff_head_total",
                            kind="acquired",
                            project_id=str(project_id),
                            mr_iid=str(mr_iid),
                        )
                    return True
                # NX 失败: 极罕见 (别人刚写完) — 走更新路径
                r.set(key, head_sha, ex=_DIFF_HEAD_TTL_SECONDS)
                if inc:
                    inc(
                        "reviewagent_lock_diff_head_total",
                        kind="acquired",
                        project_id=str(project_id),
                        mr_iid=str(mr_iid),
                    )
                return True
            if prev == head_sha:
                # 没变 — 算 contended, 不写新 TTL (避免无意义刷新)
                if inc:
                    inc(
                        "reviewagent_lock_diff_head_total",
                        kind="contended",
                        project_id=str(project_id),
                        mr_iid=str(mr_iid),
                    )
                return False
            # SHA 变了 → 新 commit. 写新 TTL 续约 24h.
            r.set(key, head_sha, ex=_DIFF_HEAD_TTL_SECONDS)
            if inc:
                inc(
                    "reviewagent_lock_diff_head_total",
                    kind="acquired",
                    project_id=str(project_id),
                    mr_iid=str(mr_iid),
                )
            return True
        except Exception as e:
            logger.warning("locks.diff_head redis failed (fail-open): {}", e)
            return True

    # ---------- is_bot ----------
    def is_bot(self, username: str) -> bool:
        """判断是否为 bot 自己 (防回环). 支持 "名字@工号" 格式.

        - GITLAB_DISABLE_BOT_LOOP_CHECK=true → 永远 False (测试/临时场景显式跳过)
        - GITLAB_BOT_USERNAME 支持逗号分隔多个别名
        - 显示名为 "名字@用户名" 时, 两侧任一命中都视为 bot
        """
        if config.gitlab_disable_bot_loop_check:
            return False
        if not username:
            return False
        aliases = {
            item.strip().lower()
            for item in config.gitlab_bot_username.split(",")
            if item.strip()
        }
        identities = {
            item.strip().lower()
            for item in username.split("@")
            if item.strip()
        }
        return bool(aliases & identities)

    # ---------- max_review_calls ----------
    def should_skip_max_review_calls(
        self,
        project_id: int,
        mr_iid: int,
        commands: tuple[str, ...] | list[str],
        max_calls: int | None = None,
    ) -> tuple[bool, int]:
        """检查 MR 的 review 次数是否已达上限."""
        from reviewagent.config import config as _config
        if max_calls is None:
            max_calls = _config.max_review_calls_per_mr
        if max_calls <= 0:
            return False, 0
        try:
            from reviewagent.telemetry.store import get_store
            store = get_store()
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

    # ---------- cooldown ----------
    def should_skip_cooldown(
        self,
        project_id: int,
        mr_iid: int,
        command: str,
    ) -> bool:
        """检查 (MR, command) 是否在 cooldown 内."""
        key = f"reviewagent:cooldown:{project_id}:{mr_iid}:{command}"
        try:
            r = self._get_redis()
            result = r.set(key, str(int(time.time())), ex=self.cooldown, nx=True)
            return result is None
        except Exception as e:
            logger.warning("locks.cooldown redis failed (fail-open): {}", e)
            return False

    def should_skip_suggestion_cooldown(
        self,
        project_id: int,
        mr_iid: int,
        action: str,
        suggestion_note_id: str,
    ) -> bool:
        """按 (MR, action, suggestion_note_id) 维度判定 cooldown.

        Why 跟 should_skip_cooldown 分开: push hook 的 cooldown 防同一 head_sha
        重复入队合理; 但 /adopt /dismiss 是用户对单条建议的操作, 不同建议应该
        独立计数 (60s 内连续 dismiss 两条建议不应被第二条吞掉).
        """
        key = f"reviewagent:cooldown:{project_id}:{mr_iid}:{action}:{suggestion_note_id}"
        try:
            r = self._get_redis()
            result = r.set(key, str(int(time.time())), ex=self.cooldown, nx=True)
            return result is None
        except Exception as e:
            logger.warning("locks.cooldown_suggestion redis failed (fail-open): {}", e)
            return False

    # ---------- chain lock ----------
    def get_lock(self, project_id: int, mr_iid: int) -> _ChainLock:
        """获取 per-MR Redis 分布式锁 (owner-token + EX TTL).

        Returns:
            _ChainLock — `.acquire(blocking, blocking_timeout)` + `.release()`.

        行为变更 (B2):
            - 不再返回 redis-py 内置 lock.Lock (没有 owner token / 心跳续约)
            - 用 SET NX EX + Lua compare-and-delete 释放, 防止上个 owner
              卡死后下个 owner 误释放, 锁更稳.
        """
        key = f"reviewagent:lock:{project_id}:{mr_iid}"
        r = self._get_redis()
        return _ChainLock(r, key, _CHAIN_LOCK_TTL_SECONDS)


# 全局单例
locks = MRLockManager()
