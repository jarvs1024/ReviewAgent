"""Unit tests for /adopt and /dismiss handlers."""
from __future__ import annotations

import pytest

from reviewagent.commands.suggestion_actions import (
    _target_region_changed,
    extract_action,
)


# ---------- extract_action ----------

def test_extract_dismiss_with_reason():
    action, reason = extract_action("/dismiss 误报")
    assert action == "dismiss"
    assert reason == "误报"


def test_extract_dismiss_no_reason():
    action, reason = extract_action("/dismiss")
    assert action == "dismiss"
    assert reason == ""


def test_extract_dismiss_no_slash():
    action, reason = extract_action("dismiss 忽略")
    assert action == "dismiss"
    assert reason == "忽略"


def test_extract_adopt_with_reason():
    action, reason = extract_action("/adopt 手动改了")
    assert action == "adopt"
    assert reason == "手动改了"


def test_extract_adopt_takes_priority_over_dismiss():
    # "/adopt 用 dismiss 风格重写" — adopt wins
    action, reason = extract_action("/adopt 用 dismiss 风格重写")
    assert action == "adopt"


def test_extract_no_command():
    assert extract_action("just a normal comment") is None
    assert extract_action("") is None


def test_extract_dismisses_inside_word_ignored():
    # "dismissed" 内嵌的 dismiss 不算 (前后是字母数字)
    assert extract_action("the state was dismissed") is None


# ---------- _target_region_changed ----------

def test_target_changed_true():
    """目标行确实被修改."""
    posted = "def f():\n    return open(p).read()\n"
    current = "def f():\n    with open(p) as f:\n        return f.read()\n"
    assert _target_region_changed(posted, current, line=2, line_end=2) is True


def test_target_changed_false_when_unchanged():
    """目标行没动."""
    posted = "def f():\n    return open(p).read()\n"
    current = "def f():\n    return open(p).read()\n"
    assert _target_region_changed(posted, current, line=2, line_end=2) is False


def test_target_changed_false_other_lines_changed():
    """其他行改了, 目标行没动."""
    posted = "def f():\n    return open(p).read()\n\ndef g(): pass\n"
    current = "def f():\n    return open(p).read()\n\ndef g():\n    return 1\n"
    assert _target_region_changed(posted, current, line=2, line_end=2) is False


def test_target_changed_empty_content():
    """空内容返回 False (无法判断)."""
    assert _target_region_changed("", "x", line=1, line_end=1) is False
    assert _target_region_changed("x", "", line=1, line_end=1) is False


def test_target_changed_multiline_region():
    """多行目标区段被替换."""
    posted = "def f():\n    x = 1\n    y = 2\n    return x + y\n"
    current = "def f():\n    x = 10\n    y = 20\n    return x + y\n"
    assert _target_region_changed(posted, current, line=2, line_end=3) is True


# ---------- _maybe_enqueue_reimprove ----------

class _FakeLockMgr:
    """minimal locks shim — flip return value per call."""
    def __init__(self, skip: bool = False):
        self.skip = skip
        self.calls = 0
    def should_skip_cooldown(self, *a, **kw):
        self.calls += 1
        return self.skip


class _FakeEnqueue:
    """mock enqueue_improve — captures kwargs, returns fake job id."""
    def __init__(self, fail: bool = False):
        self.calls: list[tuple[int, int, str, str]] = []
        self.fail = fail
        self._counter = 0
    def __call__(self, *, project_id, mr_iid, triggered_by, actor_username):
        self.calls.append((project_id, mr_iid, triggered_by, actor_username))
        if self.fail:
            raise RuntimeError("simulated redis down")
        self._counter += 1
        return f"job-{self._counter}"


