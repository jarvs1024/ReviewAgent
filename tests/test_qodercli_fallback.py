"""QoderCLIProvider fallback model — primary fails → retry with fallback."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from reviewagent.llm.base import LLMResult
from reviewagent.llm.qodercli_errors import QoderCLIOutputError, QoderCLITimeoutError
from reviewagent.llm.qodercli_provider import QoderCLIProvider


def _ok_result(model: str = "Qwen3.7-Plus") -> LLMResult:
    return LLMResult(
        data={"ok": True},
        provider="qodercli",
        model=model,
        raw_output='{"ok": true}',
    )


class TestFallbackModel:
    """Primary model failure triggers fallback to QODERCLI_FALLBACK_MODEL."""

    def test_primary_success_no_fallback(self, tmp_path: Path):
        """Primary succeeds → fallback never invoked."""
        provider = QoderCLIProvider()
        with patch(
            "reviewagent.llm.qodercli_provider.run_subprocess",
            return_value=_ok_result("DeepSeek-V4-Flash"),
        ) as mock_run:
            result = provider.run(
                agent="describe", prompt="x", workdir=tmp_path,
                files=[], timeout=30, tolerant_markdown=False,
            )
        assert result.data == {"ok": True}
        assert mock_run.call_count == 1

    def test_output_error_triggers_fallback(self, tmp_path: Path, monkeypatch):
        """QoderCLIOutputError on primary → retry with fallback model."""
        monkeypatch.setenv("QODERCLI_FALLBACK_MODEL", "Qwen3.7-Plus")
        from reviewagent.config import Config
        monkeypatch.setattr(
            "reviewagent.llm.qodercli_provider.config",
            Config.from_env(),
        )

        provider = QoderCLIProvider()
        with patch(
            "reviewagent.llm.qodercli_provider.run_subprocess",
            side_effect=[
                QoderCLIOutputError("agent output result not JSON: "),
                _ok_result("Qwen3.7-Plus"),
            ],
        ) as mock_run:
            result = provider.run(
                agent="describe", prompt="x", workdir=tmp_path,
                files=[], timeout=30, tolerant_markdown=False,
            )
        assert result.data == {"ok": True}
        assert mock_run.call_count == 2
        # Second call should use fallback model
        second_call_kwargs = mock_run.call_args_list[1]
        assert second_call_kwargs.kwargs.get("model") == "Qwen3.7-Plus" or \
               second_call_kwargs[1].get("model") == "Qwen3.7-Plus"

    def test_timeout_error_triggers_fallback(self, tmp_path: Path, monkeypatch):
        """QoderCLITimeoutError on primary → retry with fallback model."""
        monkeypatch.setenv("QODERCLI_FALLBACK_MODEL", "Qwen3.7-Plus")
        from reviewagent.config import Config
        monkeypatch.setattr(
            "reviewagent.llm.qodercli_provider.config",
            Config.from_env(),
        )

        provider = QoderCLIProvider()
        with patch(
            "reviewagent.llm.qodercli_provider.run_subprocess",
            side_effect=[
                QoderCLITimeoutError("qodercli timeout after 600s"),
                _ok_result("Qwen3.7-Plus"),
            ],
        ) as mock_run:
            result = provider.run(
                agent="describe", prompt="x", workdir=tmp_path,
                files=[], timeout=30, tolerant_markdown=False,
            )
        assert result.data == {"ok": True}
        assert mock_run.call_count == 2

    def test_no_fallback_model_raises_original(self, tmp_path: Path, monkeypatch):
        """QODERCLI_FALLBACK_MODEL empty → original error re-raised."""
        monkeypatch.delenv("QODERCLI_FALLBACK_MODEL", raising=False)
        from reviewagent.config import Config
        monkeypatch.setattr(
            "reviewagent.llm.qodercli_provider.config",
            Config.from_env(),
        )

        provider = QoderCLIProvider()
        with patch(
            "reviewagent.llm.qodercli_provider.run_subprocess",
            side_effect=QoderCLIOutputError("not JSON"),
        ) as mock_run:
            with pytest.raises(QoderCLIOutputError, match="not JSON"):
                provider.run(
                    agent="describe", prompt="x", workdir=tmp_path,
                    files=[], timeout=30, tolerant_markdown=False,
                )
        assert mock_run.call_count == 1

    def test_fallback_same_as_primary_raises(self, tmp_path: Path, monkeypatch):
        """Fallback == primary → no retry (avoid infinite loop)."""
        monkeypatch.setenv("QODERCLI_MODEL", "DeepSeek-V4-Flash")
        monkeypatch.setenv("QODERCLI_FALLBACK_MODEL", "DeepSeek-V4-Flash")
        from reviewagent.config import Config
        monkeypatch.setattr(
            "reviewagent.llm.qodercli_provider.config",
            Config.from_env(),
        )

        provider = QoderCLIProvider()
        with patch(
            "reviewagent.llm.qodercli_provider.run_subprocess",
            side_effect=QoderCLIOutputError("not JSON"),
        ) as mock_run:
            with pytest.raises(QoderCLIOutputError, match="not JSON"):
                provider.run(
                    agent="describe", prompt="x", workdir=tmp_path,
                    files=[], timeout=30, tolerant_markdown=False,
                )
        assert mock_run.call_count == 1

    def test_fallback_also_fails_raises(self, tmp_path: Path, monkeypatch):
        """Both primary and fallback fail → fallback error propagated."""
        monkeypatch.setenv("QODERCLI_FALLBACK_MODEL", "Qwen3.7-Plus")
        from reviewagent.config import Config
        monkeypatch.setattr(
            "reviewagent.llm.qodercli_provider.config",
            Config.from_env(),
        )

        provider = QoderCLIProvider()
        with patch(
            "reviewagent.llm.qodercli_provider.run_subprocess",
            side_effect=[
                QoderCLIOutputError("primary failed"),
                QoderCLIOutputError("fallback failed too"),
            ],
        ) as mock_run:
            with pytest.raises(QoderCLIOutputError, match="fallback failed too"):
                provider.run(
                    agent="describe", prompt="x", workdir=tmp_path,
                    files=[], timeout=30, tolerant_markdown=False,
                )
        assert mock_run.call_count == 2

    def test_each_call_independent(self, tmp_path: Path, monkeypatch):
        """Each run() call tries primary first (no sticky fallback)."""
        monkeypatch.setenv("QODERCLI_FALLBACK_MODEL", "Qwen3.7-Plus")
        from reviewagent.config import Config
        monkeypatch.setattr(
            "reviewagent.llm.qodercli_provider.config",
            Config.from_env(),
        )

        provider = QoderCLIProvider()
        with patch(
            "reviewagent.llm.qodercli_provider.run_subprocess",
            side_effect=[
                QoderCLIOutputError("primary failed"),
                _ok_result("Qwen3.7-Plus"),       # first call: fallback wins
                _ok_result("DeepSeek-V4-Flash"),   # second call: primary wins
            ],
        ) as mock_run:
            r1 = provider.run(
                agent="describe", prompt="x", workdir=tmp_path,
                files=[], timeout=30, tolerant_markdown=False,
            )
            r2 = provider.run(
                agent="describe", prompt="y", workdir=tmp_path,
                files=[], timeout=30, tolerant_markdown=False,
            )
        assert r1.model == "Qwen3.7-Plus"   # fallback succeeded
        assert r2.model == "DeepSeek-V4-Flash"  # primary succeeded
        assert mock_run.call_count == 3
