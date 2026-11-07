"""Task 18: 重复 webhook / 并发 push / 链锁超时 / Redis 故障语义.

对应 spec §7.7.2 与 §7.7.6：

* 重复 webhook 的「只入队一次」由 router 层 `check_diff_head_changed` + cooldown 兑现；
* 并发 chain 的串行由 `run_mr_chain` 的 per-MR 链锁兑现（锁等待超时 = fail-closed）；
* Redis 故障：cooldown / diff_head fail-open（不阻塞），链锁 fail-closed（job 失败，
  先于任何 `_run_command` 副作用）；
* 非 improve 链路（/adopt /dismiss、周报）同样携带并校验 job 指纹。

所有用例不依赖真实 Redis / GitLab：fakeredis + 替身 store + 替身命令执行体。
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import fakeredis
import pytest

from reviewagent.telemetry.store import Store
from reviewagent.workers import tasks

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_redis(monkeypatch):
    """fakeredis 替换 MRLockManager 的 Redis 连接（无 IO）."""
    redis_client = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr(
        "reviewagent.webhook.locks.MRLockManager._get_redis",
        lambda self: redis_client,
    )
    return redis_client


@pytest.fixture
def telemetry(tmp_path):
    """临时 telemetry Store 注入模块级单例（teardown 置 None）."""
    from reviewagent.telemetry import store as store_mod

    store = Store(tmp_path / "telemetry.db")
    store_mod._store = store
    try:
        yield store
    finally:
        store_mod._store = None


def _mr_open_payload(*, head_sha: str) -> dict:
    return {
        "object_kind": "merge_request",
        "event_type": "merge_request",
        "user": {"name": "Alice", "username": "alice"},
        "project": {"id": 34},
        "object_attributes": {
            "iid": 7,
            "action": "open",
            "state": "opened",
            "title": "Add helper",
            "source_branch": "feat/helper",
            "target_branch": "main",
            "draft": False,
            "diff_refs": {"head_sha": head_sha},
            "created_at": "2026-11-01T00:00:00+00:00",
            "updated_at": "2026-11-01T00:00:00+00:00",
        },
    }


class _FakeStore:
    """run_mr_chain 只用 sweep_orphaned_runs_for_mr，其余 store 调用不应出现."""

    def __init__(self) -> None:
        self.swept: list[tuple[int, int]] = []

    def sweep_orphaned_runs_for_mr(self, *, project_id: int, mr_iid: int) -> int:
        self.swept.append((project_id, mr_iid))
        return 0


# ---------------------------------------------------------------------------
# Step 1a: 重复 webhook —— 同一 head_sha 只入队一次（router 层）
# ---------------------------------------------------------------------------


async def test_duplicate_open_webhook_enqueues_once(fake_redis, telemetry, monkeypatch):
    """同 MR 同 head_sha 连续两次投递 → 只入队一次，第二次 skipped（same sha）."""
    from reviewagent.webhook import router as router_mod

    # 屏蔽场景配置写入带来的外部依赖：只断言 router 的入队决策
    monkeypatch.setattr(router_mod, "_upsert_mr_from_hook", lambda payload, mr: None)

    enqueued: list[dict] = []

    def _enqueue(**kwargs):
        enqueued.append(kwargs)
        return ["job-1"]

    payload = _mr_open_payload(head_sha="head-sha-1")

    first = await router_mod._handle_code_change(payload, "merge_request", _enqueue)
    second = await router_mod._handle_code_change(payload, "merge_request", _enqueue)

    assert first["status"] == "queued"
    assert len(enqueued) == 1
    assert second["status"] == "skipped"
    assert "same sha" in second["reason"]
    # 入队参数未被重复 webhook 改写
    assert enqueued[0]["project_id"] == 34
    assert enqueued[0]["mr_iid"] == 7
    assert enqueued[0]["commands"] == ("describe", "improve")


# ---------------------------------------------------------------------------
# Step 1b: 并发 push —— 同 MR 两个 chain 串行执行（链锁）
# ---------------------------------------------------------------------------


def test_concurrent_chains_for_same_mr_are_serialized(fake_redis, monkeypatch):
    """两线程同 project/mr 跑 run_mr_chain：锁保证命令体最大并发 = 1，两个都 success."""
    from reviewagent.webhook.locks import MRLockManager

    monkeypatch.setattr(tasks, "locks", MRLockManager(cooldown_seconds=60))
    monkeypatch.setattr(tasks, "_verify_job_manifest", lambda **_kwargs: None)
    fake_store = _FakeStore()
    monkeypatch.setattr(tasks, "get_store", lambda: fake_store)

    counter_lock = threading.Lock()
    active = 0
    max_active = 0
    entered: list[str] = []

    def fake_run_command(command, **_kwargs):
        nonlocal active, max_active
        with counter_lock:
            active += 1
            max_active = max(max_active, active)
            entered.append(command)
        time.sleep(0.05)
        with counter_lock:
            active -= 1
        return {"command": command}

    monkeypatch.setattr(tasks, "_run_command", fake_run_command)

    results: list[list[dict] | None] = [None, None]

    def worker(idx: int) -> None:
        results[idx] = tasks.run_mr_chain(
            commands=["improve"],
            project_id=34,
            mr_iid=7,
            triggered_by="webhook",
            actor_username="alice",
        )

    threads = [threading.Thread(target=worker, args=(idx,)) for idx in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert max_active == 1, "同 MR 的 chain 必须串行（链锁失效会并发检视）"
    assert len(entered) == 2
    for result in results:
        assert result is not None
        assert [item["status"] for item in result] == ["success"]
    assert fake_store.swept == [(34, 7), (34, 7)]


# ---------------------------------------------------------------------------
# Step 1c: 锁竞争 / TTL / 过期后可重新获取
# ---------------------------------------------------------------------------


def test_chain_lock_contention_ttl_and_expiry(fake_redis):
    from reviewagent.webhook.locks import _CHAIN_LOCK_TTL_SECONDS, MRLockManager

    manager = MRLockManager(cooldown_seconds=60)
    holder = manager.get_lock(34, 7)
    assert holder.acquire(blocking=False) is True

    contender = manager.get_lock(34, 7)
    started = time.monotonic()
    assert contender.acquire(blocking=True, blocking_timeout=0.2) is False
    assert time.monotonic() - started >= 0.2 - 0.05

    # owner 释放后可获取
    assert holder.release() is True
    assert contender.acquire(blocking=True, blocking_timeout=0.2) is True
    assert contender.key == "reviewagent:lock:34:7"

    # TTL 覆盖 job_timeout（崩溃时不永久占锁）
    ttl = fake_redis.ttl(contender.key)
    assert 0 < ttl <= _CHAIN_LOCK_TTL_SECONDS

    # fakeredis 无时钟穿越：删除 key 模拟 TTL 过期（worker SIGKILL 未释放）
    fake_redis.delete(contender.key)
    recovered = manager.get_lock(34, 7)
    assert recovered.acquire(blocking=False) is True
    assert recovered.release() is True


# ---------------------------------------------------------------------------
# Step 1d: 锁等待超时 = fail-closed，且不执行命令
# ---------------------------------------------------------------------------


def test_chain_lock_timeout_is_fail_closed(monkeypatch):
    class _TimeoutLock:
        key = "reviewagent:lock:34:7"

        def acquire(self, **_kwargs) -> bool:
            return False

        def release(self) -> bool:  # pragma: no cover - 未拿到锁时不应释放
            raise AssertionError("release must not run when acquire failed")

    class _LockManager:
        def get_lock(self, project_id: int, mr_iid: int):
            return _TimeoutLock()

    monkeypatch.setattr(tasks, "locks", _LockManager())
    monkeypatch.setattr(tasks, "_verify_job_manifest", lambda **_kwargs: None)
    monkeypatch.setattr(tasks, "get_store", lambda: _FakeStore())

    def _must_not_run(*_args, **_kwargs):  # pragma: no cover - 断言未执行的哨兵
        raise AssertionError("_run_command must not run when the chain lock times out")

    monkeypatch.setattr(tasks, "_run_command", _must_not_run)

    result = tasks.run_mr_chain(
        commands=["improve"],
        project_id=34,
        mr_iid=7,
        triggered_by="webhook",
        actor_username="alice",
    )

    assert result == [{"command": "lock", "status": "failed", "error": "lock_timeout"}]


# ---------------------------------------------------------------------------
# Step 1e: Redis 故障 —— cooldown / diff_head fail-open，链锁 fail-closed
# ---------------------------------------------------------------------------


def _raise_connection_error(_self):
    raise ConnectionError("redis is down")


def test_redis_failure_cooldown_and_diff_head_fail_open(monkeypatch):
    monkeypatch.setattr(
        "reviewagent.webhook.locks.MRLockManager._get_redis",
        _raise_connection_error,
    )
    from reviewagent.webhook.locks import MRLockManager

    manager = MRLockManager(cooldown_seconds=60)
    # fail-open：不跳过（宁可多跑一次串行检视，也不丢事件）
    assert manager.should_skip_cooldown(34, 7, "describe") is False
    assert manager.check_diff_head_changed(34, 7, "head-sha-1") is True


def test_redis_failure_chain_lock_is_fail_closed(monkeypatch):
    from reviewagent.webhook.locks import MRLockManager

    monkeypatch.setattr(
        "reviewagent.webhook.locks.MRLockManager._get_redis",
        _raise_connection_error,
    )
    monkeypatch.setattr(tasks, "locks", MRLockManager(cooldown_seconds=60))
    monkeypatch.setattr(tasks, "_verify_job_manifest", lambda **_kwargs: None)
    monkeypatch.setattr(tasks, "get_store", lambda: _FakeStore())

    def _must_not_run(*_args, **_kwargs):  # pragma: no cover - 断言未执行的哨兵
        raise AssertionError("_run_command must not run without a chain lock")

    monkeypatch.setattr(tasks, "_run_command", _must_not_run)

    with pytest.raises(ConnectionError):
        tasks.run_mr_chain(
            commands=["improve"],
            project_id=34,
            mr_iid=7,
            triggered_by="webhook",
            actor_username="alice",
        )


# ---------------------------------------------------------------------------
# Step 1f: 非 improve 链路同样携带 / 校验运行指纹（T15 carry 缺陷回归）
# ---------------------------------------------------------------------------

_MANIFEST_KEYS = (
    "profile",
    "profile_fingerprint",
    "config_fingerprint",
    "ruleset_fingerprint",
    "dynamic_rules_fingerprint",
)


class _FakeQueue:
    """记录 `q.enqueue(func, **kwargs)` 的假队列（不连 Redis）."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def enqueue(self, func, **kwargs):
        self.calls.append({"func": func, **kwargs})
        return SimpleNamespace(id="job-1")