def test_reimprove_enqueue_success(monkeypatch):
    """Successful enqueue returns job_id, lock + enqueue both called."""
    fake_enq = _FakeEnqueue()
    fake_lock = _FakeLockMgr(skip=False)
    # patch the lazy imports inside _maybe_enqueue_reimprove via sys.modules is overkill —
    # instead patch the function-local import by stuffing into the module's namespace.
    import reviewagent.commands.suggestion_actions as sa
    # Add fake submodules so the inner `from reviewagent.workers.tasks import enqueue_improve`
    # succeeds:
    import types, sys
    fake_tasks = types.ModuleType("reviewagent.workers.tasks")
    fake_tasks.enqueue_improve = fake_enq  # type: ignore[attr-defined]
    sys.modules["reviewagent.workers.tasks"] = fake_tasks
    fake_locks_mod = types.ModuleType("reviewagent.webhook.locks")
    fake_locks_mod.locks = fake_lock  # type: ignore[attr-defined]
    sys.modules["reviewagent.webhook.locks"] = fake_locks_mod
    # also need the real module importable for `from reviewagent.webhook.locks` etc.
    # The function imports via `from reviewagent.workers.tasks import enqueue_improve`,
    # which uses sys.modules; that works because sys.modules[name] is returned.
    job = sa._maybe_enqueue_reimprove(
        project_id=34, mr_iid=138, actor_username="jarvs"
    )
    assert job == "job-1"
    assert fake_lock.calls == 1
    assert fake_enq.calls == [(34, 138, "adopt", "jarvs")]


def test_reimprove_enqueued_returns_quoted_job_id():
    fake_enq = _FakeEnqueue()
    fake_lock = _FakeLockMgr(skip=False)
    import reviewagent.commands.suggestion_actions as sa
    import types, sys
    sys.modules["reviewagent.workers.tasks"] = types.ModuleType("reviewagent.workers.tasks")
    sys.modules["reviewagent.workers.tasks"].enqueue_improve = fake_enq  # type: ignore[attr-defined]
    sys.modules["reviewagent.webhook.locks"] = types.ModuleType("reviewagent.webhook.locks")
    sys.modules["reviewagent.webhook.locks"].locks = fake_lock  # type: ignore[attr-defined]
    job_id = sa._maybe_enqueue_reimprove(project_id=1, mr_iid=2, actor_username="x")
    assert job_id and job_id.startswith("job-")


def test_reimprove_skip_cooldown(monkeypatch):
    """When cooldown is active, function returns None without enqueue."""
    fake_enq = _FakeEnqueue()
    fake_lock = _FakeLockMgr(skip=True)
    import reviewagent.commands.suggestion_actions as sa
    import types, sys
    sys.modules["reviewagent.workers.tasks"] = types.ModuleType("reviewagent.workers.tasks")
    sys.modules["reviewagent.workers.tasks"].enqueue_improve = fake_enq  # type: ignore[attr-defined]
    sys.modules["reviewagent.webhook.locks"] = types.ModuleType("reviewagent.webhook.locks")
    sys.modules["reviewagent.webhook.locks"].locks = fake_lock  # type: ignore[attr-defined]
    job = sa._maybe_enqueue_reimprove(project_id=34, mr_iid=138, actor_username="jarvs")
    assert job is None
    assert fake_enq.calls == []  # cooldown blocked
    assert fake_lock.calls == 1


def test_reimprove_enqueue_failure_is_swallowed(monkeypatch):
    """Enqueue raising should be logged + return None, not propagate."""
    fake_enq = _FakeEnqueue(fail=True)
    fake_lock = _FakeLockMgr(skip=False)
    import reviewagent.commands.suggestion_actions as sa
    import types, sys
    sys.modules["reviewagent.workers.tasks"] = types.ModuleType("reviewagent.workers.tasks")
    sys.modules["reviewagent.workers.tasks"].enqueue_improve = fake_enq  # type: ignore[attr-defined]
    sys.modules["reviewagent.webhook.locks"] = types.ModuleType("reviewagent.webhook.locks")
    sys.modules["reviewagent.webhook.locks"].locks = fake_lock  # type: ignore[attr-defined]
    job = sa._maybe_enqueue_reimprove(project_id=34, mr_iid=138, actor_username="jarvs")
    assert job is None
    assert fake_enq.calls == [(34, 138, "adopt", "jarvs")]
