"""Pytest conftest — set baseline env so importing reviewagent.config succeeds.

`config = Config.from_env()` runs at module import time, which means it runs
before any test fixture can patch env. We set sensible defaults at import
time here; tests that need different values should use `monkeypatch.setenv`
inside the test body (monkeypatch will tear them down at fixture end).
"""
from __future__ import annotations

import os
from collections.abc import Iterator

_BASELINE = {
    "GITLAB_URL": "https://gitlab.example.test",
    "GITLAB_PERSONAL_ACCESS_TOKEN": "test-token",
    "GITLAB_WEBHOOK_SECRET": "test-secret",
    "QODERCLI_NODE_PATH": "/usr/bin/node",
    "QODERCLI_JS_PATH": "/opt/qodercli.js",
    "QODERCLI_MODEL": "DeepSeek-V4-Flash",
}

for _key, _value in _BASELINE.items():
    os.environ.setdefault(_key, _value)


import pytest  # noqa: E402  (must come after env baseline)


@pytest.fixture(autouse=True)
def _baseline_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-assert baseline before each test so test order does not matter."""
    for _key, _value in _BASELINE.items():
        monkeypatch.setenv(_key, _value)


@pytest.fixture(autouse=True)
def _restore_config_snapshots() -> Iterator[None]:
    """Snapshot/restore binder's explicit-env and the global current config.

    Both are module-level snapshots refreshed by config parsing (`from_env`)
    and scenario application (`apply_scenario`); monkeypatch removing env
    cannot roll them back, so restoring them keeps test ordering irrelevant.
    """
    # Imported lazily: reviewagent.config parses env at import time, so the
    # baseline above must be in place first.
    from reviewagent import config as config_module
    from reviewagent.settings import binder

    saved_explicit = binder._EXPLICIT_ENV
    saved_current = config_module._current
    yield
    binder._EXPLICIT_ENV = saved_explicit
    config_module._current = saved_current
