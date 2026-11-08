"""Task 15: `/readyz` 端点与 SQLite / Redis 运行时探针."""
from __future__ import annotations

import pytest
import redis
from fastapi.testclient import TestClient

from reviewagent import bootstrap
from reviewagent import main as main_module


@pytest.fixture
def client() -> TestClient:
    # 不进入 `with` → 不触发 lifespan，避免依赖真实 profile / data_dir / Redis.
    return TestClient(main_module.app)


# ---------------------------------------------------------------------------
# /readyz
# ---------------------------------------------------------------------------


def test_readyz_returns_ok_when_runtime_ready(client, monkeypatch):
    monkeypatch.setattr(
        main_module.bootstrap, "runtime_readiness", lambda: (True, [])
    )
    monkeypatch.setattr(main_module.bootstrap, "started", lambda: None)

    resp = client.get("/readyz")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_readyz_exposes_active_profile_fingerprint(client, monkeypatch):
    """服务端加载的是哪个场景包内容必须可观测（E2E 预检 / 部署核对都用它）."""
    monkeypatch.setattr(main_module.bootstrap, "runtime_readiness", lambda: (True, []))
    monkeypatch.setattr(
        main_module.bootstrap,
        "started",
        lambda: bootstrap.BootstrapResult(
            "ssd-python-autotest", "abc123", data_dir=__import__("pathlib").Path("data")
        ),
    )

    resp = client.get("/readyz")

    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ok",
        "profile": "ssd-python-autotest",
        "profile_fingerprint": "abc123",
    }


def test_readyz_returns_503_with_reasons_when_unready(client, monkeypatch):
    monkeypatch.setattr(
        main_module.bootstrap,
        "runtime_readiness",
        lambda: (False, ["sqlite: boom", "redis: boom"]),
    )

    resp = client.get("/readyz")

    assert resp.status_code == 503
    assert resp.json() == {
        "status": "unready",
        "reasons": ["sqlite: boom", "redis: boom"],
    }


def test_readyz_awaits_threadpool_offloaded_readiness(client, monkeypatch):
    """探针必须在 threadpool 执行：若退化为事件循环内直调，会记录到 running loop."""
    import asyncio

    observed: list[bool] = []

    def _probe():
        try:
            asyncio.get_running_loop()
            observed.append(True)
        except RuntimeError:
            observed.append(False)
        return (True, [])

    monkeypatch.setattr(main_module.bootstrap, "runtime_readiness", _probe)

    resp = client.get("/readyz")

    assert resp.status_code == 200
    assert observed == [False], "runtime_readiness 必须在 threadpool 执行，不能阻塞事件循环"


# ---------------------------------------------------------------------------
# RuleSet 探针
# ---------------------------------------------------------------------------


def _isolate_rule_set_check(monkeypatch) -> None:
    """只跑 RuleSet 钩子，SQLite / Redis 视为通过（不依赖外部服务）."""
    monkeypatch.setattr(bootstrap, "_readiness_checks", [bootstrap._check_rule_set_active])
    monkeypatch.setattr(bootstrap, "_check_sqlite", lambda: [])
    monkeypatch.setattr(bootstrap, "_check_redis", lambda: [])


def test_default_checks_place_rule_set_after_instance():
    names = [check.__name__ for check in bootstrap._DEFAULT_CHECKS]
    assert names == [
        "_check_config",
        "_check_profile_active",
        "_check_instance_bound",
        "_check_rule_set_active",
    ]


def test_readyz_503_when_rule_set_is_empty(client, monkeypatch):
    from types import SimpleNamespace

    from reviewagent.scenario.rules import resolver

    _isolate_rule_set_check(monkeypatch)
    monkeypatch.setattr(resolver, "active_rule_set", lambda: SimpleNamespace(rules=()))

    resp = client.get("/readyz")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "unready"
    assert any("rule set" in reason for reason in body["reasons"])
    assert any("no rules assembled" in reason for reason in body["reasons"])


def test_readyz_503_when_rule_set_lookup_raises(client, monkeypatch):
    from reviewagent.scenario.rules import resolver

    _isolate_rule_set_check(monkeypatch)

    def boom():
        raise RuntimeError("rule set broken")

    monkeypatch.setattr(resolver, "active_rule_set", boom)

    resp = client.get("/readyz")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "unready"
    assert body["reasons"] == ["rule set: RuntimeError: rule set broken"]


def test_root_endpoint_mentions_readyz(client):
    body = client.get("/").json()
    assert body["endpoints"].get("readyz") == "GET /readyz"


