"""Lock primitives — TTL + fence (owner token) + compare-and-delete.

B1: diff_head 锁必带 EX TTL (24h)，避免 Redis 长期累积
B2: chain 锁改成 owner-token + Lua compare-and-delete 释放
"""
from __future__ import annotations

import time

import pytest

from reviewagent.webhook.locks import (
    _CHAIN_LOCK_TTL_SECONDS,
    _DIFF_HEAD_TTL_SECONDS,
    _host_pid_token,
)


@pytest.fixture
def fake_redis(monkeypatch):
    """用 fakeredis 替换真实 Redis (无 IO)."""
    import fakeredis as _fr
    r = _fr.FakeStrictRedis(decode_responses=True)
    # 注: config 是 frozen dataclass, 不能 setattr. 我们只需把 _get_redis 替换成 fakeredis,
    # MRLockManager 不会再读 config.redis_url.
    monkeypatch.setattr(
        "reviewagent.webhook.locks.MRLockManager._get_redis",
        lambda self: r,
    )
    return r


# ---------- B1: diff_head TTL ----------

def test_diff_head_first_call_writes_with_ttl(fake_redis) -> None:
    from reviewagent.webhook.locks import MRLockManager
    mgr = MRLockManager(cooldown_seconds=60)
    head_sha = "abc123def"
    key = f"reviewagent:diff_head:{34}:{211}"
    res = mgr.check_diff_head_changed(34, 211, head_sha)
    assert res is True
    # B1: 必须带 TTL (24h = 86400s)
    ttl = fake_redis.ttl(key)
    assert ttl > 0, f"diff_head key 必须有 TTL, 实际 ttl={ttl}"
    assert ttl <= _DIFF_HEAD_TTL_SECONDS
    # 极小余量: (24h - 5s) <= ttl <= (24h)
    assert ttl >= _DIFF_HEAD_TTL_SECONDS - 5


def test_diff_head_changed_path_renews_ttl(fake_redis) -> None:
    from reviewagent.webhook.locks import MRLockManager
    mgr = MRLockManager(cooldown_seconds=60)
    key = f"reviewagent:diff_head:{34}:{211}"
    # 首次
    mgr.check_diff_head_changed(34, 211, "aaa111")
    time.sleep(1.1)
    first_ttl = fake_redis.ttl(key)
    # SHA 变了 → 写新 SHA + 续约 TTL
    res = mgr.check_diff_head_changed(34, 211, "bbb222")
    assert res is True
    new_ttl = fake_redis.ttl(key)
    # 新 TTL 必须刷新 (几乎 = 24h)
    assert new_ttl >= first_ttl


def test_diff_head_unchanged_no_ttl_refresh(fake_redis) -> None:
    """同 SHA 不应该刷新 TTL (避免无意义 Redis IO)."""
    from reviewagent.webhook.locks import MRLockManager
    mgr = MRLockManager(cooldown_seconds=60)
    key = f"reviewagent:diff_head:{34}:{211}"
    mgr.check_diff_head_changed(34, 211, "aaa111")
    time.sleep(1.1)
    first_ttl = fake_redis.ttl(key)
    # 同 SHA → False, 不写回 (TTL 自然衰减)
    res = mgr.check_diff_head_changed(34, 211, "aaa111")
    assert res is False
    second_ttl = fake_redis.ttl(key)
    # second 必须 <= first
    assert second_ttl <= first_ttl


def test_diff_head_empty_head_sha_returns_false(fake_redis) -> None:
    from reviewagent.webhook.locks import MRLockManager
    mgr = MRLockManager(cooldown_seconds=60)
    assert mgr.check_diff_head_changed(34, 211, "") is False
    # 不写 Redis
    key = f"reviewagent:diff_head:{34}:{211}"
    assert fake_redis.get(key) is None


# ---------- B2: chain lock fence ----------

