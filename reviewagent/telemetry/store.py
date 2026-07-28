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


# ---------- 工具 ----------
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _fmt_dt(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()