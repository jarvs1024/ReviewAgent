"""C1+C3: /metrics endpoint 暴露 prometheus text format, 内含所有注册过的 HELP/TYPE."""
from __future__ import annotations

import os
import tempfile
import pathlib
from unittest.mock import patch

import pytest


def _clear_registry():
    """清空 metrics singleton — 在需要隔离的测试里手工调用."""
    from reviewagent.metrics import metrics
    with metrics._lock:
        metrics._counters.clear()
        metrics._gauges.clear()
        metrics._registry.clear()


@pytest.fixture
def client(monkeypatch):
    """构造 FastAPI TestClient + 隔离 sqlite path."""
    # 跑测试前先清空 metrics (以防别的 test 在 metrics 里留数据)
    _clear_registry()

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    from reviewagent.config import config as _cfg
    monkeypatch.setattr(
        type(_cfg), "sqlite_path",
        property(lambda self: pathlib.Path(path)),
        raising=False,
    )

    import reviewagent.telemetry.store as st_mod
    st_mod._store = None

    from fastapi.testclient import TestClient
    from reviewagent.main import app
    yield TestClient(app)

    st_mod._store = None
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def test_endpoint_basic_format(client) -> None:
    """访问 /metrics 应返回 text/plain + 含 metric 数据."""
    from reviewagent.metrics import inc as _metric_inc, metrics as _metrics
    _metric_inc("reviewagent_webhook_received_total", object_kind="merge_request")
    _metric_inc("reviewagent_webhook_skipped_total", reason="cooldown")
    _metrics.register_help("reviewagent_webhook_received_total", "Webhook events received.")
    _metrics.register_help("reviewagent_webhook_skipped_total", "Webhook events skipped.")

    res = client.get("/metrics")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/plain")
    body = res.text
    assert "reviewagent_webhook_received_total" in body
    assert "reviewagent_webhook_skipped_total" in body
    assert "# HELP reviewagent_webhook_received_total" in body
    assert "# TYPE reviewagent_webhook_received_total counter" in body
    assert 'object_kind="merge_request"' in body


def test_endpoint_no_duplicate_lines() -> None:
    """每条 metric 只出现一次 (format_prometheus 之前有双倍 bug)."""
    _clear_registry()
    from reviewagent.metrics import inc as _metric_inc, metrics as _metrics
    _metric_inc("reviewagent_dedup_test", label="a")
    _metric_inc("reviewagent_dedup_test", label="a")
    _metrics.register_help("reviewagent_dedup_test", "test")
    out = _metrics.format_prometheus()
    data_lines = [ln for ln in out.splitlines()
                  if ln.startswith("reviewagent_dedup_test{")]
    assert len(data_lines) == 1
    assert out.count("# HELP reviewagent_dedup_test") == 1
    assert out.count("# TYPE reviewagent_dedup_test") == 1


def test_endpoint_help_registered_for_all(client) -> None:
    """main._register_metric_help 注册的 metric 即使 0 计数也应在 /metrics 里出现."""
    res = client.get("/metrics")
    assert res.status_code == 200
    body = res.text
    expected = [
        "reviewagent_improve_file_limit_total",
        "reviewagent_improve_files_skipped_total",
        "reviewagent_webhook_received_total",
        "reviewagent_webhook_skipped_total",
        "reviewagent_chain_enqueued_total",
        "reviewagent_lock_diff_head_total",
        "reviewagent_lock_chain_total",
        "reviewagent_suggestion_supersede_total",
        "reviewagent_llm_provider_initialized_total",
    ]
    for name in expected:
        assert f"# HELP {name}" in body, f"missing HELP for {name} in /metrics output"
        assert f"# TYPE {name}" in body, f"missing TYPE for {name}"


def test_endpoint_label_value_quotes_escaped() -> None:
    """label value 含双引号应该被转义."""
    _clear_registry()
    from reviewagent.metrics import inc as _metric_inc, metrics as _metrics
    _metric_inc("reviewagent_test_escape", label='hi"world')
    out = _metrics.format_prometheus()
    assert 'hi\\"world' in out


def test_endpoint_unknown_labelset_outputs_zero() -> None:
    """注册了 HELP 但还没 inc 的 metric 应输出 'name 0' (无 labels)."""
    _clear_registry()
    from reviewagent.metrics import metrics as _metrics
    _metrics.register_help("reviewagent_new_metric", "A brand new metric.")
    out = _metrics.format_prometheus()
    assert "# HELP reviewagent_new_metric" in out
    assert "# TYPE reviewagent_new_metric counter" in out
    assert any(
        ln.strip() == "reviewagent_new_metric 0" for ln in out.splitlines()
    ), f"expected 'reviewagent_new_metric 0' line in:\n{out}"
