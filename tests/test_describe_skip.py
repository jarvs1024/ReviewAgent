"""/describe 一次性守卫: 显式 note 命令允许覆盖, 自动链仍只改一次."""
from __future__ import annotations

from unittest.mock import patch

import pytest


class _FakeStore:
    def __init__(self, generated: int):
        self._generated = generated

    def get_mr(self, project_id, mr_iid):
        return {"description_generated": self._generated}


@pytest.fixture
def make_cmd(monkeypatch):
    def _make(*, triggered_by: str, generated: int):
        from reviewagent.commands.describe import DescribeCommand
        from reviewagent.telemetry import store as st

        monkeypatch.setattr(st, "get_store", lambda: _FakeStore(generated))
        with patch("reviewagent.commands._common.GitLabClient"):
            return DescribeCommand(
                project_id=1, mr_iid=2, triggered_by=triggered_by,
            )

    return _make


def test_explicit_note_describe_bypasses_generated_flag(make_cmd):
    cmd = make_cmd(triggered_by="note", generated=1)
    assert cmd._should_skip({}) is None


def test_auto_describe_skips_after_generated(make_cmd):
    cmd = make_cmd(triggered_by="webhook", generated=1)
    result = cmd._should_skip({})
    assert result is not None
    assert result["reason"] == "already_described"


def test_auto_describe_runs_when_never_generated(make_cmd):
    cmd = make_cmd(triggered_by="push", generated=0)
    assert cmd._should_skip({}) is None