# ---------------------------------------------------------------------------
# SQLite 探针
# ---------------------------------------------------------------------------


def test_sqlite_probe_passes_with_working_store(tmp_path, monkeypatch):
    from reviewagent.telemetry import store as store_module

    monkeypatch.setattr(store_module, "_store", store_module.Store(tmp_path / "probe.db"))

    assert bootstrap._check_sqlite() == []


def test_sqlite_probe_uses_store_ping(monkeypatch):
    """探针必须走 `Store.ping()`（真实 SELECT 1）；退化为 `_conn()` 时本测试失败."""
    from reviewagent.telemetry import store as store_module

    calls: list[str] = []

    class _FakeStore:
        def ping(self):
            calls.append("ping")

        def _conn(self):  # pragma: no cover - 只会在实现退化时被触达
            raise AssertionError("探针不得直接使用 _conn()，应调用 Store.ping()")

    monkeypatch.setattr(store_module, "get_store", lambda: _FakeStore())

    assert bootstrap._check_sqlite() == []
    assert calls == ["ping"]


def test_sqlite_probe_reports_failure(monkeypatch):
    from reviewagent.telemetry import store as store_module

    def boom():
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(store_module, "get_store", boom)

    reasons = bootstrap._check_sqlite()
    assert len(reasons) == 1
    assert reasons[0].startswith("sqlite:")
    assert "db unavailable" in reasons[0]


# ---------------------------------------------------------------------------
# Redis 探针
# ---------------------------------------------------------------------------


def test_redis_probe_passes_with_fake_client(monkeypatch):
    calls: list[dict] = []
    closed: list[bool] = []

    class _FakeRedis:
        def ping(self):
            return True

        def close(self):
            closed.append(True)

    def from_url(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return _FakeRedis()

    monkeypatch.setattr(redis.Redis, "from_url", from_url)

    assert bootstrap._check_redis() == []
    assert calls and calls[0]["socket_connect_timeout"] == 1
    assert calls[0]["socket_timeout"] == 1
    assert closed == [True], "专用探针 client 必须关闭，避免每次 /readyz 泄漏连接"


def test_redis_probe_closes_client_when_ping_fails(monkeypatch):
    closed: list[bool] = []

    class _FakeRedis:
        def ping(self):
            raise ConnectionError("redis down")

        def close(self):
            closed.append(True)

    monkeypatch.setattr(redis.Redis, "from_url", lambda _url, **_kw: _FakeRedis())

    reasons = bootstrap._check_redis()
    assert len(reasons) == 1 and reasons[0].startswith("redis:")
    assert closed == [True]


def test_redis_probe_tolerates_client_without_close(monkeypatch):
    class _FakeRedis:
        def ping(self):
            return True

    monkeypatch.setattr(redis.Redis, "from_url", lambda _url, **_kw: _FakeRedis())

    assert bootstrap._check_redis() == []


def test_redis_probe_reports_failure(monkeypatch):
    def from_url(_url, **_kwargs):
        raise ConnectionError("redis down")

    monkeypatch.setattr(redis.Redis, "from_url", from_url)

    reasons = bootstrap._check_redis()
    assert len(reasons) == 1
    assert reasons[0].startswith("redis:")
    assert "redis down" in reasons[0]


# ---------------------------------------------------------------------------
# runtime_readiness 组合
# ---------------------------------------------------------------------------


def test_runtime_readiness_ok(monkeypatch):
    monkeypatch.setattr(bootstrap, "readiness", lambda: (True, []))
    monkeypatch.setattr(bootstrap, "_check_sqlite", lambda: [])
    monkeypatch.setattr(bootstrap, "_check_redis", lambda: [])

    assert bootstrap.runtime_readiness() == (True, [])


def test_runtime_readiness_includes_sqlite_and_redis_reasons(monkeypatch):
    monkeypatch.setattr(bootstrap, "readiness", lambda: (True, []))
    monkeypatch.setattr(bootstrap, "_check_sqlite", lambda: ["sqlite: boom"])
    monkeypatch.setattr(bootstrap, "_check_redis", lambda: ["redis: boom"])

    ok, reasons = bootstrap.runtime_readiness()
    assert ok is False
    assert any(reason.startswith("sqlite:") for reason in reasons)
    assert any(reason.startswith("redis:") for reason in reasons)


def test_runtime_readiness_keeps_base_reasons(monkeypatch):
    monkeypatch.setattr(bootstrap, "readiness", lambda: (False, ["profile not initialized"]))

    ok, reasons = bootstrap.runtime_readiness()
    assert ok is False
    assert "profile not initialized" in reasons
