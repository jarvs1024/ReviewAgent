"""E8: `review_runs.model` 两义修复 —— 拆成「实际生效模型」+「请求时配置模型」.

Why 需要这份测试:
`review_runs.model` 这一列历史上塞了**两种语义**:
    1. 请求时用的**配置模型 id** (如 `deepseek/deepseek-v4-flash`) —— skipped run /
       provider 未回填 model 时的兜底值;
    2. provider **实际回报**的模型名 (如 `DeepSeek-Flash`).
两者混在一列里 → 任何按 `model` 做的统计/过滤都要先猜这行是哪种语义，
实测 p31 上同一个 success 状态就并存两种取值 (58 条配置 id vs 110 条回报名)。

修复后的契约:
    `model`            = **实际生效模型** —— provider 回报什么就记什么;
                          provider 没回报时记 NULL（不再拿配置 id 冒充）.
    `model_configured` = **请求时配置模型 id** —— run 发起时用的配置值,
                          与 provider 回报无关，成功/跳过/失败都记.

Why skipped 的 `model` 要置 NULL:
skipped 根本没调用 LLM，任何模型名都是假的；配置 id 挪到 `model_configured`
后，skipped 行的 `model` 为空才诚实。

Why 不能只靠「两列都有值」来判:
    - skipped:  model=NULL, model_configured='deepseek/deepseek-v4-flash'
    - success:  model='DeepSeek-Flash', model_configured='deepseek/deepseek-v4-flash'
两行 `model_configured` 相同、`model` 不同 → 语义已完全可分.
"""
from __future__ import annotations

import os
import pathlib
import tempfile
from datetime import datetime, timezone

import pytest

CONFIGURED = "deepseek/deepseek-v4-flash"
REPORTED = "DeepSeek-Flash"


