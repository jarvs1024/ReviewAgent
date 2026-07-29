"""Telemetry API 路由 — 暴露 reviewagent 数据采集.

端点:
    GET /api/v1/telemetry/health           - 健康检查
    GET /api/v1/telemetry/runs              - 列出 run (分页 + 多条件过滤)
    GET /api/v1/telemetry/runs/{id}         - 单 run 详情
    GET /api/v1/telemetry/mr/{proj}/{iid}   - MR 元信息 + run 历史
    GET /api/v1/telemetry/summary           - 聚合统计 (按 command/status/day/MR)
"""
from __future__ import annotations

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


@router.get("/mr/{project_id}/{mr_iid}")
async def mr_detail(project_id: int, mr_iid: int) -> dict[str, Any]:
    s = get_store()
    mr = s.get_mr(project_id, mr_iid)
    if not mr:
        raise HTTPException(404, f"MR not found: {project_id}/{mr_iid}")
    runs = s.list_runs(project_id=project_id, mr_iid=mr_iid, limit=50)
    return {"mr": mr, "recent_runs": runs}


@router.get("/summary")
async def summary(
    since: str | None = Query(None, description="起始 ISO 时间"),
    until: str | None = Query(None, description="结束 ISO 时间 (exclusive)"),
) -> dict[str, Any]:
    s = get_store()
    return s.summary(since=_parse_iso(since), until=_parse_iso(until))