def test_suggestion_and_weekly_enqueue_carry_manifest_fingerprints(monkeypatch):
    """enqueue_suggestion_action / enqueue_weekly_report 必须注入 5 个扁平指纹字段."""
    from reviewagent.config import current
    from reviewagent.scenario.profile import active_or_load
    from reviewagent.scenario.rules.resolver import active_rule_set
    from reviewagent.settings.binder import fingerprint as binder_fingerprint
    from reviewagent.settings.schema import SCHEMA

    fake_queue = _FakeQueue()
    monkeypatch.setattr(tasks, "get_queue", lambda: fake_queue)
    monkeypatch.setattr(tasks, "get_weekly_queue", lambda: fake_queue)

    tasks.enqueue_suggestion_action(
        action="adopt",
        project_id=34,
        mr_iid=7,
        suggestion_note_id="note-1",
        actor_username="alice",
        reason="fix",
    )
    tasks.enqueue_weekly_report(week_offset=0, push=False)

    assert len(fake_queue.calls) == 2
    profile = active_or_load()
    expected_config = binder_fingerprint(
        {spec.name: getattr(current(), spec.name) for spec in SCHEMA}
    )
    for call in fake_queue.calls:
        assert "manifest" not in call
        for key in _MANIFEST_KEYS:
            assert key in call, f"{call['func']} missing flat kwarg {key}"
        assert call["profile"] == profile.name
        assert call["profile_fingerprint"] == profile.fingerprint
        assert call["config_fingerprint"] == expected_config
        assert call["ruleset_fingerprint"] == active_rule_set().fingerprint


