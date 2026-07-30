"""SQLite 持久化 — WAL 模式 + 薄 DAO.

表:
    mr_activity: MR 元信息快照（每次活动更新）
    review_runs: 每次检视任务执行记录
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from reviewagent.config import config
from reviewagent.logging_setup import logger
from reviewagent.telemetry.models import MRRecord, ReviewRun


# ---------- DDL ----------
_DDL = """
CREATE TABLE IF NOT EXISTS mr_activity (
    project_id          INTEGER NOT NULL,
    mr_iid              INTEGER NOT NULL,
    title               TEXT,
    author_username     TEXT,
    author_sticky       TEXT,
    source_branch       TEXT,
    target_branch       TEXT,
    state               TEXT,
    created_at          TIMESTAMP,
    updated_at          TIMESTAMP,
    merged_at           TIMESTAMP,
    description_generated INTEGER DEFAULT 0,
    last_review_at      TIMESTAMP,
    PRIMARY KEY (project_id, mr_iid)
);

CREATE TABLE IF NOT EXISTS review_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id          INTEGER NOT NULL,
    mr_iid              INTEGER NOT NULL,
    command             TEXT NOT NULL,
    triggered_by        TEXT NOT NULL,
    actor_username      TEXT,
    started_at          TIMESTAMP NOT NULL,
    finished_at         TIMESTAMP,
    status              TEXT NOT NULL,
    error               TEXT,
    model               TEXT,
    prompt_tokens       INTEGER DEFAULT 0,
    completion_tokens   INTEGER DEFAULT 0,
    total_tokens        INTEGER DEFAULT 0,
    duration_ms         INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_runs_project_mr ON review_runs(project_id, mr_iid);
CREATE INDEX IF NOT EXISTS idx_runs_started ON review_runs(started_at);
CREATE INDEX IF NOT EXISTS idx_mr_state ON mr_activity(state);

-- suggestion_actions: /adopt /dismiss 事件追踪
CREATE TABLE IF NOT EXISTS suggestion_actions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id          INTEGER NOT NULL,
    mr_iid              INTEGER NOT NULL,
    suggestion_note_id  TEXT NOT NULL,         -- GitLab discussion/note id (字符串)
    file_path           TEXT,
    target_line         INTEGER,
    action              TEXT NOT NULL,         -- 'adopted' | 'dismissed'
    actor_username      TEXT,
    reason              TEXT,
    validation_status   TEXT,                  -- /adopt: ok / target-unchanged / content-unavailable etc.
    head_sha_posted     TEXT,                  -- /adopt: suggestion 发布时的 head_sha
    head_sha_current    TEXT,                  -- /adopt: 当前 head_sha
    created_at          TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_actions_project_mr ON suggestion_actions(project_id, mr_iid);
CREATE INDEX IF NOT EXISTS idx_actions_suggestion ON suggestion_actions(suggestion_note_id);

-- suggestions: 记录 improve 发布的每条 suggestion (用于 /adopt 验证)
CREATE TABLE IF NOT EXISTS suggestions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id          INTEGER NOT NULL,
    mr_iid              INTEGER NOT NULL,
    note_id             TEXT NOT NULL,         -- GitLab discussion/note id (字符串)
    file_path           TEXT NOT NULL,
    target_line         INTEGER NOT NULL,
    target_line_end     INTEGER,               -- 多行替换时的结束行
    existing_code       TEXT,                  -- 原文 (用于 /adopt 验证匹配)
    improved_code       TEXT,
    header              TEXT,
    severity            TEXT,
    head_sha            TEXT NOT NULL,         -- 发布时的 head_sha
    state               TEXT DEFAULT 'open',   -- open / applied / dismissed / superseded
    applied_at          TIMESTAMP,
    dismissed_at        TIMESTAMP,
    dismissed_by        TEXT,
    created_at          TIMESTAMP NOT NULL,
    updated_at          TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_sug_project_mr ON suggestions(project_id, mr_iid);
