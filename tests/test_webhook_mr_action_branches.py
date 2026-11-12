"""E10: MR 事件的 action 分支覆盖 —— close / merge / draft / 其他 action.

Why 需要这份测试:
`_handle_code_change` 里按 `mr.action` 分了 4 条路:
    update / open|reopen / draft(独立前置判断) / 其余 action(else 分支)
但此前测试只覆盖了 open / reopen / update 三条（tests/test_webhook_diff_head_lock.py），
**close / merge / draft / 未知 action 全无覆盖** —— 这几条恰恰是「不该触发检视」的分支，
一旦被改坏（比如 close 误触发 review、draft 漏跳过）不会有任何测试变红。

真实业务含义:
    - close / merge: MR 已经结束，**不得**再触发任何检视命令，
      但 MR 元信息（state/merged_at）必须落库（后续 dashboard 依赖）。
    - draft: 草稿 MR 等用户标 Ready，**不得**触发检视。
    - 未知 action（如 approval / approved）: 保守跳过。

同时断言 skip 分支的 **metrics reason** 与返回体里的 reason 一致，
避免 telemetry 里 reason 归类错位导致监控看错。
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from reviewagent.webhook import locks as locks_mod
from reviewagent.webhook.router import _handle_code_change


class _FakeLocks:
    """记录 diff_head 检查调用; 恒返回「有变化」以便走到 action 分派之后."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, int, str]] = []

    def is_bot(self, _username: str) -> bool:
        return False

    def check_diff_head_changed(self, project_id: int, mr_iid: int, head_sha: str) -> bool:
        self.calls.append((project_id, mr_iid, head_sha))
        return True

    def should_skip_max_review_calls(self, *a, **kw) -> tuple[bool, int]:
        return False, 0

    def should_skip_cooldown(self, *a, **kw) -> bool:
        return False

    def claim_cooldown(self, *a, **kw) -> None:
        return None

    def try_lock(self, *a, **kw) -> bool:
        return True


def _payload(
    *,
    action: str,
    state: str = "opened",
    draft: bool = False,
    head_sha: str = "aaa111",
) -> dict[str, Any]:
    return {
        "object_kind": "merge_request",
        "event_type": "merge_request",
        "project": {"id": 34},
        "user": {"username": "human-tester@root", "name": "Human"},
        "object_attributes": {
            "iid": 211,
            "action": action,
            "state": state,
            "title": "feat: x",
            "source_branch": "feat",
            "target_branch": "main",
            "draft": draft,
            "last_commit": {"id": head_sha},
            "diff_refs": {"head_sha": head_sha},
        },
    }


@pytest.fixture(autouse=True)
def _no_mr_author_lookup(monkeypatch) -> None:
    """单测不打 GitLab API."""
    import reviewagent.webhook.router as router_mod

    monkeypatch.setattr(router_mod, "_mr_record_from_api", lambda mr: None)


@pytest.fixture
def locks(monkeypatch) -> _FakeLocks:
    fake = _FakeLocks()
    import reviewagent.webhook.router as router_mod

    monkeypatch.setattr(router_mod, "locks", fake)
    return fake


# ---------------------------------------------------------------------------
# close / merge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["close", "merge"])
async def test_close_and_merge_actions_never_trigger_review(locks, action) -> None:
    """close / merge action 绝不能 enqueue 任何检视命令."""
    enq = MagicMock(return_value=["job-1"])
    res = await _handle_code_change(
        _payload(action=action, state="opened"),
        "merge_request",
        enq,
    )
    assert res["status"] == "skipped", res
    assert not enq.called, f"{action} action 不得触发检视, res={res}"
    assert action in res.get("reason", ""), (
        f"reason 必须点名 action ({action}), 实际={res.get('reason')!r}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["closed", "merged"])
async def test_non_opened_state_skips_review(locks, state) -> None:
    """MR state 不是 opened → 不触发检视（close/merge 事件的常规形态）."""
    enq = MagicMock(return_value=["job-1"])
    res = await _handle_code_change(
        _payload(action="update", state=state),
        "merge_request",
        enq,
    )
    assert res["status"] == "skipped", res
    assert not enq.called, f"state={state} 不得触发检视, res={res}"
    assert res.get("reason") == f"state={state}", res


@pytest.mark.asyncio
async def test_close_action_still_upserts_mr_metadata(locks) -> None:
    """close / merge 虽不触发检视, 但 MR 元信息必须落库（dashboard 依赖 state）."""
    from reviewagent.telemetry.store import get_store

    enq = MagicMock(return_value=["job-1"])
    await _handle_code_change(
        _payload(action="close", state="closed"),
        "merge_request",
        enq,
    )
    row = get_store().get_mr(34, 211)
    assert row is not None, "close 事件必须 upsert MR 记录"
    assert row["state"] == "closed", row


# ---------------------------------------------------------------------------
# draft
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_draft_mr_never_triggers_review(locks) -> None:
    """草稿 MR 不触发检视（等用户标 Ready）."""
    enq = MagicMock(return_value=["job-1"])
    res = await _handle_code_change(
        _payload(action="open", draft=True),
        "merge_request",
        enq,
    )
    assert res["status"] == "skipped", res
    assert res.get("reason") == "draft_mr", res
    assert not enq.called, f"草稿 MR 不得触发检视, res={res}"


@pytest.mark.asyncio
async def test_draft_check_precedes_action_dispatch(locks) -> None:
    """草稿判断必须早于 diff_head 锁 —— 否则草稿 MR 会污染 diff_head 锁."""
    enq = MagicMock(return_value=["job-1"])
    await _handle_code_change(
        _payload(action="open", draft=True),
        "merge_request",
        enq,
    )
    assert locks.calls == [], (
        f"草稿 MR 不该写 diff_head 锁, 实际 calls={locks.calls}"
    )


@pytest.mark.asyncio
async def test_non_draft_mr_does_write_diff_head_lock(locks) -> None:
    """对照组: 非草稿 MR 照常写锁 —— 证明上一条不是因为锁逻辑没跑到."""
    enq = MagicMock(return_value=["job-1"])
    await _handle_code_change(
        _payload(action="open", draft=False),
        "merge_request",
        enq,
    )
    assert locks.calls == [(34, 211, "aaa111")], locks.calls


# ---------------------------------------------------------------------------
# 未知 action
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["approved", "approval", "unapproved", "approved"])
async def test_unknown_action_skips_review(locks, action) -> None:
    """未知 action 保守跳过, 不得触发检视."""
    enq = MagicMock(return_value=["job-1"])
    res = await _handle_code_change(
        _payload(action=action),
        "merge_request",
        enq,
    )
    assert res["status"] == "skipped", res
    assert not enq.called, f"未知 action {action} 不得触发检视, res={res}"
    assert action in res.get("reason", ""), res


# ---------------------------------------------------------------------------
# bot 自触发
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bot_actor_is_skipped_before_state_check(monkeypatch) -> None:
    """bot 自己触发的事件（如 E2E 机器人 push）→ 直接跳过, 不查锁也不排命令."""
    import reviewagent.webhook.router as router_mod

    class _BotLocks(_FakeLocks):
        def is_bot(self, username: str) -> bool:
            return True

    fake = _BotLocks()
    monkeypatch.setattr(router_mod, "locks", fake)

    enq = MagicMock(return_value=["job-1"])
    res = await _handle_code_change(
        _payload(action="update"),
        "merge_request",
        enq,
    )
    assert res["status"] == "skipped", res
    assert res.get("reason") == "bot_self_trigger", res
    assert not enq.called, res
    assert fake.calls == [], "bot 自触发不该写 diff_head 锁"