def test_run_suggestion_action_rejects_stale_manifest(monkeypatch):
    """在途 /adopt job 的 profile_fingerprint 与激活环境不一致 → 拒绝且不触碰 process_*."""
    from reviewagent.commands import suggestion_actions
    from reviewagent.config import current
    from reviewagent.scenario.profile import active_or_load
    from reviewagent.settings.binder import fingerprint as binder_fingerprint
    from reviewagent.settings.schema import SCHEMA

    touched: list[str] = []
    monkeypatch.setattr(
        suggestion_actions, "process_adopt", lambda **_kw: touched.append("adopt")
    )
    monkeypatch.setattr(
        suggestion_actions, "process_dismiss", lambda **_kw: touched.append("dismiss")
    )

    profile = active_or_load()
    config_fingerprint = binder_fingerprint(
        {spec.name: getattr(current(), spec.name) for spec in SCHEMA}
    )

    for action in ("adopt", "dismiss"):
        with pytest.raises(RuntimeError, match="fingerprint"):
            tasks.run_suggestion_action(
                action=action,
                project_id=34,
                mr_iid=7,
                suggestion_note_id="note-1",
                actor_username="alice",
                reason="fix",
                profile=profile.name,
                profile_fingerprint="stale-fingerprint",
                config_fingerprint=config_fingerprint,
            )

    assert touched == []


def test_run_weekly_report_rejects_stale_manifest(monkeypatch):
    """在途周报 job 的 profile_fingerprint 不一致 → 拒绝且不运行 run_weekly_job."""
    from reviewagent.config import current
    from reviewagent.reporting import runner as reporting_runner
    from reviewagent.scenario.profile import active_or_load
    from reviewagent.settings.binder import fingerprint as binder_fingerprint
    from reviewagent.settings.schema import SCHEMA

    touched: list[str] = []
    monkeypatch.setattr(
        reporting_runner,
        "run_weekly_job",
        lambda **_kw: touched.append("weekly"),
    )

    profile = active_or_load()
    config_fingerprint = binder_fingerprint(
        {spec.name: getattr(current(), spec.name) for spec in SCHEMA}
    )

    with pytest.raises(RuntimeError, match="fingerprint"):
        tasks.run_weekly_report_job(
            week_offset=0,
            profile=profile.name,
            profile_fingerprint="stale-fingerprint",
            config_fingerprint=config_fingerprint,
        )

    assert touched == []
