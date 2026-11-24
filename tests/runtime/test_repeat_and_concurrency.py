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
    """两线程同 project/mr 跑 run_mr_chain：锁保证命令体最大并发 = 1，两个都 success.

    用 Event 确定性制造重叠（不依赖固定 sleep / 机器速度）：第一个命令体进入后
    阻塞，主线程确认第二个线程已在等链锁（存活且未进入命令体）后才放行.
    """
    from reviewagent.webhook.locks import MRLockManager

    monkeypatch.setattr(tasks, "locks", MRLockManager(cooldown_seconds=60))
    monkeypatch.setattr(tasks, "_verify_job_manifest", lambda **_kwargs: None)
    fake_store = _FakeStore()
    monkeypatch.setattr(tasks, "get_store", lambda: fake_store)

    counter_lock = threading.Lock()
    active = 0
    max_active = 0
    entered: list[str] = []
    entered_event = threading.Event()
    release = threading.Event()
    second_started = threading.Event()

    def fake_run_command(command, **_kwargs):
        nonlocal active, max_active
        with counter_lock:
            active += 1
            max_active = max(max_active, active)
            is_first = not entered
            entered.append(command)
        if is_first:
            entered_event.set()
        # 命令体阻塞到主线程确认第二个线程已在等锁 — 不依赖机器速度的固定 sleep
        assert release.wait(timeout=10), "命令体未被主线程放行（并发用例时序异常）"
        with counter_lock:
            active -= 1
        return {"command": command}

    monkeypatch.setattr(tasks, "_run_command", fake_run_command)

    results: list[list[dict] | None] = [None, None]

    def worker(idx: int) -> None:
        if idx == 1:
            # 在调用 run_mr_chain 之前打点：主线程据此确认第二个线程已启动
            second_started.set()
        results[idx] = tasks.run_mr_chain(
            commands=["improve"],
            project_id=34,
            mr_iid=7,
            triggered_by="webhook",
            actor_username="alice",
        )

    first_thread = threading.Thread(target=worker, args=(0,))
    first_thread.start()
    assert entered_event.wait(timeout=10), "第一个 chain 未进入命令体"

    second_thread = threading.Thread(target=worker, args=(1,))
    second_thread.start()
    assert second_started.wait(timeout=10), "第二个 chain 未启动"
    try:
        # join 短暂确认第二个线程仍存活 = 卡在链锁上（而非已进入命令体）
        second_thread.join(timeout=0.3)
        assert second_thread.is_alive(), "第二个 chain 未在等锁（链锁失效）"
        with counter_lock:
            assert active == 1, "第一个命令体未完成前不得有第二个命令体进入"
            assert max_active == 1
        assert entered == ["improve"], "第二个 chain 在锁释放前不得执行命令"
    finally:
        release.set()

    for thread in (first_thread, second_thread):
        thread.join(timeout=30)
        assert not thread.is_alive()

    assert max_active == 1, "同 MR 的 chain 必须串行（链锁失效会并发检视）"
    assert entered == ["improve", "improve"]
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

        def __init__(self) -> None:
            self.acquire_calls: list[dict] = []

        def acquire(self, **kwargs) -> bool:
            self.acquire_calls.append(kwargs)
            return False

        def release(self) -> bool:  # pragma: no cover - 未拿到锁时不应释放
            raise AssertionError("release must not run when acquire failed")

    timed_out_lock = _TimeoutLock()

    class _LockManager:
        def get_lock(self, project_id: int, mr_iid: int):
            return timed_out_lock

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
    # 链锁语义必须是「阻塞等待 300s」：改成非阻塞 / 其它超时会破坏串行与 fail-closed 契约
    assert timed_out_lock.acquire_calls == [{"blocking": True, "blocking_timeout": 300}]

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

def _active_manifest() -> dict:
    """按入队侧同一入口（`manifest_for_active`）计算当前生效 manifest."""
    from reviewagent.config import config
    from reviewagent.review.manifest import manifest_for_active

    return manifest_for_active(
        cfg=config,
        # 与 tasks._manifest_kwargs 的模型选择保持一致
        model=(
            config.qodercli_model
            if config.llm_provider == "qodercli"
            else config.opencode_model
        ),
        provider=config.llm_provider,
    )

def _manifest_payload(*, profile_fingerprint: str | None = None) -> dict:
    """8 字段 manifest 中入队/校验用的 5 个扁平指纹字段（可篡改单个字段）."""
    manifest = _active_manifest()
    payload = {key: manifest[key] for key in _MANIFEST_KEYS}
    if profile_fingerprint is not None:
        payload["profile_fingerprint"] = profile_fingerprint
    return payload

