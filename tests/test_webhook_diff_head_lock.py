"""Webhook router: open / reopen action 必须写 diff_head 锁，防止后续 update 重投递.

背景 bug (2026-08-05):
之前 `open` action 完全不调用 `check_diff_head_changed()`，导致：
- MR 首次打开 → enqueue chain #1，**但 Redis diff_head 仍是空**
- 60+ 秒后 GitLab 推送一个 secondary 事件（自动重发 / 用户操作），action=update
  → check_diff_head_changed 看到 prev=None → 返回 True → 重复 enqueue chain #2

修复：open / reopen 也走 check_diff_head_changed，首次见 SHA → 写锁；同 SHA 重放 → 跳过
"""

from __future__ import annotations

import importlib
from typing import Any
from unittest.mock import MagicMock

import pytest

from reviewagent.webhook import locks as locks_mod
from reviewagent.webhook.router import _handle_code_change


class _FakeLocks:
    """记录每次 check_diff_head_changed 的调用，看 open 路径有没有调它."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, int, str]] = []
        # 模拟 Redis 状态: {key: sha}
        self._store: dict[tuple[int, int], str] = {}

    def is_bot(self, _username: str) -> bool:  # noqa: D401
        return False

    def check_diff_head_changed(self, project_id: int, mr_iid: int, head_sha: str) -> bool:
        self.calls.append((project_id, mr_iid, head_sha))
        key = (project_id, mr_iid)
        prev = self._store.get(key)
        if prev is None:
            self._store[key] = head_sha
            return True  # 首次 → True
        if prev == head_sha:
            return False  # 同 SHA → False
        self._store[key] = head_sha
        return True  # 新 SHA → True

    def should_skip_max_review_calls(self, *a, **kw) -> tuple[bool, int]:
        return False, 0

    def should_skip_cooldown(self, *a, **kw) -> bool:
        return False

    def claim_cooldown(self, *a, **kw) -> None:
        return None

    def try_lock(self, *a, **kw) -> bool:
        return True


def _make_payload(*, action: str, head_sha: str) -> dict[str, Any]:
    return {
        "object_kind": "merge_request",
        "event_type": "merge_request",
        "project": {"id": 34},
        "object_attributes": {
            "iid": 211,
            "action": action,
            "state": "opened",
            "source_branch": "feat",
            "target_branch": "main",
            "last_commit": {"id": head_sha},
            "head_sha": head_sha,
            "author_id": 40,
            "user": {"username": "human-tester@root"},
        },
    }


@pytest.fixture
def locks(monkeypatch) -> _FakeLocks:
    fake = _FakeLocks()
    # 替换 module-level singleton
    import reviewagent.webhook.locks as _locks_mod
    # _handle_code_change 通过 `from . import locks` 拿到引用
    # router.py 写法: `from reviewagent.webhook.locks import locks`
    # 所以 router 命名空间里的 `locks` 已经绑定到导入时的那个 singleton.
    # monkeypatch setattr 在 router 命名空间上替换这个 binding.
    import reviewagent.webhook.router as router_mod
    monkeypatch.setattr(router_mod, "locks", fake)
    return fake


@pytest.mark.asyncio
async def test_open_action_writes_diff_head_lock(locks: _FakeLocks) -> None:
    """open action 必须调用 check_diff_head_changed（修复后行为）."""
    fake_enqueue = MagicMock(return_value=["job-1"])
    res = await _handle_code_change(
        _make_payload(action="open", head_sha="aaa111"),
        "merge_request",
        fake_enqueue,
    )
    assert locks.calls == [(34, 211, "aaa111")], \
        f"open action 应触发 diff_head 检查, 实际 calls={locks.calls}"
    assert fake_enqueue.called, "open 首次应 enqueue"


@pytest.mark.asyncio
async def test_open_replay_same_sha_skipped(locks: _FakeLocks) -> MagicMock:
    """open 重放同 SHA 必须被 check_diff_head_changed short-circuit."""
    # 首次开 MR
    enq1 = MagicMock(return_value=["job-1"])
    await _handle_code_change(
        _make_payload(action="open", head_sha="aaa111"),
        "merge_request", enq1,
    )
    assert enq1.called

    # 同 SHA 重放 → 应当 skip
    enq2 = MagicMock(return_value=["job-2"])
    res = await _handle_code_change(
        _make_payload(action="open", head_sha="aaa111"),
        "merge_request", enq2,
    )
    assert not enq2.called, "open 重放同 SHA 必须跳过"


@pytest.mark.asyncio
async def test_open_then_update_should_not_duplicate(locks: _FakeLocks) -> MagicMock:
    """核心修复 bug: open 之后 update 同 SHA 必须 skip（修复前会 duplicate enqueue）."""
    enq1 = MagicMock(return_value=["job-1"])
    await _handle_code_change(
        _make_payload(action="open", head_sha="aaa111"),
        "merge_request", enq1,
    )
    assert enq1.call_count == 1

    # GitLab 推送 secondary 事件，action=update，**同 SHA** — 这是本次 bug 触发场景
    enq2 = MagicMock(return_value=["job-2"])
    res = await _handle_code_change(
        _make_payload(action="update", head_sha="aaa111"),
        "merge_request", enq2,
    )
    assert not enq2.called, f"open→update 同 SHA 必须 skip, res={res}"
    assert res["status"] == "skipped", res
    assert "no new commit" in res.get("reason", ""), res


@pytest.mark.asyncio
async def test_open_then_update_new_sha_triggers(locks: _FakeLocks) -> MagicMock:
    """open 之后 update **新 SHA**（真实 push）必须 enqueue."""
    enq1 = MagicMock(return_value=["job-1"])
    await _handle_code_change(
        _make_payload(action="open", head_sha="aaa111"),
        "merge_request", enq1,
    )

    enq2 = MagicMock(return_value=["job-2"])
    res = await _handle_code_change(
        _make_payload(action="update", head_sha="bbb222"),
        "merge_request", enq2,
    )
    assert enq2.called, f"push 新 commit 必须 enqueue, res={res}"