def test_chain_lock_basic_acquire_release(fake_redis) -> None:
    from reviewagent.webhook.locks import MRLockManager
    mgr = MRLockManager(cooldown_seconds=60)
    lock = mgr.get_lock(34, 211)
    assert lock.acquire(blocking=False) is True
    key = f"reviewagent:lock:{34}:{211}"
    assert fake_redis.get(key) is not None
    assert lock.owner is not None
    assert lock.release() is True
    # 释放后 key 应被删
    assert fake_redis.get(key) is None


def test_chain_lock_owner_mismatch_not_released(fake_redis) -> None:
    """别人拿了锁, 当前 owner 释放不能误删 (compare-and-delete)."""
    from reviewagent.webhook.locks import MRLockManager
    mgr = MRLockManager(cooldown_seconds=60)
    key = f"reviewagent:lock:{34}:{211}"

    # Lock A 拿到锁
    lock_a = mgr.get_lock(34, 211)
    assert lock_a.acquire(blocking=False) is True

    # 模拟 token 被覆盖 (e.g. 前一个 owner 卡死, Redis TTL 还没过期)
    # 别人/另一个 worker 接管
    fake_owner_token = "imposter-worker:99999:deadbeef"
    fake_redis.set(key, fake_owner_token, ex=1800)

    # Lock A 仍以为自己持有原始 owner, 试图 release → 应该失败
    res_a = lock_a.release()
    assert res_a is False, "lock_a.release() 应在 token mismatch 时返回 False"
    # key 仍存在 (imposter 拥有)
    assert fake_redis.get(key) == fake_owner_token


def test_chain_lock_contention_blocks_second(fake_redis) -> None:
    """同时只能有一个 lock 拿到锁 (blocking=False)."""
    from reviewagent.webhook.locks import MRLockManager
    mgr = MRLockManager(cooldown_seconds=60)
    lock_a = mgr.get_lock(34, 211)
    assert lock_a.acquire(blocking=False) is True

    lock_b = mgr.get_lock(34, 211)
    assert lock_b.acquire(blocking=False) is False, "已有 lock 时, 第二个应拿不到"
    # owner 不变
    assert lock_b.owner is None


def test_chain_lock_release_without_owner_returns_false(fake_redis) -> None:
    from reviewagent.webhook.locks import MRLockManager
    mgr = MRLockManager(cooldown_seconds=60)
    lock = mgr.get_lock(34, 211)
    # 未 acquire 就 release
    assert lock.release() is False


def test_chain_lock_context_manager(fake_redis) -> None:
    from reviewagent.webhook.locks import MRLockManager
    mgr = MRLockManager(cooldown_seconds=60)
    key = f"reviewagent:lock:{34}:{211}"
    with mgr.get_lock(34, 211):
        assert fake_redis.get(key) is not None
    assert fake_redis.get(key) is None


def test_chain_lock_blocking_timeout(fake_redis) -> None:
    """blocking=True 但锁被占, blocking_timeout=0 应该立刻返回 False."""
    from reviewagent.webhook.locks import MRLockManager
    mgr = MRLockManager(cooldown_seconds=60)
    lock_a = mgr.get_lock(34, 211)
    assert lock_a.acquire(blocking=False) is True

    lock_b = mgr.get_lock(34, 211)
    res = lock_b.acquire(blocking=True, blocking_timeout=0)
    assert res is False


def test_chain_lock_default_ttl(fake_redis) -> None:
    from reviewagent.webhook.locks import MRLockManager
    mgr = MRLockManager(cooldown_seconds=60)
    lock = mgr.get_lock(34, 211)
    assert lock.acquire(blocking=False) is True
    key = f"reviewagent:lock:{34}:{211}"
    ttl = fake_redis.ttl(key)
    # 链锁 TTL 应该是 30 分钟
    assert ttl > 0
    assert ttl <= _CHAIN_LOCK_TTL_SECONDS


def test_host_pid_token_unique() -> None:
    """每个 token 都不一样 (uuid 包含)."""
    t1 = _host_pid_token()
    t2 = _host_pid_token()
    assert t1 != t2
    assert ":" in t1
