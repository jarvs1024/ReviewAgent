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
from reviewagent.telemetry.store import get_store


router = APIRouter(prefix="/api/v1/telemetry", tags=["telemetry"])


def _parse_iso(s: str | None) -> str | None:
    """把 ISO 8601 字符串归一为 SQLite 能比较的 ISO 格式 (含时区)."""
    if not s:
        return None
    s = s.strip()
    # 接受 'YYYY-MM-DD' 或完整 ISO
    if len(s) == 10:
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
    return {"total": len(runs), "limit": limit, "offset": offset, "runs": runs}


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
    return {"mr": mr, "recent_runs": runs}


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
    return {"total": len(rows), "limit": limit, "offset": offset, "suggestions": rows}


@router.get("/mr/{project_id}/{mr_iid}/runs")
async def mr_runs(project_id: int, mr_iid: int, limit: int = Query(100, ge=1, le=1000)) -> dict[str, Any]:
    return {"runs": get_store().list_runs(project_id=project_id, mr_iid=mr_iid, limit=limit)}


@router.get("/mr/{project_id}/{mr_iid}/stats")
async def mr_stats(project_id: int, mr_iid: int) -> dict[str, Any]:
    return get_store().suggestion_stats(project_id, mr_iid)


@router.get("/mr/{project_id}/{mr_iid}/timeline")
async def mr_timeline(project_id: int, mr_iid: int, limit: int = Query(200, ge=1, le=1000)) -> dict[str, Any]:
    s = get_store()
    with s._conn() as conn:  # noqa: SLF001
        rows = conn.execute(
            "SELECT started_at AS at, 'run' AS event_type, id AS event_id, command AS detail, status AS state "
            "FROM review_runs WHERE project_id=? AND mr_iid=? "
            "UNION ALL SELECT created_at, 'suggestion_action', id, action, validation_status "
            "FROM suggestion_actions WHERE project_id=? AND mr_iid=? "
            "UNION ALL SELECT created_at, 'suggestion_posted', id, file_path, state "
            "FROM suggestions WHERE project_id=? AND mr_iid=? "
            "ORDER BY at DESC LIMIT ?",
            (project_id, mr_iid, project_id, mr_iid, project_id, mr_iid, limit),
        ).fetchall()
    return {"events": [dict(row) for row in rows]}


@router.get("/mrs")
async def list_mrs(
    project_id: int | None = Query(None), state: str | None = Query(None),
    since: str | None = Query(None), limit: int = Query(100, ge=1, le=1000),
) -> dict[str, Any]:
    s = get_store()
    clauses, params = [], []
    if project_id is not None:
        clauses.append("project_id = ?"); params.append(project_id)
    if state:
        clauses.append("state = ?"); params.append(state)
    if since:
        clauses.append("updated_at >= ?"); params.append(_parse_iso(since))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with s._conn() as conn:  # noqa: SLF001
        rows = conn.execute(f"SELECT * FROM mr_activity{where} ORDER BY updated_at DESC LIMIT ?", (*params, limit)).fetchall()
    return {"total": len(rows), "mrs": [dict(row) for row in rows]}


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
async def metrics_rules(project_id: int | None = Query(None), since: str | None = Query(None), until: str | None = Query(None)) -> dict[str, Any]:
    # 当前 suggestion schema 没有 rule_key；按 severity 返回稳定的前端兼容分组。
    metrics = get_store().suggestion_metrics(project_id=project_id, since=_parse_iso(since), until=_parse_iso(until))
    return {"rules": [{"rule_key": key, "count": count} for key, count in metrics["severity_counts"].items()]}


@router.get("/metrics/authors")
async def metrics_authors(project_id: int | None = Query(None), since: str | None = Query(None), until: str | None = Query(None)) -> dict[str, Any]:
    s = get_store()
    clauses, params = ["1=1"], []
    if project_id is not None:
        clauses.append("project_id = ?"); params.append(project_id)
    if since:
        clauses.append("updated_at >= ?"); params.append(_parse_iso(since))
    if until:
        clauses.append("updated_at < ?"); params.append(_parse_iso(until))
    with s._conn() as conn:  # noqa: SLF001
        rows = conn.execute(
            f"SELECT COALESCE(NULLIF(author_sticky,''),'unknown') AS author, COUNT(*) AS runs "
            f"FROM mr_activity WHERE {' AND '.join(clauses)} GROUP BY author ORDER BY runs DESC",
            params,
        ).fetchall()
    return {"authors": [dict(row) for row in rows]}


@router.get("/dismissals")
async def dismissals(
    project_id: int | None = Query(None), mr_iid: int | None = Query(None), rule_key: str | None = Query(None),
    since: str | None = Query(None), limit: int = Query(500, ge=1, le=5000),
) -> dict[str, Any]:
    # rule_key is accepted for pr-agent dashboard compatibility; ReviewAgent stores severity instead.
    rows = get_store().list_suggestion_actions(project_id=project_id, mr_iid=mr_iid, action="dismissed", since=_parse_iso(since), limit=limit)
    if rule_key:
        rows = [r for r in rows if r.get("file_path") == rule_key or r.get("reason") == rule_key]
    return {"total": len(rows), "dismissals": rows}
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
        if project_id and f"-{project_id}-" not in p.name and "project_id" in p.read_text(errors='replace')[:200]:
            pass
        files.append({"name": p.name, "path": str(p), "size": p.stat().st_size,
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

