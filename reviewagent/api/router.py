"""Telemetry API 路由 — 暴露 reviewagent 数据采集.

端点:
    GET /api/v1/telemetry/health           - 健康检查
    GET /api/v1/telemetry/runs              - 列出 run (分页 + 多条件过滤)
    GET /api/v1/telemetry/runs/{id}         - 单 run 详情
    GET /api/v1/telemetry/mr/{proj}/{iid}   - MR 元信息 + run 历史
    GET /api/v1/telemetry/summary           - 聚合统计 (按 command/status/day/MR)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from reviewagent.logging_setup import logger
from reviewagent.telemetry.store import get_store, is_effective_action


router = APIRouter(prefix="/api/v1/telemetry", tags=["telemetry"])

_ADOPTION_SOURCE_LABELS = {
    "ui_apply": "应用建议",
    "manual_change": "手动修改",
    "adopt_command": "/adopt",
    "unknown": "历史数据",
    "gitlab_resolve": "GitLab 直接解决主题",
}

_STATE_LABELS = {
    "open": "待处理",
    "applied": "已采纳",
    "dismissed": "已忽略",
    "resolved": "已关闭（未分类）",
    "superseded": "已过期",
}


def _adoption_source_label(source: str | None) -> str | None:
    return _ADOPTION_SOURCE_LABELS.get(source) if source else None


def _parse_iso(s: str | None) -> str | None:
    """把 ISO 8601 字符串归一为 SQLite 能比较的 ISO 格式 (含时区)."""
    if not s:
        return None
    s = s.strip()
    # 接受 'YYYY-MM-DD' 或完整 ISO
    if len(s) == 10:
        # Why 要严格校验: 之前只看长度, 任何 10 个字符都被当成日期
        # (`since=not-a-date` 会静默通过, 查询按错误时间窗返回数据而不是 400).
        try:
            datetime.strptime(s, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(
                status_code=400, detail=f"invalid ISO datetime: {s}"
            )
        return s + "T00:00:00+00:00"
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except ValueError:
        raise HTTPException(
            status_code=400, detail=f"invalid ISO datetime: {s}"
        )


@router.get("/health")
async def health() -> dict[str, Any]:
    s = get_store()
    with s._conn() as conn:  # noqa: SLF001
        n_mr = conn.execute("SELECT COUNT(*) AS n FROM mr_activity").fetchone()["n"]
        n_run = conn.execute("SELECT COUNT(*) AS n FROM review_runs").fetchone()["n"]
    return {
        "status": "ok",
        "db_path": str(s.path),
        "mr_records": n_mr,
        "run_records": n_run,
    }


@router.get("/runs")
async def list_runs(
    project_id: int | None = Query(None, description="按 project_id 过滤"),
    mr_iid: int | None = Query(None, description="按 mr_iid 过滤 (需 project_id 同时)"),
    command: str | None = Query(None, description="describe/improve"),
    status: str | None = Query(None, description="running/success/failed/timeout"),
    since: str | None = Query(None, description="起始 ISO 时间"),
    until: str | None = Query(None, description="结束 ISO 时间 (exclusive)"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    s = get_store()
    runs = s.list_runs(
        project_id=project_id, mr_iid=mr_iid,
        command=command, status=status,
        since=_parse_iso(since), until=_parse_iso(until),
        limit=limit, offset=offset,
    )
    total = s.count_runs(
        project_id=project_id, mr_iid=mr_iid,
        command=command, status=status,
        since=_parse_iso(since), until=_parse_iso(until),
    )
    return {"total": total, "limit": limit, "offset": offset, "runs": runs}


@router.get("/runs/{run_id}")
async def run_detail(run_id: int) -> dict[str, Any]:
    s = get_store()
    with s._conn() as conn:  # noqa: SLF001
        row = conn.execute("SELECT * FROM review_runs WHERE id = ?", (run_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"run not found: {run_id}")
    return {"run": dict(row)}


@router.get("/mr/{project_id}/{mr_iid}")
async def mr_detail(project_id: int, mr_iid: int) -> dict[str, Any]:
    s = get_store()
    mr = s.get_mr(project_id, mr_iid)
    if not mr:
        raise HTTPException(404, f"MR not found: {project_id}/{mr_iid}")
    runs = s.list_runs(project_id=project_id, mr_iid=mr_iid, limit=50)
    # 当前进程配置的 LLM provider (opencode/qodercli), 给 dashboard 一眼看到当前用的哪家
    from reviewagent.config import config as _app_config
    current_llm_provider = getattr(_app_config, "llm_provider", None)
    # 历史 run 上最近的 provider (按 finished_at desc, 跳过 llm_provider IS NULL 的旧记录)
    last_provider = next(
        (r.get("llm_provider") for r in runs if r.get("llm_provider")),
        None,
    )
    return {
        "mr": mr,
        "recent_runs": runs,
        "current_llm_provider": current_llm_provider,
        "last_llm_provider": last_provider,
    }


@router.get("/mr/{project_id}/{mr_iid}/suggestions")
async def mr_suggestions(
    project_id: int, mr_iid: int,
    state: str | None = Query(None),
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    rows = get_store().list_suggestions(
        project_id=project_id, mr_iid=mr_iid, state=state,
        limit=limit, offset=offset,
    )
    for row in rows:
        source = row.get("adoption_source")
        row["adoption_source_label"] = _adoption_source_label(source)
        row["state_label"] = _STATE_LABELS.get(row.get("state"), row.get("state"))
    return {"total": len(rows), "limit": limit, "offset": offset, "suggestions": rows}


@router.get("/mr/{project_id}/{mr_iid}/runs")
async def mr_runs(project_id: int, mr_iid: int, limit: int = Query(100, ge=1, le=1000)) -> dict[str, Any]:
    return {"runs": get_store().list_runs(project_id=project_id, mr_iid=mr_iid, limit=limit)}


@router.get("/mr/{project_id}/{mr_iid}/stats")
async def mr_stats(project_id: int, mr_iid: int) -> dict[str, Any]:
    return get_store().suggestion_stats(project_id, mr_iid)


@router.get("/mr/{project_id}/{mr_iid}/timeline")
async def mr_timeline(project_id: int, mr_iid: int, limit: int = Query(200, ge=1, le=1000)) -> dict[str, Any]:
    """MR 事件时间线。

    `effective` 列（仅 suggestion_action 事件有意义）标注这条动作**是否真的生效**:
    `/adopt` 校验失败也会写一行 `action='adopted'` 作审计（`validation_status` 区分）,
    但那不是一次真实采纳 —— 消费者不必自己复刻 `is_effective_action` 规则,
    直接看 `effective` 即可（1=生效, 0=只是尝试/被拒）。
    """
    s = get_store()
    with s._conn() as conn:  # noqa: SLF001
        rows = conn.execute(
            "SELECT started_at AS at, 'run' AS event_type, id AS event_id, command AS detail, "
            "       status AS state, NULL AS effective "
            "FROM review_runs WHERE project_id=? AND mr_iid=? "
            "UNION ALL SELECT created_at, 'suggestion_action', id, action, validation_status, NULL "
            "FROM suggestion_actions WHERE project_id=? AND mr_iid=? "
            "UNION ALL SELECT created_at, 'suggestion_posted', id, file_path, state, NULL "
            "FROM suggestions WHERE project_id=? AND mr_iid=? "
            "ORDER BY at DESC LIMIT ?",
            (project_id, mr_iid, project_id, mr_iid, project_id, mr_iid, limit),
        ).fetchall()
    events = []
    for row in rows:
        event = dict(row)
        if event.get("event_type") == "suggestion_action":
            # 单一事实源: store.is_effective_action（统计口径与这里必须一致，
            # 在 SQL 里重写一遍规则会随规则演进悄悄漂移）
            event["effective"] = int(
                is_effective_action(str(event.get("detail") or ""), event.get("state"))
            )
        events.append(event)
    return {"events": events}


@router.get("/mrs")
async def list_mrs(
    project_id: int | None = Query(None), state: str | None = Query(None),
    since: str | None = Query(None), until: str | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
) -> dict[str, Any]:
    # 时间窗口按 created_at (MR 开启时间), 与 list_mrs / mr_overview / 周报口径一致.
    mrs = get_store().list_mrs(
        project_id=project_id, state=state,
        since=_parse_iso(since), until=_parse_iso(until), limit=limit,
    )
    return {"total": len(mrs), "mrs": mrs}


@router.get("/mrs/{project_id}/{mr_iid}")
async def mr_detail_alias(project_id: int, mr_iid: int) -> dict[str, Any]:
    return await mr_detail(project_id, mr_iid)


@router.get("/summary")
async def summary(
    since: str | None = Query(None, description="起始 ISO 时间"),
    until: str | None = Query(None, description="结束 ISO 时间 (exclusive)"),
) -> dict[str, Any]:
    s = get_store()
    return s.summary(since=_parse_iso(since), until=_parse_iso(until))


@router.get("/metrics/overview")
async def metrics_overview(
    project_id: int | None = Query(None), since: str | None = Query(None), until: str | None = Query(None),
) -> dict[str, Any]:
    s = get_store()
    result = s.summary(since=_parse_iso(since), until=_parse_iso(until))
    result["suggestions"] = s.suggestion_metrics(project_id=project_id, since=_parse_iso(since), until=_parse_iso(until))
    return result


@router.get("/metrics/severity")
async def metrics_severity(project_id: int | None = Query(None), since: str | None = Query(None), until: str | None = Query(None)) -> dict[str, Any]:
    return {"severity_counts": get_store().suggestion_metrics(project_id=project_id, since=_parse_iso(since), until=_parse_iso(until))["severity_counts"]}


@router.get("/metrics/rules")
async def metrics_rules(
    project_id: int | None = Query(None), since: str | None = Query(None),
    until: str | None = Query(None), top_n: int = Query(10, ge=1, le=100),
) -> dict[str, Any]:
    """suggestion.rule_keys 拆分计数 (真实的规则键, 不是 severity)."""
    rows = get_store().rule_key_counts(
        project_id=project_id, since=_parse_iso(since),
        until=_parse_iso(until), top_n=top_n,
    )
    return {"rules": [{"rule_key": key, "count": count} for key, count in rows]}


@router.get("/metrics/authors")
async def metrics_authors(project_id: int | None = Query(None), since: str | None = Query(None), until: str | None = Query(None)) -> dict[str, Any]:
    s = get_store()
    mr_clauses, mr_params = ["1=1"], []
    if project_id is not None:
        mr_clauses.append("project_id = ?"); mr_params.append(project_id)
    if since:
        mr_clauses.append("datetime(created_at) >= datetime(?)"); mr_params.append(_parse_iso(since))
    if until:
        mr_clauses.append("datetime(created_at) < datetime(?)"); mr_params.append(_parse_iso(until))

    run_clauses, run_params = ["1=1"], []
    if project_id is not None:
        run_clauses.append("project_id = ?"); run_params.append(project_id)
    if since:
        run_clauses.append("datetime(started_at) >= datetime(?)"); run_params.append(_parse_iso(since))
    if until:
        run_clauses.append("datetime(started_at) < datetime(?)"); run_params.append(_parse_iso(until))

    with s._conn() as conn:  # noqa: SLF001
        mr_rows = conn.execute(
            "SELECT COALESCE(NULLIF(author_sticky,''),'unknown') AS author, COUNT(*) AS mrs "
            f"FROM mr_activity WHERE {' AND '.join(mr_clauses)} GROUP BY author",
            mr_params,
        ).fetchall()
        run_rows = conn.execute(
            "SELECT COALESCE(NULLIF(actor_username,''),'unknown') AS author, COUNT(*) AS runs "
            f"FROM review_runs WHERE {' AND '.join(run_clauses)} GROUP BY author",
            run_params,
        ).fetchall()

    merged: dict[str, dict[str, Any]] = {}
    for row in mr_rows:
        merged.setdefault(row["author"], {"author": row["author"], "mrs": 0, "runs": 0})["mrs"] = row["mrs"]
    for row in run_rows:
        merged.setdefault(row["author"], {"author": row["author"], "mrs": 0, "runs": 0})["runs"] = row["runs"]
    authors = sorted(merged.values(), key=lambda a: (-a["runs"], -a["mrs"], a["author"]))
    return {"authors": authors}


@router.get("/dismissals")
async def dismissals(
    project_id: int | None = Query(None), mr_iid: int | None = Query(None), rule_key: str | None = Query(None),
    since: str | None = Query(None), until: str | None = Query(None),
    limit: int = Query(500, ge=1, le=5000), offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    s = get_store()
    rows = s.list_dismissals(
        project_id=project_id, mr_iid=mr_iid, rule_key=rule_key,
        since=_parse_iso(since), until=_parse_iso(until),
        limit=limit, offset=offset,
    )
    total = s.count_dismissals(
        project_id=project_id, mr_iid=mr_iid, rule_key=rule_key,
        since=_parse_iso(since), until=_parse_iso(until),
    )
    return {"total": total, "limit": limit, "offset": offset, "dismissals": rows}
@router.get("/dismissals/by-rule")
async def dismissals_by_rule(
    project_id: int | None = Query(None),
    since: str | None = Query(None),
) -> dict[str, Any]:
    return {"rules": get_store().dismissals_by_rule(project_id=project_id, since=_parse_iso(since))}

@router.get("/mrs/{project_id}/{mr_iid}/dismissals")
async def mr_dismissals(project_id: int, mr_iid: int, limit: int = Query(200, ge=1, le=2000)) -> dict[str, Any]:
    return {"dismissals": get_store().list_dismissals(project_id=project_id, mr_iid=mr_iid, limit=limit)}

@router.get("/weekly-reports")
async def list_weekly_reports(
    project_id: int | None = Query(None), limit: int = Query(20, ge=1, le=200),
) -> dict[str, Any]:
    from reviewagent.config import config
    base = (config.weekly_reports_dir if hasattr(config, "weekly_reports_dir") else config.data_dir / "weekly_reports")
    if not base.exists():
        return {"reports": []}
    files = []
    for p in sorted(base.glob("weekly-*.json"), reverse=True):
        if project_id is not None:
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if data.get("project_id") != project_id:
                    continue
            except (json.JSONDecodeError, OSError):
                continue
        files.append({"name": p.name, "path": str(p), "url": f"/api/v1/telemetry/weekly-reports/{p.name}", "size": p.stat().st_size,
                      "modified": p.stat().st_mtime})
    return {"total": len(files), "reports": files[:limit]}

@router.get("/weekly-reports/{name}")
async def read_weekly_report(name: str) -> dict[str, Any]:
    from reviewagent.config import config
    base = (config.weekly_reports_dir if hasattr(config, "weekly_reports_dir") else config.data_dir / "weekly_reports")
    if "/" in name or ".." in name:
        raise HTTPException(400, "invalid name")
    path = base / name
    if not path.exists():
        raise HTTPException(404, f"report not found: {name}")
    return {"name": name, "json": json.loads(path.read_text(encoding="utf-8"))}
