"""全量档 preflight：服务加载的场景包必须与磁盘一致（防改完 prompts 忘重启）."""
from __future__ import annotations

import json
from types import SimpleNamespace

from scripts.e2e import stack

_PROFILE = ("ssd-python-autotest", "a" * 64)


def _patch(monkeypatch, readyz_body: dict, *, readyz_status: int = 200) -> None:
    def _http(url: str, **_kwargs):
        if url.endswith("/readyz"):
            return readyz_status, json.dumps(readyz_body).encode()
        return 200, b'{"status":"ok"}'

    monkeypatch.setattr(stack, "_http", _http)
    monkeypatch.setattr(stack, "current_profile", lambda: _PROFILE)
    monkeypatch.setattr(
        stack, "redis_client", lambda: SimpleNamespace(ping=lambda: True)
    )


def _checks() -> dict[str, tuple[bool, str]]:
    return {name: (ok, detail) for name, ok, detail in stack.probe()}


def test_probe_flags_stale_profile_fingerprint(monkeypatch):
    _patch(
        monkeypatch,
        {"status": "ok", "profile": _PROFILE[0], "profile_fingerprint": "b" * 64},
    )

    ok, detail = _checks()["profile.freshness"]

    assert ok is False
    assert "restart_local.sh" in detail


def test_probe_passes_when_service_matches_disk(monkeypatch):
    _patch(
        monkeypatch,
        {"status": "ok", "profile": _PROFILE[0], "profile_fingerprint": _PROFILE[1]},
    )

    checks = _checks()

    assert checks["profile.freshness"][0] is True
    assert all(ok for ok, _ in checks.values())
