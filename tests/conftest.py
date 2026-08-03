"""Pytest conftest — set baseline env so importing reviewagent.config succeeds.

`config = Config.from_env()` runs at module import time, which means it runs
before any test fixture can patch env. We set sensible defaults at import
time here; tests that need different values should use `monkeypatch.setenv`
inside the test body (monkeypatch will tear them down at fixture end).
"""
from __future__ import annotations

import os

_BASELINE = {
    "GITLAB_URL": "https://gitlab.example.test",
    "GITLAB_PERSONAL_ACCESS_TOKEN": "test-token",
    "GITLAB_WEBHOOK_SECRET": "test-secret",
}

for _key, _value in _BASELINE.items():
    os.environ.setdefault(_key, _value)


import pytest  # noqa: E402  (must come after env baseline)


@pytest.fixture(autouse=True)
def _baseline_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-assert baseline before each test so test order does not matter."""
    for _key, _value in _BASELINE.items():
        monkeypatch.setenv(_key, _value)