CREATE INDEX IF NOT EXISTS idx_sug_note_id ON suggestions(note_id);
CREATE INDEX IF NOT EXISTS idx_sug_state ON suggestions(state);
"""


# ---------- 单例 ----------
_store: "Store | None" = None


def get_store() -> "Store":
    global _store
    if _store is None:
        _store = Store(config.sqlite_path)
    return _store


# ---------- Store 类 ----------
class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        logger.info("telemetry.store init path={}", path)

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(_DDL)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(suggestions)")}
            migrations = {
                "applied_at": "ALTER TABLE suggestions ADD COLUMN applied_at TIMESTAMP",
                "dismissed_at": "ALTER TABLE suggestions ADD COLUMN dismissed_at TIMESTAMP",
                "dismissed_by": "ALTER TABLE suggestions ADD COLUMN dismissed_by TEXT",
            }
            for column, sql in migrations.items():
                if column not in columns:
                    conn.execute(sql)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(
            str(self.path),
            timeout=10,
            isolation_level=None,  # autocommit; 我们用显式 BEGIN
            check_same_thread=False,
        )
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.row_factory = sqlite3.Row
            yield conn
        finally:
            conn.close()

    # ---------- MR ----------
    def upsert_mr(self, mr: MRRecord) -> None:
        """插入或更新 MR；保留已有 author_sticky（不被覆盖）."""
        with self._conn() as conn:
            conn.execute("BEGIN")
            try:
                conn.execute(
                    """
                    INSERT INTO mr_activity (
                        project_id, mr_iid, title, author_username,
                        source_branch, target_branch, state,
                        created_at, updated_at, merged_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(project_id, mr_iid) DO UPDATE SET
                        title = excluded.title,
                        source_branch = excluded.source_branch,
                        target_branch = excluded.target_branch,
                        state = excluded.state,
                        updated_at = excluded.updated_at,
                        merged_at = COALESCE(excluded.merged_at, mr_activity.merged_at)
                    """,
                    (
                        mr.project_id, mr.mr_iid, mr.title, mr.author_username,
                        mr.source_branch, mr.target_branch, mr.state,
                        _fmt_dt(mr.created_at), _fmt_dt(mr.updated_at), _fmt_dt(mr.merged_at),
                    ),
                )
                conn.execute(
                    """
                    UPDATE mr_activity
                    SET author_sticky = COALESCE(author_sticky, author_username)
                    WHERE project_id = ? AND mr_iid = ?
                    """,
                    (mr.project_id, mr.mr_iid),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def mark_description_generated(self, project_id: int, mr_iid: int) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE mr_activity
                SET description_generated = 1, last_review_at = ?
                WHERE project_id = ? AND mr_iid = ?
                """,
                (_fmt_dt(_utcnow()), project_id, mr_iid),
            )

    def get_mr(self, project_id: int, mr_iid: int) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM mr_activity WHERE project_id = ? AND mr_iid = ?",
                (project_id, mr_iid),
            ).fetchone()
            return dict(row) if row else None

    # ---------- Review Run ----------
    def insert_run(self, run: ReviewRun) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO review_runs (
                    project_id, mr_iid, command, triggered_by, actor_username,
                    started_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.project_id, run.mr_iid, run.command, run.triggered_by,
                    run.actor_username, _fmt_dt(run.started_at), run.status,
                ),
            )
            return cur.lastrowid

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        error: str | None = None,
        model: str | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        duration_ms: int = 0,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE review_runs
                SET finished_at = ?, status = ?, error = ?, model = ?,
                    prompt_tokens = ?, completion_tokens = ?,
                    total_tokens = ?, duration_ms = ?
                WHERE id = ?
                """,
                (
                    _fmt_dt(_utcnow()), status, error, model,
                    prompt_tokens, completion_tokens,
                    prompt_tokens + completion_tokens, duration_ms,
                    run_id,
                ),
            )

    # ---------- Suggestions (/adopt /dismiss 数据采集) ----------
    def record_suggestion(
        self,
        *,
        project_id: int,
        mr_iid: int,
        note_id: str,
        file_path: str,
        target_line: int,
        target_line_end: int | None = None,
        existing_code: str | None = None,
        improved_code: str | None = None,
        header: str | None = None,
        severity: str | None = None,
        head_sha: str,
    ) -> int:
        """记录 improve 发布的一条 inline suggestion (用于后续 /adopt 验证)."""
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO suggestions (
                    project_id, mr_iid, note_id, file_path,
                    target_line, target_line_end,
                    existing_code, improved_code,
                    header, severity, head_sha,
                    state, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
                """,
                (
                    project_id, mr_iid, note_id, file_path,
                    target_line, target_line_end,
                    existing_code, improved_code,
                    header, severity, head_sha,
                    _fmt_dt(_utcnow()),
                ),
            )
            return int(cur.lastrowid or 0)

    def get_suggestion_by_note_id(self, note_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM suggestions WHERE note_id = ? ORDER BY id DESC LIMIT 1",
                (note_id,),
            ).fetchone()
            return dict(row) if row else None

    def update_suggestion_state(
        self,
        note_id: str,
        state: str,
        *,
        actor_username: str | None = None,
    ) -> None:
        """标记 suggestion 为 applied / dismissed / superseded."""
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE suggestions
                SET state = ?, updated_at = ?,
                    applied_at = CASE WHEN ? = 'applied' THEN ? ELSE applied_at END,
                    dismissed_at = CASE WHEN ? = 'dismissed' THEN ? ELSE dismissed_at END,
                    dismissed_by = CASE WHEN ? = 'dismissed' THEN ? ELSE dismissed_by END
                WHERE note_id = ?
                """,
                (state, _fmt_dt(_utcnow()), state, _fmt_dt(_utcnow()),
                 state, _fmt_dt(_utcnow()), state, actor_username, note_id),
            )

    def list_suggestions(
        self, *, project_id: int | None = None, mr_iid: int | None = None,
        state: str | None = None, since: str | None = None,
        until: str | None = None, limit: int = 100, offset: int = 0,
    ) -> list[dict]:
        clauses, params = [], []
        for field, value in (("project_id", project_id), ("mr_iid", mr_iid), ("state", state)):
            if value is not None:
                clauses.append(f"{field} = ?"); params.append(value)
        if since:
            clauses.append("created_at >= ?"); params.append(since)
        if until:
            clauses.append("created_at < ?"); params.append(until)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM suggestions{where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return [dict(row) for row in rows]

    def suggestion_stats(self, project_id: int, mr_iid: int) -> dict:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT state, COUNT(*) AS n FROM suggestions WHERE project_id=? AND mr_iid=? GROUP BY state",
                (project_id, mr_iid),
            ).fetchall()
            actions = conn.execute(
                "SELECT action, COUNT(*) AS n FROM suggestion_actions WHERE project_id=? AND mr_iid=? GROUP BY action",
                (project_id, mr_iid),
            ).fetchall()
            severities = conn.execute(
                "SELECT COALESCE(NULLIF(severity,''),'unspecified') AS severity, COUNT(*) AS n "
                "FROM suggestions WHERE project_id=? AND mr_iid=? GROUP BY severity",
                (project_id, mr_iid),
            ).fetchall()
        states = {row["state"]: row["n"] for row in rows}
        action_counts = {row["action"]: row["n"] for row in actions}
        total = sum(states.values())
        adopted = states.get("applied", 0)
        dismissed = states.get("dismissed", 0)
        return {
            "total": total, "state_counts": states,
            "action_counts": action_counts,
            "severity_counts": {row["severity"]: row["n"] for row in severities},
            "adopted": adopted, "dismissed": dismissed,
            "open": states.get("open", 0),
            "adoption_rate": round(adopted / (adopted + dismissed) * 100, 1) if adopted + dismissed else 0.0,
        }

    def suggestion_metrics(self, *, project_id: int | None = None,
                           since: str | None = None, until: str | None = None) -> dict:
        suggestions = self.list_suggestions(project_id=project_id, since=since, until=until, limit=100000)
        by_state, by_severity = {}, {}
        for row in suggestions:
            by_state[row["state"]] = by_state.get(row["state"], 0) + 1
            severity = row.get("severity") or "unspecified"
            by_severity[severity] = by_severity.get(severity, 0) + 1
        actions = self.list_suggestion_actions(project_id=project_id, since=since, until=until, limit=100000)
        by_action = {}
        for row in actions:
            by_action[row["action"]] = by_action.get(row["action"], 0) + 1
        adopted = by_state.get("applied", 0)
        dismissed = by_state.get("dismissed", 0)
        return {"total": len(suggestions), "state_counts": by_state,
                "severity_counts": by_severity, "action_counts": by_action,
                "adopted": adopted, "dismissed": dismissed,
                "adoption_rate": round(adopted / (adopted + dismissed) * 100, 1) if adopted + dismissed else 0.0}

    def record_suggestion_action(
        self,
        *,
        project_id: int,
        mr_iid: int,
        suggestion_note_id: str,
        file_path: str | None = None,
        target_line: int | None = None,
        action: str,
        actor_username: str | None = None,
        reason: str | None = None,
        validation_status: str | None = None,
        head_sha_posted: str | None = None,
        head_sha_current: str | None = None,
    ) -> int:
        """记录 /adopt /dismiss 事件."""
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO suggestion_actions (
                    project_id, mr_iid, suggestion_note_id, file_path, target_line,
                    action, actor_username, reason,
                    validation_status, head_sha_posted, head_sha_current,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id, mr_iid, suggestion_note_id, file_path, target_line,
                    action, actor_username, reason,
                    validation_status, head_sha_posted, head_sha_current,
                    _fmt_dt(_utcnow()),
                ),
            )
            return int(cur.lastrowid or 0)

    def list_suggestion_actions(
        self,
        *,
        project_id: int | None = None,
        mr_iid: int | None = None,
        action: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """列出 suggestion action 事件 (用于周报/dashboard)."""
        clauses: list[str] = []
        params: list = []
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        if mr_iid is not None:
            clauses.append("mr_iid = ?")
            params.append(mr_iid)
        if action:
            clauses.append("action = ?")
            params.append(action)
        if since:
            clauses.append("created_at >= ?")
            params.append(since)
        if until:
            clauses.append("created_at < ?")
            params.append(until)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM suggestion_actions {where} ORDER BY id DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- 查询 (Phase 3 数据采集用) ----------
    def list_runs(
        self,
        *,
        project_id: int | None = None,
        mr_iid: int | None = None,
        since: str | None = None,
        until: str | None = None,
        command: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """列出 run 记录，支持多种过滤.

        Args:
            project_id: 按项目过滤
            mr_iid: 按 MR 过滤 (需 project_id 同时)
            since: ISO 时间下界 (e.g. '2026-07-21' 或 '2026-07-21T00:00:00Z')
            until: ISO 时间上界
            command: describe/review/improve
            status: running/success/failed/timeout
            limit: 默认 100
            offset: 分页
        """
        clauses: list[str] = []
        params: list = []
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        if mr_iid is not None:
            clauses.append("mr_iid = ?")
            params.append(mr_iid)
        if since:
            clauses.append("started_at >= ?")
            params.append(since)
        if until:
            clauses.append("started_at < ?")
            params.append(until)
        if command:
            clauses.append("command = ?")
            params.append(command)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            "SELECT * FROM review_runs" + where +
            " ORDER BY started_at DESC LIMIT ? OFFSET ?"
        )
        params.extend([limit, offset])
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def summary(
        self,
        *,
        since: str | None = None,
        until: str | None = None,
    ) -> dict:
        """聚合统计 — 命令维度 / 状态维度 / 性能维度.

        Returns:
            {
              "since": str, "until": str,
              "total_runs": int,
              "by_command": {cmd: {count, success, failed, avg_duration_ms, total_tokens}},
              "by_status": {status: count},
              "by_day": {YYYY-MM-DD: count},
              "top_mrs": [{project_id, mr_iid, runs}, ...]
            }
        """
        clauses: list[str] = []
        params: list = []
        if since:
            clauses.append("started_at >= ?")
            params.append(since)
        if until:
            clauses.append("started_at < ?")
            params.append(until)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

        with self._conn() as conn:
            # 1. by_command — 一次 group by 出整体 + 各 status 计数
            by_command: dict[str, dict] = {}
            by_status: dict[str, int] = {}
            rows = conn.execute(
                f"SELECT command, COUNT(*) as n, "
                f"AVG(duration_ms) as avg_dur, "
                f"SUM(total_tokens) as tokens "
                f"FROM review_runs {where} GROUP BY command",
                params,
            ).fetchall()
            for r in rows:
                cmd = r["command"]
                by_command[cmd] = {
                    "count": r["n"],
                    "success": 0, "failed": 0, "timeout": 0, "running": 0,
                    "avg_duration_ms": int(r["avg_dur"] or 0),
                    "total_tokens": int(r["tokens"] or 0),
                }
            rows = conn.execute(
                f"SELECT command, status, COUNT(*) as n "
                f"FROM review_runs {where} GROUP BY command, status",
                params,
            ).fetchall()
            for r in rows:
                cmd, st, n = r["command"], r["status"], r["n"]
                if cmd in by_command and st in by_command[cmd]:
                    by_command[cmd][st] = n
                by_status[st] = by_status.get(st, 0) + n

            # 2. by_day
            rows = conn.execute(
                f"SELECT substr(started_at, 1, 10) as day, COUNT(*) as n "
                f"FROM review_runs {where} GROUP BY day ORDER BY day",
                params,
            ).fetchall()
            by_day = {r["day"]: r["n"] for r in rows}

            # 3. top_mrs
            rows = conn.execute(
                f"SELECT project_id, mr_iid, COUNT(*) as runs "
                f"FROM review_runs {where} "
                f"GROUP BY project_id, mr_iid "
                f"ORDER BY runs DESC LIMIT 10",
                params,
            ).fetchall()
            top_mrs = [
                {"project_id": r["project_id"], "mr_iid": r["mr_iid"], "runs": r["runs"]}
                for r in rows
            ]

            # 4. total
            total = conn.execute(
                f"SELECT COUNT(*) as n FROM review_runs {where}", params
            ).fetchone()["n"]

        return {
            "since": since,
            "until": until,
            "total_runs": total,
            "by_command": by_command,
            "by_status": by_status,
            "by_day": by_day,
            "top_mrs": top_mrs,
        }


# ---------- 工具 ----------
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _fmt_dt(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()
