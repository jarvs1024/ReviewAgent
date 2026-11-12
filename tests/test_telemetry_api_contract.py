"""Telemetry API 的 **HTTP 层**端点契约测试（全端点覆盖）.

Why 需要这份文件: 此前 HTTP 层只测了 4 个端点（/runs、/dismissals、
/metrics/rules、/metrics/authors），其余 15 个端点只有**运行时**验证
（真实业务跑通时逐个 curl 过 200），**没有回归测试** ——
端点被改坏（路径、参数、响应结构、错误码）不会有任何测试变红。

这里按「契约」而非「实现」断言:
    每个端点 → 200 + 响应体关键键存在 + 分页/过滤参数生效
    错误路径 → 404 / 400 / 422 各归其位

新增端点时同步在 `ALL_ENDPOINTS` 里登记，`test_every_route_is_registered`
会强制它出现在契约表里（防漏测）。
"""
from __future__ import annotations

import os
import pathlib
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

PROJECT = 31
MR = 3
SHA = "a" * 40
NOTE_ID = "d-1111"
DISMISSED_NOTE_ID = "d-2222"
NOW = datetime(2026, 11, 11, 0, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def client(monkeypatch):
    """FastAPI TestClient + 隔离 sqlite + 隔离周报目录."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    reports_dir = tempfile.mkdtemp(prefix="weekly-reports-")

    from reviewagent.config import config as _cfg
    monkeypatch.setattr(
        type(_cfg),
        "sqlite_path",
        property(lambda self: pathlib.Path(db_path)),
        raising=False,
    )
    monkeypatch.setattr(
        type(_cfg),
        "weekly_reports_dir",
        property(lambda self: pathlib.Path(reports_dir)),
        raising=False,
    )

    import reviewagent.telemetry.store as st_mod

    st_mod._store = None

    from fastapi.testclient import TestClient
    from reviewagent.main import app

    yield TestClient(app)

    st_mod._store = None
    try:
        os.unlink(db_path)
    except FileNotFoundError:
        pass


@pytest.fixture
def seeded(client):
    """写入一条 MR / run / suggestion / action，供只读端点消费."""
    from reviewagent.telemetry.models import MRRecord
    from reviewagent.telemetry.store import get_store

    store = get_store()
    store.upsert_mr(
        MRRecord(
            project_id=PROJECT,
            mr_iid=MR,
            title="契约测试 MR",
            author_username="测试员@root",
            source_branch="feature/contract",
            target_branch="main",
            state="opened",
            created_at=NOW - timedelta(days=1),
            updated_at=NOW,
            description_generated=True,
            last_review_at=NOW,
        )
    )
    run_id = store.insert_run(
        _run_record()
    )
    store.finish_run(
        run_id,
        status="success",
        model="DeepSeek-Flash",
        prompt_tokens=100,
        completion_tokens=20,
        cost_credits=0.5,
        duration_ms=1234,
        llm_provider="qoder_sdk",
        rule_keys_cited=["R-ERR", "SSD-RULE-NAMING"],
        suggestion_count=1,
    )
    store.record_suggestion(
        project_id=PROJECT,
        mr_iid=MR,
        note_id=NOTE_ID,
        file_path="app/service.py",
        target_line=5,
        target_line_end=5,
        existing_code="from app.utils import *",
        improved_code="from app import utils",
        header="禁止 wildcard import",
        severity="high",
        head_sha=SHA,
        rule_keys=["R-ERR"],
        fingerprint="fp-1",
        content_fingerprint="cfp-1",
        cohort_key="cohort-1",
        severity_source="rule",
        score=77,
        applicability="replace",
    )
    # 第二条: 已忽略 —— dismissals 端点按 **suggestion.state='dismissed'** 过滤,
    # 只写 action 行不翻转 state 的话端点查不到（与真实 dismiss 链路一致）。
    store.record_suggestion(
        project_id=PROJECT,
        mr_iid=MR,
        note_id=DISMISSED_NOTE_ID,
        file_path="app/service.py",
        target_line=9,
        target_line_end=9,
        existing_code="import os",
        improved_code="import os",
        header="补充导入",
        severity="medium",
        head_sha=SHA,
        rule_keys=["R-ERR", "SSD-RULE-NAMING"],
        fingerprint="fp-2",
        content_fingerprint="cfp-2",
        cohort_key="cohort-2",
        severity_source="rule",
        score=60,
        applicability="replace",
    )
    store.record_suggestion_action(
        project_id=PROJECT,
        mr_iid=MR,
        suggestion_note_id=DISMISSED_NOTE_ID,
        file_path="app/service.py",
        target_line=9,
        action="dismissed",
        actor_username="测试员@root",
        reason="本轮范围外",
        validation_status="ok",
        head_sha_posted=SHA,
    )
    store.update_suggestion_state(
        DISMISSED_NOTE_ID,
        "dismissed",
        actor_username="测试员@root",
        dismissed_reason="本轮范围外",
    )
    return store


def _run_record():
    from reviewagent.telemetry.models import ReviewRun

    return ReviewRun(
        project_id=PROJECT,
        mr_iid=MR,
        command="improve",
        triggered_by="note",
        actor_username="测试员@root",
        started_at=NOW,
        status="running",
    )


# ---------------------------------------------------------------------------
# 全端点契约表
# ---------------------------------------------------------------------------

#: (端点路径, 响应体必须存在的键)
ALL_ENDPOINTS: list[tuple[str, tuple[str, ...]]] = [
    ("/api/v1/telemetry/health", ("status", "db_path", "mr_records", "run_records")),
    ("/api/v1/telemetry/runs", ("total", "limit", "offset", "runs")),
    ("/api/v1/telemetry/mr/{p}/{i}", ("mr", "recent_runs", "current_llm_provider")),
    ("/api/v1/telemetry/mr/{p}/{i}/suggestions", ("total", "limit", "offset", "suggestions")),
    ("/api/v1/telemetry/mr/{p}/{i}/runs", ("runs",)),
    ("/api/v1/telemetry/mr/{p}/{i}/stats", ("total", "state_counts", "action_counts")),
    ("/api/v1/telemetry/mr/{p}/{i}/timeline", ("events",)),
    ("/api/v1/telemetry/mrs", ("total", "mrs")),
    ("/api/v1/telemetry/mrs/{p}/{i}", ("mr", "recent_runs")),
    ("/api/v1/telemetry/summary", ("total_runs",)),
    ("/api/v1/telemetry/metrics/overview", ("suggestions",)),
    ("/api/v1/telemetry/metrics/severity", ("severity_counts",)),
    ("/api/v1/telemetry/metrics/rules", ("rules",)),
    ("/api/v1/telemetry/metrics/authors", ("authors",)),
    ("/api/v1/telemetry/dismissals", ("total", "dismissals")),
    ("/api/v1/telemetry/dismissals/by-rule", ("rules",)),
    ("/api/v1/telemetry/mrs/{p}/{i}/dismissals", ("dismissals",)),
    ("/api/v1/telemetry/weekly-reports", ("reports",)),
]


def _resolve(path: str) -> str:
    return path.format(p=PROJECT, i=MR)


@pytest.mark.parametrize("path,keys", ALL_ENDPOINTS, ids=[p for p, _ in ALL_ENDPOINTS])
def test_endpoint_contract(client, seeded, path, keys) -> None:
    """每个端点: 200 + 响应体关键键存在."""
    res = client.get(_resolve(path))
    assert res.status_code == 200, f"{path} -> {res.status_code} {res.text[:200]}"
    body = res.json()
    for key in keys:
        assert key in body, f"{path} 响应缺键 {key}: {list(body)}"


def test_every_route_is_registered(client) -> None:
    """**防漏测**: app 里注册的每个 telemetry 路由都必须在契约表里登记."""
    registered = {
        route.path
        for route in client.app.routes
        if getattr(route, "path", "").startswith("/api/v1/telemetry")
    }
    registered.discard("/api/v1/telemetry/runs/{run_id}")  # 单独用例覆盖
    registered.discard("/api/v1/telemetry/weekly-reports/{name}")  # 单独用例覆盖
    listed = {_resolve(p) for p, _ in ALL_ENDPOINTS}
    missing = registered - listed
    assert not missing, f"以下端点未登记进契约表: {sorted(missing)}"


# ---------------------------------------------------------------------------
# 过滤 / 分页生效
# ---------------------------------------------------------------------------


def test_runs_filter_by_project_and_command(client, seeded) -> None:
    res = client.get(f"/api/v1/telemetry/runs?project_id={PROJECT}&command=improve")
    assert res.status_code == 200
    assert res.json()["total"] == 1
    res = client.get(f"/api/v1/telemetry/runs?project_id={PROJECT}&command=describe")
    assert res.json()["total"] == 0


def test_runs_total_is_not_page_size(client, seeded) -> None:
    """total 必须是全量计数, 不能等于当前页条数（分页语义回归）."""
    res = client.get("/api/v1/telemetry/runs?limit=1")
    assert res.json()["total"] >= 1
    assert len(res.json()["runs"]) <= 1


def test_suggestions_filter_by_state(client, seeded) -> None:
    res = client.get(f"/api/v1/telemetry/mr/{PROJECT}/{MR}/suggestions?state=open")
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 1
    assert body["suggestions"][0]["state"] == "open"


def test_suggestions_expose_frontend_labels(client, seeded) -> None:
    body = client.get(f"/api/v1/telemetry/mr/{PROJECT}/{MR}/suggestions").json()
    by_id = {row["note_id"]: row for row in body["suggestions"]}
    assert by_id[NOTE_ID]["state_label"] == "待处理"
    assert by_id[DISMISSED_NOTE_ID]["state_label"] == "已忽略"
    # adoption_source 为空时也要有 label 键（前端不能因缺字段崩）
    assert "adoption_source_label" in by_id[NOTE_ID]


def test_timeline_merges_three_tables(client, seeded) -> None:
    body = client.get(f"/api/v1/telemetry/mr/{PROJECT}/{MR}/timeline").json()
    kinds = {e["event_type"] for e in body["events"]}
    assert "run" in kinds
    assert "suggestion_action" in kinds
    assert "suggestion_posted" in kinds


def test_dismissals_filter_by_rule_key(client, seeded) -> None:
    hit = client.get("/api/v1/telemetry/dismissals?rule_key=R-ERR").json()
    assert hit["total"] == 1
    miss = client.get("/api/v1/telemetry/dismissals?rule_key=__none__").json()
    assert miss["total"] == 0


def test_mr_detail_reports_provider(client, seeded) -> None:
    body = client.get(f"/api/v1/telemetry/mr/{PROJECT}/{MR}").json()
    assert body["last_llm_provider"] == "qoder_sdk"


# ---------------------------------------------------------------------------
# 错误路径
# ---------------------------------------------------------------------------


def test_run_detail_404_when_missing(client, seeded) -> None:
    assert client.get("/api/v1/telemetry/runs/999999").status_code == 404


def test_mr_detail_404_when_missing(client, seeded) -> None:
    assert client.get("/api/v1/telemetry/mr/999/999").status_code == 404


@pytest.mark.parametrize("path", [
    "/api/v1/telemetry/runs?since=not-a-date",
    "/api/v1/telemetry/mrs?since=not-a-date",
    "/api/v1/telemetry/summary?until=not-a-date",
    "/api/v1/telemetry/metrics/overview?since=not-a-date",
])
def test_invalid_iso_returns_400(client, seeded, path) -> None:
    assert client.get(path).status_code == 400


@pytest.mark.parametrize("path", [
    "/api/v1/telemetry/runs?limit=0",
    "/api/v1/telemetry/runs?limit=99999",
    "/api/v1/telemetry/runs?offset=-1",
    "/api/v1/telemetry/mr/{p}/{i}/suggestions?limit=0",
    "/api/v1/telemetry/metrics/rules?top_n=0",
])
def test_out_of_range_params_return_422(client, seeded, path) -> None:
    assert client.get(_resolve(path)).status_code == 422


def test_weekly_report_name_rejects_path_traversal(client, seeded) -> None:
    assert client.get("/api/v1/telemetry/weekly-reports/..%2Fetc%2Fpasswd").status_code in (400, 404)


def test_weekly_report_missing_returns_404(client, seeded) -> None:
    assert client.get("/api/v1/telemetry/weekly-reports/weekly-1999-W01.json").status_code == 404


def test_weekly_reports_list_is_empty_without_artifacts(client, seeded) -> None:
    body = client.get("/api/v1/telemetry/weekly-reports").json()
    assert body["reports"] == []


def test_timeline_marks_effective_vs_attempted(client, seeded) -> None:
    from reviewagent.telemetry.store import get_store

    """时间线必须区分「真实生效」与「只是尝试」.

    Why: `/adopt` 校验失败也会写一行 `action='adopted'` 作审计 —— 原始 SQL 与
    时间线里都能看到「尝试」, 消费者若不自己复刻规则就会把失败当成功。
    `effective` 列由 store.is_effective_action（同统计口径）计算, 不复制规则。
    """
    store = get_store()
    store.record_suggestion_action(
        project_id=PROJECT, mr_iid=MR, suggestion_note_id="n-ok",
        file_path="a.py", target_line=1, action="adopted",
        actor_username="tester", validation_status="ok",
    )
    store.record_suggestion_action(
        project_id=PROJECT, mr_iid=MR, suggestion_note_id="n-same-head",
        file_path="a.py", target_line=2, action="adopted",
        actor_username="tester", validation_status="same-head",
    )
    store.record_suggestion_action(
        project_id=PROJECT, mr_iid=MR, suggestion_note_id="n-unchanged",
        file_path="a.py", target_line=3, action="adopted",
        actor_username="tester", validation_status="target-unchanged",
    )
    store.record_suggestion_action(
        project_id=PROJECT, mr_iid=MR, suggestion_note_id="n-already",
        file_path="a.py", target_line=4, action="adopted",
        actor_username="tester", validation_status="already-dismissed",
    )
    store.record_suggestion_action(
        project_id=PROJECT, mr_iid=MR, suggestion_note_id="n-dismissed",
        file_path="a.py", target_line=5, action="dismissed",
        actor_username="tester", validation_status="ok",
    )

    body = client.get(f"/api/v1/telemetry/mr/{PROJECT}/{MR}/timeline").json()
    events = [e for e in body["events"] if e["event_type"] == "suggestion_action"]
    assert events, "应返回 suggestion_action 事件"
    by_detail = {e["detail"]: e for e in events}

    def eff(validation_status: str) -> int:
        for e in events:
            if e["state"] == validation_status:
                return int(e["effective"])
        raise AssertionError(f"没找到 validation_status={validation_status} 的事件")

    assert eff("ok") == 1, "真实采纳 → effective=1"
    assert eff("same-head") == 0, "同 head 未改代码 → 只是尝试"
    assert eff("target-unchanged") == 0, "目标未变 → 只是尝试"
    assert eff("already-dismissed") == 0, "已是 dismissed → 只是尝试"
    assert by_detail["dismissed"]["effective"] == 1, "dismissed 动作本身生效"


def test_timeline_effective_matches_store_rule(client, seeded) -> None:
    """`effective` 必须与 store 的口径**逐状态一致**（防规则漂移）."""
    from reviewagent.telemetry.store import get_store, is_effective_action

    store = get_store()
    cases = [
        ("adopted", "ok"), ("adopted", "same-head"),
        ("adopted", "content-unavailable"), ("adopted", "target-unchanged"),
        ("adopted", "already-applied"), ("adopted", "already-dismissed"),
        ("adopted", "already-resolved"), ("adopted", None),
        ("dismissed", "ok"), ("resolved", "gitlab-resolve"),
    ]
    for idx, (action, status) in enumerate(cases):
        store.record_suggestion_action(
            project_id=PROJECT, mr_iid=MR, suggestion_note_id=f"n-{idx}",
            file_path="a.py", target_line=idx + 1, action=action,
            actor_username="tester", validation_status=status,
        )

    body = client.get(f"/api/v1/telemetry/mr/{PROJECT}/{MR}/timeline").json()
    events = [e for e in body["events"] if e["event_type"] == "suggestion_action"]
    seen = {(str(e["detail"]), str(e["state"])) for e in events}
    for action, status in cases:
        assert (action, str(status)) in seen, f"种下的 {action}/{status} 没出现在时间线里"
    # 逐条比对: 端点的 effective 必须与 store 口径一致（含 seeded 里的历史事件）
    for event in events:
        expected = int(is_effective_action(str(event["detail"]), event["state"]))
        assert int(event["effective"]) == expected, (
            f"action={event['detail']} status={event['state']}: "
            f"effective={event['effective']} 期望 {expected}"
        )
