"""Subprocess fallback path — QODERCLI_DRIVER=subprocess."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from reviewagent.llm.qodercli_subprocess import run_subprocess
from reviewagent.llm.qodercli_provider import QoderCLIOutputError


def test_subprocess_path_invokes_node_script(tmp_path: Path) -> None:
    captured = {}
    fake_proc = MagicMock()
    fake_proc.stdout = json.dumps({
        "type": "result",
        "subtype": "success",
        "result": "{\"ok\": true}",
        "stop_reason": "end_turn",
        "duration_ms": 1234,
        "usage": {"input_tokens": 1, "output_tokens": 2},
    })
    fake_proc.stderr = ""
    fake_proc.returncode = 0

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["timeout"] = kwargs.get("timeout")
        return fake_proc

    with patch("subprocess.run", side_effect=_fake_run):
        result = run_subprocess(
            agent="improve",
            prompt="review",
            workdir=tmp_path,
            files=[],
            timeout=120,
            tolerant_markdown=False,
        )
    assert "--append-system-prompt" in captured["cmd"]
    assert "--model" in captured["cmd"]
    assert captured["cmd"][-1] == "review"
    assert result.data == {"ok": True}
    assert result.provider == "qodercli"
    assert result.model  # filled from config
    assert result.duration_ms >= 0


def test_subprocess_path_tolerant_markdown(tmp_path: Path) -> None:
    fake_proc = MagicMock()
    fake_proc.stdout = "not json"
    fake_proc.stderr = ""
    fake_proc.returncode = 0

    def _fake_run(cmd, **kwargs):
        return fake_proc

    with patch("subprocess.run", side_effect=_fake_run):
        result = run_subprocess(
            agent="improve",
            prompt="x",
            workdir=tmp_path,
            files=[],
            timeout=30,
            tolerant_markdown=True,
        )
    assert result.data == {}
    assert result.raw_output == "not json"


def test_subprocess_path_raises_on_empty_stdout(tmp_path: Path) -> None:
    fake_proc = MagicMock()
    fake_proc.stdout = ""
    fake_proc.stderr = "boom"
    fake_proc.returncode = 0
    with patch("subprocess.run", return_value=fake_proc):
        with pytest.raises(QoderCLIOutputError, match="empty"):
            run_subprocess(agent="improve", prompt="x", workdir=tmp_path, files=[], timeout=30, tolerant_markdown=False)
