from pathlib import Path
from types import SimpleNamespace

import pytest
from rq.worker import SpawnWorker

from reviewagent.workers.rq_worker import ReviewAgentSpawnWorker


ROOT = Path(__file__).resolve().parents[1]
WORKER_CLASS = "reviewagent.workers.rq_worker.ReviewAgentSpawnWorker"


def _make_worker() -> tuple[ReviewAgentSpawnWorker, object, dict[str, object], object]:
    worker = object.__new__(ReviewAgentSpawnWorker)
    connection_kwargs: dict[str, object] = {}
    registry = object()
    connection_kwargs["himport_registry"] = registry
    worker.connection = SimpleNamespace(
        connection_pool=SimpleNamespace(connection_kwargs=connection_kwargs)
    )
    queue = object()
    return worker, queue, connection_kwargs, registry


def test_spawn_worker_omits_himport_registry_during_spawn(monkeypatch):
    worker, queue, connection_kwargs, registry = _make_worker()
    observed_kwargs: dict[str, object] = {}

    def fake_spawn(_worker, _job, _queue):
        observed_kwargs.update(connection_kwargs)
        return "spawned"

    monkeypatch.setattr(SpawnWorker, "fork_work_horse", fake_spawn)

    assert worker.fork_work_horse(object(), queue) == "spawned"
    assert "himport_registry" not in observed_kwargs
    assert connection_kwargs["himport_registry"] is registry


def test_spawn_worker_restores_himport_registry_after_error(monkeypatch):
    worker, queue, connection_kwargs, registry = _make_worker()

    def fail_spawn(_worker, _job, _queue):
        assert "himport_registry" not in connection_kwargs
        raise RuntimeError("spawn failed")

    monkeypatch.setattr(SpawnWorker, "fork_work_horse", fail_spawn)

    with pytest.raises(RuntimeError, match="spawn failed"):
        worker.fork_work_horse(object(), queue)
    assert connection_kwargs["himport_registry"] is registry


@pytest.mark.parametrize("script", ["scripts/restart_local.sh", "scripts/run_worker.sh"])
def test_local_launchers_use_fork_safe_worker(script):
    content = (ROOT / script).read_text(encoding="utf-8")

    assert WORKER_CLASS in content
    assert "OBJC_DISABLE_INITIALIZE_FORK_SAFETY" not in content


@pytest.mark.parametrize("script", ["scripts/run_webhook.sh", "scripts/run_worker.sh"])
def test_local_launchers_load_credentials_from_dotenv(script):
    content = (ROOT / script).read_text(encoding="utf-8")

    assert "source .env" in content
    for variable in (
        "GITLAB_PERSONAL_ACCESS_TOKEN",
        "GITLAB_WEBHOOK_SECRET",
        "OPENCODE_PASSWORD",
    ):
        assert f"export {variable}=" not in content


def test_example_no_qodercli_acp_references():
    """The .env.example must not point at the dead ACP driver."""
    content = (ROOT / ".env.example").read_text(encoding="utf-8")
    for needle in ("QODERCLI_DRIVER", "QODERCLI_ACP_EXTRA_ARGS",
                   "QODERCLI_MAX_CONCURRENT_SESSIONS", "QODERCLI_SESSION_TIMEOUT"):
        assert needle not in content, f"{needle} still in .env.example"


def test_scripts_do_not_reference_removed_acp_module():
    for script in ("scripts/restart_local.sh", "scripts/run_worker.sh"):
        content = (ROOT / script).read_text(encoding="utf-8")
        assert "qodercli_acp" not in content


def test_restart_local_cleans_stale_screen_sessions():
    content = (ROOT / "scripts/restart_local.sh").read_text(encoding="utf-8")

    assert "screen -wipe" in content
    assert "screen -ls 2>/dev/null | grep -q" not in content


def test_restart_local_keeps_services_attached_to_screen():
    content = (ROOT / "scripts/restart_local.sh").read_text(encoding="utf-8")

    assert content.count("| tee") == 1
    assert "terminate_matching" in content
    assert "terminate_worker_jobs" in content


@pytest.mark.parametrize("script", ["scripts/restart_local.sh", "scripts/run_worker.sh"])
def test_worker_launchers_do_not_inherit_terminal_stdin(script):
    content = (ROOT / script).read_text(encoding="utf-8")

    assert content.count("</dev/null") >= 2


# ---------------------------------------------------------------------------
# Task 15: RQ job manifest 校验必须先于任何 store / 命令动作
# ---------------------------------------------------------------------------


def test_run_command_rejects_mismatched_manifest_before_command(monkeypatch):
    """manifest 指纹不一致 → 校验阶段即失败，命令 run() 不会被调用."""
    import reviewagent.commands.improve as improve_module
    from reviewagent.workers import tasks

    touched: list[str] = []

    def fake_get_store():
        touched.append("store")
        raise AssertionError("store must not be touched before manifest verification")

    monkeypatch.setattr(tasks, "get_store", fake_get_store)

    class _SentinelCommand:
        def __init__(self, **_kwargs):
            touched.append("command")

        def run(self):
            touched.append("run")
            return {}

    monkeypatch.setattr(improve_module, "ImproveCommand", _SentinelCommand)

    with pytest.raises(RuntimeError, match="job.profile_mismatch"):
        tasks._run_command(
            "improve",
            project_id=1,
            mr_iid=2,
            triggered_by="webhook",
            actor_username="tester",
            profile="not-the-active-profile",
            profile_fingerprint="deadbeef",
            config_fingerprint="deadbeef",
        )

    assert touched == []


def test_run_command_without_manifest_still_runs(monkeypatch):
    """滚动升级: 老 payload（无指纹字段）→ 只 WARNING，命令照常执行."""
    import reviewagent.commands.improve as improve_module
    from reviewagent.workers import tasks

    called: list[str] = []

    class _FakeStore:
        def sweep_orphaned_runs(self, **_kwargs):
            called.append("sweep")

    class _FakeCommand:
        def __init__(self, **_kwargs):
            called.append("init")

        def run(self):
            called.append("run")
            return {"ok": True}

    monkeypatch.setattr(tasks, "get_store", lambda: _FakeStore())
    monkeypatch.setattr(improve_module, "ImproveCommand", _FakeCommand)

    result = tasks._run_command(
        "improve",
        project_id=1,
        mr_iid=2,
        triggered_by="webhook",
        actor_username="tester",
    )

    assert result == {"ok": True}
    assert called == ["sweep", "init", "run"]
