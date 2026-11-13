"""note 触发的单命令必须持 per-MR 链锁（N9 回归）。

Why:
链式路径 (`run_mr_chain`) 已经持 per-MR 锁再跑 describe/improve；
但 note 路径 (`/describe` / `/improve` 评论) 走的是 RQ 单命令入口
`run_describe` / `run_improve` → `_run_command` —— **不取锁**。

后果（N9 实测，E2E 全量档 `telemetry:mr_activity` 审计报红）:
* cooldown 只挡「入队」，不挡「执行」—— 排队中的 note 任务会与链式任务**重叠跑**；
* 两个 describe 并发执行时，「GitLab 标题」与「telemetry 标题」的
  **最后写入者可能不同**（一个写 GitLab 后还没回写 telemetry，另一个就插进来），
  于是 telemetry 与 GitLab 的 MR 标题**永久漂移**（dashboard 显示旧标题）。

修法: 单命令入口也取同一把 per-MR 锁 → 同一 MR 的命令**执行**串行，
两个 sink 的「最后写入者」必然一致。

测试只跑纯函数级逻辑（fake lock + monkeypatch `_run_command`），
**不依赖谁在跑 / 在什么 shell 里跑**，也不连真实 Redis / GitLab。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest


class _FakeLock:
    """记录 acquire/release 时序，并暴露「当前是否被持有」."""

    def __init__(self, *, acquire_ok: bool = True) -> None:
        self.acquire_ok = acquire_ok
        self.held = False
        self.events: list[str] = []

    def acquire(self, **_kwargs) -> bool:
        self.events.append("acquire")
        if not self.acquire_ok:
            return False
        self.held = True
        return True

    def release(self) -> None:
        self.events.append("release")
        self.held = False


class _FakeLocks:
    def __init__(self, lock: _FakeLock) -> None:
        self.lock = lock
        self.calls: list[tuple[int, int]] = []

    def get_lock(self, project_id: int, mr_iid: int) -> _FakeLock:
        self.calls.append((project_id, mr_iid))
        return self.lock


@pytest.fixture
def note_env(monkeypatch):
    """装好 fake lock + fake `_run_command`，返回观察容器."""
    import reviewagent.workers.tasks as tasks

    lock = _FakeLock()
    fake_locks = _FakeLocks(lock)
    observed: dict[str, object] = {"lock": lock, "locks": fake_locks, "ran": []}

    def fake_run_command(command, **kw):
        observed["ran"].append((command, kw))
        observed["held_during_run"] = lock.held
        return {"status": "success"}

    monkeypatch.setattr(tasks, "locks", fake_locks)
    monkeypatch.setattr(tasks, "_run_command", fake_run_command)
    monkeypatch.setattr(
        tasks, "_verify_job_manifest", lambda **kw: None, raising=False
    )
    return observed


def test_note_describe_holds_per_mr_lock_during_execution(note_env) -> None:
    import reviewagent.workers.tasks as tasks

    tasks.run_describe(
        project_id=56,
        mr_iid=1,
        triggered_by="note",
        actor_username="reviewer",
    )

    assert note_env["held_during_run"] is True, "命令执行期间必须持 per-MR 锁"
    assert note_env["lock"].events == ["acquire", "release"], note_env["lock"].events
    assert note_env["locks"].calls == [(56, 1)]


def test_note_improve_holds_per_mr_lock_during_execution(note_env) -> None:
    import reviewagent.workers.tasks as tasks

    tasks.run_improve(
        project_id=56,
        mr_iid=1,
        triggered_by="note",
        actor_username="reviewer",
    )

    assert note_env["held_during_run"] is True
    assert note_env["lock"].events == ["acquire", "release"]


def test_lock_timeout_must_not_run_the_command(monkeypatch) -> None:
    """拿不到锁 → 绝不能「裸跑」（否则又回到并发写标题的老路）."""
    import reviewagent.workers.tasks as tasks

    lock = _FakeLock(acquire_ok=False)
    ran: list[str] = []
    monkeypatch.setattr(tasks, "locks", _FakeLocks(lock))
    monkeypatch.setattr(tasks, "_run_command", lambda *a, **kw: ran.append("run"))

    with pytest.raises(Exception):
        tasks.run_describe(
            project_id=56,
            mr_iid=1,
            triggered_by="note",
            actor_username="reviewer",
        )

    assert ran == [], "拿不到锁时命令必须被拒绝执行"
    assert "release" not in lock.events, "没拿到锁就不能 release"


def test_lock_is_released_when_command_raises(monkeypatch) -> None:
    import reviewagent.workers.tasks as tasks

    lock = _FakeLock()
    monkeypatch.setattr(tasks, "locks", _FakeLocks(lock))

    def boom(*a, **kw):
        raise RuntimeError("command failed")

    monkeypatch.setattr(tasks, "_run_command", boom)

    with pytest.raises(RuntimeError, match="command failed"):
        tasks.run_describe(
            project_id=56,
            mr_iid=1,
            triggered_by="note",
            actor_username="reviewer",
        )

    assert lock.events == ["acquire", "release"], lock.events


def test_chain_path_acquires_lock_exactly_once(monkeypatch) -> None:
    """链式路径已持锁 —— 单命令入口不能再取一次（同一把锁不可重入，会自锁）."""
    import reviewagent.workers.tasks as tasks

    lock = _FakeLock()
    fake_locks = _FakeLocks(lock)
    ran: list[str] = []
    monkeypatch.setattr(tasks, "locks", fake_locks)
    monkeypatch.setattr(tasks, "get_store", lambda: SimpleNamespace(
        sweep_orphaned_runs_for_mr=lambda **kw: 0,
    ))
    monkeypatch.setattr(tasks, "_run_command", lambda *a, **kw: ran.append("run"))

    tasks.run_mr_chain(
        commands=["describe"],
        project_id=56,
        mr_iid=1,
        triggered_by="webhook",
        actor_username="reviewer",
    )

    assert len(fake_locks.calls) == 1, f"链式路径只能取一次锁，实际 {fake_locks.calls}"
    assert ran == ["run"]