class _FakeQueue:
    """记录 `q.enqueue(func, **kwargs)` 的假队列（不连 Redis）."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def enqueue(self, func, **kwargs):
        self.calls.append({"func": func, **kwargs})
        return SimpleNamespace(id="job-1")

def test_suggestion_and_weekly_enqueue_carry_manifest_fingerprints(monkeypatch):
    """enqueue_suggestion_action / enqueue_weekly_report 注入的指纹值必须与 manifest 一致."""
    from reviewagent.config import current
    from reviewagent.scenario.dynamic import load_dynamic_rules
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
    # 入队内部走 manifest_for_active；直接调用同一入口比对最稳
    manifest = _active_manifest()
    expected_config = binder_fingerprint(
        {spec.name: getattr(current(), spec.name) for spec in SCHEMA}
    )
    expected_ruleset = active_rule_set().fingerprint
    expected_dynamic = load_dynamic_rules(profile).fingerprint

    for call in fake_queue.calls:
        assert "manifest" not in call
        for key in _MANIFEST_KEYS:
            assert key in call, f"{call['func']} missing flat kwarg {key}"
        # 不只查键存在：值与 manifest / binder / 规则集 / 动态规则逐项一致
        assert call["profile"] == profile.name == manifest["profile"]
        assert (
            call["profile_fingerprint"]
            == profile.fingerprint
            == manifest["profile_fingerprint"]
        )
        assert (
            call["config_fingerprint"]
            == expected_config
            == manifest["config_fingerprint"]
        )
        assert (
            call["ruleset_fingerprint"]
            == expected_ruleset
            == manifest["ruleset_fingerprint"]
        )
        assert (
            call["dynamic_rules_fingerprint"]
            == expected_dynamic
            == manifest["dynamic_rules_fingerprint"]
        )

def test_run_suggestion_action_rejects_stale_manifest(monkeypatch):
    """在途 /adopt job 的 profile_fingerprint 与激活环境不一致 → 拒绝且不触碰 process_*."""
    from reviewagent.commands import suggestion_actions

    touched: list[str] = []
    monkeypatch.setattr(
        suggestion_actions, "process_adopt", lambda **_kw: touched.append("adopt")
    )
    monkeypatch.setattr(
        suggestion_actions, "process_dismiss", lambda **_kw: touched.append("dismiss")
    )

    expected = _manifest_payload(profile_fingerprint="stale-fingerprint")
    real_verify = tasks._verify_job_manifest
    verified: list[dict] = []

    def spy(**kwargs):
        verified.append(dict(kwargs))
        real_verify(**kwargs)

    monkeypatch.setattr(tasks, "_verify_job_manifest", spy)

    for action in ("adopt", "dismiss"):
        with pytest.raises(RuntimeError, match="fingerprint"):
            tasks.run_suggestion_action(
                action=action,
                project_id=34,
                mr_iid=7,
                suggestion_note_id="note-1",
                actor_username="alice",
                reason="fix",
                **expected,
            )

    assert touched == []
    assert len(verified) == 2
    for kwargs in verified:
        # 5 个字段完整透传（仅 profile_fingerprint 被篡改）
        assert set(kwargs) == set(_MANIFEST_KEYS)
        assert kwargs == expected

def test_run_weekly_report_rejects_stale_manifest(monkeypatch):
    """在途周报 job 的 profile_fingerprint 不一致 → 拒绝且不运行 run_weekly_job."""
    from reviewagent.reporting import runner as reporting_runner

    touched: list[str] = []
    monkeypatch.setattr(
        reporting_runner,
        "run_weekly_job",
        lambda **_kw: touched.append("weekly"),
    )

    expected = _manifest_payload(profile_fingerprint="stale-fingerprint")
    real_verify = tasks._verify_job_manifest
    verified: list[dict] = []

    def spy(**kwargs):
        verified.append(dict(kwargs))
        real_verify(**kwargs)

    monkeypatch.setattr(tasks, "_verify_job_manifest", spy)

    with pytest.raises(RuntimeError, match="fingerprint"):
        tasks.run_weekly_report_job(week_offset=0, **expected)

    assert touched == []
    assert len(verified) == 1
    assert set(verified[0]) == set(_MANIFEST_KEYS)
    assert verified[0] == expected

# ---------------------------------------------------------------------------
# 2026-11-24 (MR 1291 生产事故): 同 MR 重复入队 + lock_timeout 被误标孤儿
#
# 事故链:
#   11:27:37 push → 入队 job A;  11:31:08 又 push → 入队 job B
#   A 持 per-MR 锁跑 32 分钟; B 等锁 300s 超时 → 直接 return
#   B 把自己的 running 记录留在库里 → A 跑完释放锁 → C 拿锁 → sweep
#   把 B 那条当"work-horse 被杀"的孤儿标 failed (误导: A 其实活着跑完了)
#
# 两条修法:
#   A) enqueue_mr_chain 入队前去重 —— 同 MR 已有活跃 job 就跳过
#   C) lock_timeout 时主动把自己的 running 标成 skipped (别留给 sweep)
# ---------------------------------------------------------------------------

def test_enqueue_skips_when_same_mr_has_active_chain_job(monkeypatch, fake_redis):
    """同 MR 已有 queued/started 的 chain job → 第二次入队被跳过."""
    monkeypatch.setattr(tasks, "get_redis", lambda: fake_redis)
    monkeypatch.setattr(tasks, "_active_chain_job_for_mr", lambda *a, **k: "job-active")

    enqueued: list[dict] = []

    class _Queue:
        def enqueue(self, *args, **kwargs):
            enqueued.append(kwargs)
            return SimpleNamespace(id="job-new")

    monkeypatch.setattr(tasks, "get_queue", lambda: _Queue())

    job_ids = tasks.enqueue_mr_chain(
        commands=("describe", "improve"),
        project_id=34,
        mr_iid=7,
        triggered_by="webhook",
        actor_username="alice",
    )

    assert job_ids == [], "有活跃 job 时应返回空列表 (跳过)"
    assert not enqueued, "不应该真的入队"

def test_enqueue_proceeds_when_no_active_job(monkeypatch, fake_redis):
    """没有活跃 job → 正常入队, 返回 job_id."""
    monkeypatch.setattr(tasks, "get_redis", lambda: fake_redis)
    monkeypatch.setattr(tasks, "_active_chain_job_for_mr", lambda *a, **k: None)

    enqueued: list[dict] = []

    class _Queue:
        def enqueue(self, *args, **kwargs):
            enqueued.append(kwargs)
            return SimpleNamespace(id="job-new")

    monkeypatch.setattr(tasks, "get_queue", lambda: _Queue())

    job_ids = tasks.enqueue_mr_chain(
        commands=("describe", "improve"),
        project_id=34,
        mr_iid=7,
        triggered_by="webhook",
        actor_username="alice",
    )

    assert job_ids == ["job-new"]
    assert len(enqueued) == 1

def test_enqueue_dedup_check_failure_does_not_block_enqueue(monkeypatch, fake_redis):
    """去重检查本身炸了 → 不能挡住正常入队 (fail-open)."""
    monkeypatch.setattr(tasks, "get_redis", lambda: fake_redis)

    def _boom(*_a, **_k):
        raise RuntimeError("redis down")

    monkeypatch.setattr(tasks, "_active_chain_job_for_mr", _boom)

    class _Queue:
        def enqueue(self, *args, **kwargs):
            return SimpleNamespace(id="job-new")

    monkeypatch.setattr(tasks, "get_queue", lambda: _Queue())

    job_ids = tasks.enqueue_mr_chain(
        commands=("improve",),
        project_id=34,
        mr_iid=7,
        triggered_by="webhook",
        actor_username="alice",
    )

    assert job_ids == ["job-new"], "去重检查失败应 fail-open, 正常入队"

def test_active_chain_job_detects_queued_job():
    """真实 RQ job 在 queued 状态 → 能被查到 (集成级, 用 fakeredis).

    ⚠️ 这里**必须**用 decode_responses=False 的 fakeredis:
       RQ 把 job 存成 pickle 塞在 job hash 里, decode_responses=True 会
       HGETALL 解码失败 (UnicodeDecodeError) → 读不出 job.
       生产 get_redis() 就是默认的 decode_responses=False (二进制安全).
    """
    import fakeredis
    from rq import Queue

    monkeypatch_queue = fakeredis.FakeStrictRedis(decode_responses=False)
    # ⚠️ 队列名要用 config.rq_queue_name (默认 "review") —— 硬编码会查不到
    q = Queue(tasks.config.rq_queue_name, connection=monkeypatch_queue)
    job = q.enqueue(
        "reviewagent.workers.tasks.run_mr_chain",
        commands=["describe", "improve"],
        project_id=34,
        mr_iid=7,
        triggered_by="webhook",
        actor_username="alice",
    )
    assert job.get_status() == "queued"

    found = tasks._active_chain_job_for_mr(34, 7, connection=monkeypatch_queue)
    assert found == job.id, f"应查到 {job.id}, 实际 {found}"

    # 别的 MR 查不到
    assert tasks._active_chain_job_for_mr(34, 8, connection=monkeypatch_queue) is None
    assert tasks._active_chain_job_for_mr(99, 7, connection=monkeypatch_queue) is None

def test_chain_lock_timeout_marks_own_run_as_skipped(telemetry, monkeypatch):
    """锁超时 → 主动把自己的 running 记录标成 skipped (不是 failed/orphaned).

    ⚠️ 这是 C 方案的核心: 之前那条记录会留给 sweep, 被误标成
       "work-horse killed" 的 failed.
    """
    from datetime import UTC, datetime

    from reviewagent.telemetry.models import ReviewRun

    # 造一条 running 记录 (模拟本次 chain 已经建了 run)
    run_id = telemetry.insert_run(
        ReviewRun(
            project_id=34,
            mr_iid=7,
            command="improve",
            triggered_by="webhook",
            actor_username="alice",
            started_at=datetime.now(UTC),
            status="running",
        )
    )

    class _TimeoutLock:
        key = "reviewagent:lock:34:7"

        def acquire(self, **kwargs) -> bool:
            return False

        def release(self) -> bool:  # pragma: no cover
            raise AssertionError("release must not run when acquire failed")

    class _LockManager:
        def get_lock(self, project_id: int, mr_iid: int):
            return _TimeoutLock()

    monkeypatch.setattr(tasks, "locks", _LockManager())
    monkeypatch.setattr(tasks, "_verify_job_manifest", lambda **_kwargs: None)

    result = tasks.run_mr_chain(
        commands=["improve"],
        project_id=34,
        mr_iid=7,
        triggered_by="webhook",
        actor_username="alice",
    )

    assert result == [{"command": "lock", "status": "failed", "error": "lock_timeout"}]

    # 查那条记录: 应是 skipped + 明确的 lock_timeout 原因 (不是 failed/orphaned)
    import sqlite3

    with sqlite3.connect(str(telemetry.path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status, error, finished_at FROM review_runs WHERE id = ?",
            (run_id,),
        ).fetchone()

    assert row is not None
    assert row["status"] == "skipped", f"应是 skipped, 实际 {row['status']}"
    assert "lock_timeout" in (row["error"] or ""), row["error"]
    assert "work-horse" not in (row["error"] or ""), "不该再说是 work-horse 被杀"
    assert row["finished_at"], "应有 finished_at"

def test_mark_lock_timeout_does_not_touch_older_running_run(telemetry):
    """⚠️ 前一个 chain 还活着 → 它的 running 记录不能被标掉.

    store 只处理**最新一条** running —— 更早的是存活 chain 的记录.
    """
    from datetime import UTC, datetime, timedelta

    from reviewagent.telemetry.models import ReviewRun

    now = datetime.now(UTC)
    old_id = telemetry.insert_run(
        ReviewRun(
            project_id=34, mr_iid=7, command="improve",
            triggered_by="webhook", actor_username="alice",
            started_at=now - timedelta(minutes=30), status="running",
        )
    )
    new_id = telemetry.insert_run(
        ReviewRun(
            project_id=34, mr_iid=7, command="improve",
            triggered_by="webhook", actor_username="alice",
            started_at=now, status="running",
        )
    )

    n = telemetry.mark_lock_timeout_runs_for_mr(
        project_id=34, mr_iid=7, wait_seconds=300
    )
    assert n == 1

    import sqlite3

    with sqlite3.connect(str(telemetry.path)) as conn:
        conn.row_factory = sqlite3.Row
        old = conn.execute("SELECT status FROM review_runs WHERE id = ?",
                           (old_id,)).fetchone()
        new = conn.execute("SELECT status, error FROM review_runs WHERE id = ?",
                           (new_id,)).fetchone()

    assert old["status"] == "running", "存活 chain 的记录不能被标掉"
    assert new["status"] == "skipped"
    assert "lock_timeout" in (new["error"] or "")

def test_sweep_no_longer_catches_lock_timeout_records(telemetry):
    """回归锁: 主动标记后, sweep 不该再把它当孤儿标 failed."""
    from datetime import UTC, datetime

    from reviewagent.telemetry.models import ReviewRun

    run_id = telemetry.insert_run(
        ReviewRun(
            project_id=34, mr_iid=7, command="improve",
            triggered_by="webhook", actor_username="alice",
            started_at=datetime.now(UTC), status="running",
        )
    )
    # 先主动标记 (lock_timeout 路径)
    telemetry.mark_lock_timeout_runs_for_mr(project_id=34, mr_iid=7, wait_seconds=300)
    # 再 sweep —— 不该再动它
    swept = telemetry.sweep_orphaned_runs_for_mr(project_id=34, mr_iid=7)
    assert swept == 0

    import sqlite3

    with sqlite3.connect(str(telemetry.path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT status, error FROM review_runs WHERE id = ?",
                           (run_id,)).fetchone()

    assert row["status"] == "skipped"
    assert "work-horse" not in (row["error"] or "")