@pytest.fixture
def store(monkeypatch):
    """隔离 sqlite 的 telemetry store."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    from reviewagent.config import config as _cfg

    monkeypatch.setattr(
        type(_cfg),
        "sqlite_path",
        property(lambda self: pathlib.Path(db_path)),
        raising=False,
    )
    import reviewagent.telemetry.store as st_mod

    st_mod._store = None
    try:
        yield st_mod.get_store()
    finally:
        st_mod._store = None
        try:
            os.unlink(db_path)
        except FileNotFoundError:
            pass


def _insert(store, command="improve"):
    from reviewagent.telemetry.models import ReviewRun

    return store.insert_run(
        ReviewRun(
            project_id=31,
            mr_iid=3,
            command=command,
            triggered_by="webhook",
            started_at=datetime(2026, 11, 11, tzinfo=timezone.utc),
        )
    )


def test_finish_run_stores_model_and_model_configured_separately(store):
    """success run: model=provider 回报值, model_configured=配置 id —— 两列各归其位."""
    run_id = _insert(store)
    store.finish_run(
        run_id,
        status="success",
        model=REPORTED,
        model_configured=CONFIGURED,
        prompt_tokens=10,
        completion_tokens=5,
    )
    row = store.list_runs(project_id=31, mr_iid=3)[0]
    assert row["model"] == REPORTED, "model 必须是 provider 实际回报的模型名"
    assert row["model_configured"] == CONFIGURED, "model_configured 必须是请求时的配置 id"


def test_skipped_run_has_null_model_but_keeps_model_configured(store):
    """skipped run: 没调 LLM → model 必须为空；配置 id 记到 model_configured."""
    run_id = _insert(store)
    store.finish_run(
        run_id,
        status="skipped",
        model=None,
        model_configured=CONFIGURED,
    )
    row = store.list_runs(project_id=31, mr_iid=3)[0]
    assert row["model"] is None, "skipped run 没调用 LLM, model 不得冒充配置 id"
    assert row["model_configured"] == CONFIGURED


def test_model_configured_column_exists_in_schema(store):
    """迁移后 review_runs 必须有 model_configured 列（老库升级也生效）."""
    with store._conn() as conn:  # noqa: SLF001
        cols = {r[1] for r in conn.execute("PRAGMA table_info(review_runs)")}
    assert "model_configured" in cols, f"缺 model_configured 列, 现有={sorted(cols)}"


def test_provider_without_reported_model_keeps_configured_column(store):
    """provider 没回填 model → model 为空, 但 model_configured 仍在（可追溯配置）."""
    run_id = _insert(store)
    store.finish_run(
        run_id,
        status="success",
        model=None,
        model_configured=CONFIGURED,
    )
    row = store.list_runs(project_id=31, mr_iid=3)[0]
    assert row["model"] is None
    assert row["model_configured"] == CONFIGURED


def test_run_detail_api_exposes_model_configured(store):
    """/runs/{id} 与 /runs 列表都要能拿到 model_configured（前端可区分两义）."""
    from fastapi.testclient import TestClient

    from reviewagent.main import app

    run_id = _insert(store)
    store.finish_run(
        run_id,
        status="success",
        model=REPORTED,
        model_configured=CONFIGURED,
    )
    c = TestClient(app)
    detail = c.get(f"/api/v1/telemetry/runs/{run_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["run"]["model_configured"] == CONFIGURED

    listing = c.get("/api/v1/telemetry/runs?project_id=31")
    assert listing.status_code == 200, listing.text
    runs = listing.json()["runs"]
    assert runs and runs[0]["model_configured"] == CONFIGURED


# ---------------------------------------------------------------------------
# 命令层: BaseCommand.run() 落库时的 model / model_configured 语义
# ---------------------------------------------------------------------------


def _patch_command_env(monkeypatch, *, provider_model: str):
    """把 BaseCommand 的外部依赖全部换成假件, 只留 telemetry 捕获点."""
    from types import SimpleNamespace

    import reviewagent.commands._common as common

    monkeypatch.setattr(
        common,
        "config",
        SimpleNamespace(
            llm_provider="qodercli",
            opencode_model=CONFIGURED,
            qodercli_model=provider_model,
            max_diff_chars=0,
        ),
    )

    class _FakeProvider:
        provider_name = "qodercli"

    monkeypatch.setattr(common, "get_client", lambda: _FakeProvider())

    fake_gitlab = SimpleNamespace(
        get_mr=lambda *_a, **_k: {
            "sha": "deadbeef",
            "state": "opened",
            "project_id": 1,
            "iid": 1,
            "title": "Test MR",
            "author": {"username": "root", "name": "Root"},
            "source_branch": "feat",
            "target_branch": "main",
            "created_at": "2026-08-05T00:00:00Z",
            "updated_at": "2026-08-05T00:00:00Z",
            "merged_at": None,
        },
        get_mr_diff=lambda *_a, **_k: "diff --git a/x b/x\n+ok",
        get_project_git_url=lambda *_a, **_k: "git@example.com:x.git",
    )
    monkeypatch.setattr(common, "prepare_workspace", lambda **_k: object())
    monkeypatch.setattr(common, "cleanup_workspace", lambda *_a, **_k: None)
    monkeypatch.setattr(common, "build_repo_context", lambda *_a, **_k: "")

    captured: dict = {}

    def _fake_emit_finished(run_id, status, **kw):
        captured["status"] = status
        captured.update(kw)

    from reviewagent.telemetry import events

    monkeypatch.setattr(events, "emit_run_finished", _fake_emit_finished)
    monkeypatch.setattr(events, "emit_run_started", lambda *_a, **_k: 1)
    monkeypatch.setattr(events, "emit_mr_activity", lambda *_a, **_k: None)
    return fake_gitlab, captured


def test_command_skipped_run_records_configured_model_only(monkeypatch):
    """命令层 skipped run: model=None（没调 LLM），model_configured=配置 id."""
    import reviewagent.commands._common as common

    fake_gitlab, captured = _patch_command_env(monkeypatch, provider_model="Lite")

    class _T(common.BaseCommand):
        COMMAND_NAME = "describe"
        DEFAULT_AGENT = "describe"

    cmd = _T(project_id=1, mr_iid=1)
    # MR 已 merged → 走 skip 分支
    _base_mr = fake_gitlab.get_mr()
    monkeypatch.setattr(
        cmd.gitlab,
        "get_mr",
        lambda *_a, **_k: {**_base_mr, "state": "merged"},
    )

    result = cmd.run()
    assert result["status"] == "skipped", result
    assert captured["status"] == "skipped"
    assert captured.get("model") is None, (
        f"skipped run 的 model 必须是 None, 实际={captured.get('model')!r}"
    )
    assert captured.get("model_configured") == "Lite", captured


def test_command_success_run_records_reported_model_and_configured(monkeypatch):
    """命令层 success run: model=provider 回报名, model_configured=配置 id."""
    import reviewagent.commands._common as common
    from reviewagent.llm.base import LLMResult

    fake_gitlab, captured = _patch_command_env(monkeypatch, provider_model="Lite")

    class _T(common.BaseCommand):
        COMMAND_NAME = "describe"
        DEFAULT_AGENT = "describe"

        def _call_agent(self, _ws, *, overflow_files=None):
            self._last_oc_result = LLMResult(
                data={"summary_md": "ok"},
                prompt_tokens=10,
                completion_tokens=5,
                model=REPORTED,
                provider="qodercli",
            )
            return {"summary_md": "ok"}

        def _publish(self, agent_result):
            return {"status": "success"}

    cmd = _T(project_id=1, mr_iid=1)
    monkeypatch.setattr(cmd, "gitlab", fake_gitlab)

    cmd.run()
    assert captured["status"] == "success", captured
    assert captured.get("model") == REPORTED, (
        f"success run 的 model 必须是 provider 回报值, 实际={captured.get('model')!r}"
    )
    assert captured.get("model_configured") == "Lite", captured


def test_command_success_run_without_reported_model_keeps_model_null(monkeypatch):
    """provider 没回填 model → model 必须是 None, 不得回退成配置 id 冒充."""
    import reviewagent.commands._common as common
    from reviewagent.llm.base import LLMResult

    fake_gitlab, captured = _patch_command_env(monkeypatch, provider_model="Lite")

    class _T(common.BaseCommand):
        COMMAND_NAME = "describe"
        DEFAULT_AGENT = "describe"

        def _call_agent(self, _ws, *, overflow_files=None):
            self._last_oc_result = LLMResult(
                data={"summary_md": "ok"},
                prompt_tokens=0,
                completion_tokens=0,
                model="",          # provider 没回填
                provider="qodercli",
            )
            return {"summary_md": "ok"}

        def _publish(self, agent_result):
            return {"status": "success"}

    cmd = _T(project_id=1, mr_iid=1)
    monkeypatch.setattr(cmd, "gitlab", fake_gitlab)

    cmd.run()
    assert captured["status"] == "success", captured
    assert captured.get("model") is None, (
        f"provider 未回填 model 时必须是 None, 实际={captured.get('model')!r}"
    )
    assert captured.get("model_configured") == "Lite", captured
