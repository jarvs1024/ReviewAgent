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
from reviewagent.gitlab.client import client as _gl
from reviewagent.logging_setup import logger
from reviewagent.telemetry.models import MRRecord, ReviewRun


def _enrich_web_url(row: dict) -> dict:
    """给 MR dict 加 web_url 字段 (调 GitLab API 拿 path_with_namespace).

    失败时静默跳过 (不影响主体查询). 项目级缓存, 同一项目内多次调只打一次 API.
    """
    try:
        url = _gl.get_mr_web_url(row["project_id"], row["mr_iid"])
    except Exception as e:  # noqa: BLE001 — 不让 enrich 失败影响主路径
        logger.debug("store._enrich_web_url failed: {}", e)
        url = None
    if url:
        row["web_url"] = url
    return row


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



def _norm_iso(s: str | None) -> str | None:
    """归一化 ISO 时间字符串 — DB created_at 是 UTC "+00:00" 格式, SQLite 字符串
    比较走字典序, 跟 "+08:00" 等格式混用会错位. 统一转 UTC 后给 SQLite datetime() 比较."""
    if not s:
        return s
    try:
        from datetime import datetime, timezone as _tz
        norm = s.replace("Z", "+00:00") if s.endswith("Z") else s
        dt = datetime.fromisoformat(norm)
        if dt.tzinfo is None:
            # naive 当作 UTC (DB 写入路径都是 UTC)
            dt = dt.replace(tzinfo=_tz.utc)
        # 输出 SQLite datetime() 兼容的 "YYYY-MM-DD HH:MM:SS" UTC
        return dt.astimezone(_tz.utc).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return s  # 解析失败就原样返回, 不让边界失效


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
                "dismissed_reason": "ALTER TABLE suggestions ADD COLUMN dismissed_reason TEXT",
                "rule_keys": "ALTER TABLE suggestions ADD COLUMN rule_keys TEXT",
                "one_sentence_summary": "ALTER TABLE suggestions ADD COLUMN one_sentence_summary TEXT",
                "importance": "ALTER TABLE suggestions ADD COLUMN importance INTEGER",
                "score": "ALTER TABLE suggestions ADD COLUMN score INTEGER",
                "fingerprint": "ALTER TABLE suggestions ADD COLUMN fingerprint TEXT",
                "cohort_key": "ALTER TABLE suggestions ADD COLUMN cohort_key TEXT",
                "severity_source": "ALTER TABLE suggestions ADD COLUMN severity_source TEXT",
                "label": "ALTER TABLE suggestions ADD COLUMN label TEXT",
                "posted_at": "ALTER TABLE suggestions ADD COLUMN posted_at TIMESTAMP",
            }
            for column, sql in migrations.items():
                if column not in columns:
                    conn.execute(sql)
            run_columns = {row[1] for row in conn.execute("PRAGMA table_info(review_runs)")}
            for column, sql in {
                "triggered_by": "ALTER TABLE review_runs ADD COLUMN triggered_by TEXT",
                "rule_keys_cited": "ALTER TABLE review_runs ADD COLUMN rule_keys_cited TEXT",
                "suggestion_count": "ALTER TABLE review_runs ADD COLUMN suggestion_count INTEGER DEFAULT 0",
            }.items():
                if column not in run_columns:
                    conn.execute(sql)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sug_cohort ON suggestions(mr_iid, cohort_key)"
            )

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
        if not row:
            return None
        return _enrich_web_url(dict(row))

    def list_mrs(self, *, project_id: int | None = None, since: str | None = None,
                 until: str | None = None, state: str | None = None,
                 limit: int = 100) -> list[dict]:
        """列出 MR (按 project/时间/state 筛选), 用于周报窗口统计."""
        clauses, params = [], []
        if project_id is not None:
            clauses.append("project_id = ?"); params.append(project_id)
        if since:
            clauses.append("datetime(created_at) >= datetime(?)"); params.append(_norm_iso(since))
        if until:
            clauses.append("datetime(created_at) < datetime(?)"); params.append(_norm_iso(until))
        if state:
            clauses.append("state = ?"); params.append(state)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM mr_activity{where} ORDER BY created_at DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [_enrich_web_url(dict(r)) for r in rows]

    def mr_overview(self, *, project_id: int | None = None, since: str | None = None,
                    until: str | None = None) -> dict:
        """PR-Agent 风格的 MR 概览: {total, opened, closed, merged, window_count}."""
        clauses, params = [], []
        if project_id is not None:
            clauses.append("project_id = ?"); params.append(project_id)
        if since:
            clauses.append("datetime(created_at) >= datetime(?)"); params.append(_norm_iso(since))
        if until:
            clauses.append("datetime(created_at) < datetime(?)"); params.append(_norm_iso(until))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT state, COUNT(*) AS n FROM mr_activity{where} GROUP BY state", params
            ).fetchall()
        counts = {"total": 0, "opened": 0, "closed": 0, "merged": 0}
        for r in rows:
            st = r["state"] or ""
            counts[st] = counts.get(st, 0) + r["n"]
            counts["total"] += r["n"]
        window_count = counts.get("opened", 0) + counts.get("closed", 0) + counts.get("merged", 0)
        return {**counts, "window_count": window_count}

    def rule_key_counts(self, *, project_id: int | None = None, since: str | None = None,
                       until: str | None = None, top_n: int = 5) -> list[tuple[str, int]]:
        """统计 suggestion 触发最多的规则 (按 rule_keys 拆分后计数)."""
        clauses, params = [], []
        if project_id is not None:
            clauses.append("s.project_id = ?"); params.append(project_id)
        if since:
            clauses.append("datetime(s.created_at) >= datetime(?)"); params.append(_norm_iso(since))
        if until:
            clauses.append("datetime(s.created_at) < datetime(?)"); params.append(_norm_iso(until))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT rule_keys FROM suggestions s{where}", params,
            ).fetchall()
        bucket: dict[str, int] = {}
        for r in rows:
            for k in (r["rule_keys"] or "").split(","):
                k = k.strip()
                if k:
                    bucket[k] = bucket.get(k, 0) + 1
        ranked = sorted(bucket.items(), key=lambda x: (-x[1], x[0]))
        return ranked[:top_n]

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

    def save_agent_output_fail(self, text: str, agent: str) -> None:
        """保存 agent 失败时的输出前 500 字符到 agent_failures 表（用于调试）."""
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_failures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    agent TEXT,
                    text_preview TEXT
                )
                """
            )
            conn.execute(
                "INSERT INTO agent_failures (agent, text_preview) VALUES (?, ?)",
                (agent, text[:500]),
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
        rule_keys: list[str] | None = None,
        one_sentence_summary: str | None = None,
        importance: int | None = None,
        label: str | None = None,
        fingerprint: str | None = None,
        cohort_key: str | None = None,
        severity_source: str | None = None,
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
                    rule_keys, one_sentence_summary, importance, label,
                    fingerprint, cohort_key, severity_source, posted_at,
                    state, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
                """,
                (
                    project_id, mr_iid, note_id, file_path,
                    target_line, target_line_end,
                    existing_code, improved_code,
                    header, severity, head_sha,
                    ",".join(rule_keys or []),
                    one_sentence_summary,
                    importance,
                    label,
                    fingerprint,
                    cohort_key,
                    severity_source,
                    _fmt_dt(_utcnow()),
                    _fmt_dt(_utcnow()),
                ),
            )
            return int(cur.lastrowid or 0)

    def suggestion_exists_by_fingerprint(
        self, project_id: int, mr_iid: int, fingerprint: str
    ) -> bool:
        """跨次去重: 同一 (project, mr, fingerprint) 已发布则返回 True."""
        if not fingerprint:
            return False
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM suggestions "
                "WHERE project_id=? AND mr_iid=? AND fingerprint=? LIMIT 1",
                (project_id, mr_iid, fingerprint),
            ).fetchone()
            return row is not None

    def suggestion_exists_at_line(
        self,
        project_id: int,
        mr_iid: int,
        file_path: str,
        target_line: int,
        severity: str = "",  # 保留参数仅为向后兼容, 当前实现不再按 severity 过滤
        head_sha: str = "",
        line_tolerance: int = 0,
        rule_keys: str | None = None,  # 逗号分隔字符串, 命中任一即视为同规则 dedup
    ) -> bool:
        """跨次去重 (heuristic): 同 (file, line[±tolerance][, head_sha]) 已发布则返回 True.

        Why: LLM 每次返回的 existing_code 范围不一致 (有时 1 行签名, 有时
        整段函数体), 同一 bug 在不同次 improve 跑出来的 fingerprint 不一样,
        导致纯 fingerprint dedup 命中率低. 用 (file, line) 做兜底:
        同一行任意 severity 的重复建议都视为同一 bug, 直接跳过 — 用户已经
        检视过这一行了, 不管严重度如何都不该重复推送 (避免 LLM 改判 severity
        后绕开 dedup).

        head_sha 维度: MR 被 force-push / reset 到旧 commit 后, 老建议的
        head_sha 跟当前 diff 的 head_sha 不一致, 此时 dedup 应该放行, 让
        bot 重新发现这些 bug. 调用方传 head_sha='' 时退回旧的 (file, line)
        兜底行为.

        line_tolerance 维度 (默认 2): LLM 跨次 improve 容易出现 ±1~3 行的
        position 漂移 (行号指 def 行而非真实出错行 / 数错 @@ 偏移). 加 ±N
        行容差后, 同一 head 下 (file, line±N) 已检视过则视为重复 — 比强制
        LLM 给精确行号更可靠. 设为 0 = 严格相等.

        - 状态过滤: 任何状态都视为"已存在" — dismissed 也算, 用户已经明确
          拒绝过这条建议, 不应该重复推送骚扰.

        rule_keys 维度 (None = 兼容旧行为): 调用方传入新建议的 rule_keys
        (逗号分隔字符串, 如 "SSD-RULE-NO-MUTABLE-DEFAULT,SSD-RULE-TYPEHINTS"),
        若已有建议 rule_keys 与传入 rule_keys 有任一重叠 (LIKE 匹配), 才
        视为同规则 dedup. **完全不同的规则即使在同 line ±tolerance 内也
        不应误杀** (例: SSD-RULE-NO-MUTABLE-DEFAULT L10 与
        SSD-RULE-NO-LOG-EXC L12 是两条独立建议, 不应 dedup).
        """
        del severity  # 静默未使用, 保持向后兼容的调用签名
        lo = target_line - max(0, line_tolerance)
        hi = target_line + max(0, line_tolerance)
        # dedup 策略: 跨 head_sha 共享 (file, line) dedup, 但只看 state=open.
        # 已 applied / dismissed 的视为"已处理", 允许重新检视
        # (例: 用户 push 改了内容让 auto_detect 标 applied, 然后又撤回
        # 原始内容 → 系统应能重新检视出新 issue).
        # Why: 之前用 head_sha 限定导致跨 V dedup 失效 (V1 V2 V3 同一 file:line
        # 都会重新发, 引起 GitLab 重复评论).
        with self._conn() as conn:
            # 基础 (file, line, state=open) 过滤
            base_sql = (
                "SELECT 1 FROM suggestions "
                "WHERE project_id=? AND mr_iid=? "
                "  AND file_path=? AND target_line BETWEEN ? AND ? "
                "  AND state='open'"
            )
            base_params: list = [project_id, mr_iid, file_path, lo, hi]
            # rule_keys 比对策略 (2 选 1):
            #   A) 已有建议 rule_keys 为空/None (旧数据 / 未分类) → 视为 dedup 命中
            #      (兼容旧 dedup 行为, 避免新规则绕过旧建议)
            #   B) 已有建议 rule_keys 与新建议 rule_keys 任一重叠 → 视为 dedup 命中
            #      (用 ',<rk>,' 包裹 LIKE 避免前缀误匹配, 如 SSD-RULE-NO-LOG
            #      不能误命中 SSD-RULE-NO-LOG-EXC)
            rk_clauses = (
                " AND ("
                "(COALESCE(rule_keys,'') = '')"          # 情况 A: 旧数据
                " OR "
                "(" + " OR ".join(
                    "(',' || COALESCE(rule_keys,'') || ',') LIKE ?"
                    for _ in (rule_keys.split(",") if rule_keys else [])
                    if _.strip()
                ) + ")"
                ")"
            )
            rk_params = []
            if rule_keys:
                rks = [rk.strip() for rk in rule_keys.split(",") if rk.strip()]
                rk_params = [f"%,{rk},%" for rk in rks]
            # 没有 rks 时, 第二个 OR 内空, SQL 变成 "... OR ()" → SQLite 不允许
            # 退化处理: 没传 rule_keys 时直接走 (file, line) 兜底, 不加 rule_keys 子句
            if not rk_params:
                row = conn.execute(base_sql + " LIMIT 1", base_params).fetchone()
            else:
                row = conn.execute(base_sql + rk_clauses + " LIMIT 1", base_params + rk_params).fetchone()
        if head_sha:  # 保留参数以避免破坏调用方, 但不使用
            pass
            return row is not None

    def list_suggestion_headers(
        self, project_id: int, mr_iid: int
    ) -> list[dict[str, Any]]:
        """给 agent 看的"已发过建议"列表 (用于 prompt 注入, 避免 agent 重复提).

        只返回轻量级字段 (不返回完整 diff) 节省 token:
          - file, line (用于 agent 判断是否已覆盖)
          - header (用于 agent 判断是否是同类问题)
          - severity / status
          - existing_code_hash (fingerprint 前 8 位, 用于完全匹配判定)
        """
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT file_path, target_line, header, severity, status,
                       substr(fingerprint, 1, 8) AS fp_short
                FROM suggestions
                WHERE project_id = ? AND mr_iid = ?
                ORDER BY target_line, file_path
                """,
                (project_id, mr_iid),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_suggestion_by_note_id(self, note_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM suggestions WHERE note_id = ? ORDER BY id DESC LIMIT 1",
                (note_id,),
            ).fetchone()
            return dict(row) if row else None

    def find_open_suggestion_by_line(
        self,
        *,
        project_id: int,
        mr_iid: int,
        file_path: str,
        target_line: int,
        window: int = 3,
    ) -> dict | None:
        """找同 file:line 处 state='open' 的 suggestion, 容忍 ±window 行偏移.

        GitLab UI "Apply suggestion" 的 system DiffNote 报的行号偶尔会因 diff
        重排轻微偏移几行, 严格相等匹配容易漏掉; 加 window=3 兜底.
        """
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT * FROM suggestions
                WHERE project_id = ? AND mr_iid = ?
                  AND file_path = ?
                  AND state = 'open'
                  AND target_line BETWEEN ? AND ?
                ORDER BY ABS(target_line - ?) ASC, id DESC
                LIMIT 1
                """,
                (project_id, mr_iid, file_path,
                 target_line - window, target_line + window,
                 target_line),
            ).fetchone()
            return dict(row) if row else None

    def list_open_suggestions(
        self,
        *,
        project_id: int,
        mr_iid: int,
    ) -> list[dict]:
        """列出该 MR 全部 state=open 的 suggestions (给 auto_detect_applied 用).

        Returns: list of dict with note_id, file_path, target_line,
        target_line_end, existing_code, improved_code, head_sha.
        """
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT note_id, file_path, target_line, target_line_end,
                       existing_code, improved_code, head_sha
                FROM suggestions
                WHERE project_id=? AND mr_iid=? AND state='open'
                ORDER BY id
                """,
                (project_id, mr_iid),
            ).fetchall()
            return [dict(r) for r in rows]

    def update_suggestion_state(
        self,
        note_id: str,
        state: str,
        *,
        actor_username: str | None = None,
        dismissed_reason: str | None = None,
    ) -> None:
        """标记 suggestion 为 applied / dismissed / superseded."""
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE suggestions
                SET state = ?, updated_at = ?,
                    applied_at = CASE WHEN ? = 'applied' THEN ? ELSE applied_at END,
                    dismissed_at = CASE WHEN ? = 'dismissed' THEN ? ELSE dismissed_at END,
                    dismissed_by = CASE WHEN ? = 'dismissed' THEN ? ELSE dismissed_by END,
                    dismissed_reason = CASE WHEN ? = 'dismissed' AND ? IS NOT NULL
                                            THEN ? ELSE dismissed_reason END
                WHERE note_id = ?
                """,
                (state, _fmt_dt(_utcnow()),
                 state, _fmt_dt(_utcnow()),
                 state, _fmt_dt(_utcnow()),
                 state, actor_username,
                 state, dismissed_reason, dismissed_reason,
                 note_id),
            )

    def supersede_stale_open_suggestions(
        self,
        *,
        project_id: int,
        mr_iid: int,
        current_head_sha: str,
    ) -> list[str]:
        """把该 MR 上 head_sha != current_head_sha 的全部 state=open suggestions
        标记为 'superseded'.

        Why:
            用户 UI Apply suggestion / 新 push 后 head_sha 变化, 老 suggestions
            的 inline 行号 / 上下文可能已不再有效. 把它们标 superseded 而不是
            留 state=open, 避免:
              - /adopt 误以为这行还待应用, 触发 reconcile 失败
              - 前端 V{N} / 列表里看到一堆"仍 open"但实际已 outdated 的提示
              - 后续 improve 的 dedup_at_line 把它们当作"已发过"而漏掉新 bug

        Returns:
            superseded note_id 列表 (用于发一次性合并通知).

        边界: head_sha 为空时不操作 (前置校验失败的情况).
        """
        if not current_head_sha:
            return []
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT note_id FROM suggestions "
                "WHERE project_id=? AND mr_iid=? AND state='open' "
                "AND head_sha != ?",
                (project_id, mr_iid, current_head_sha),
            ).fetchall()
            note_ids = [str(r["note_id"]) for r in rows]
            if not note_ids:
                return []
            # 批量更新: 按 note_id 逐条 update (note_id 是字符串 PK-ish, 写 IN 可能超长)
            conn.executemany(
                "UPDATE suggestions SET state='superseded', updated_at=? "
                "WHERE note_id=? AND state='open'",
                [(_fmt_dt(_utcnow()), nid) for nid in note_ids],
            )
            return note_ids

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
            clauses.append("datetime(created_at) >= datetime(?)"); params.append(_norm_iso(since))
        if until:
            clauses.append("datetime(created_at) < datetime(?)"); params.append(_norm_iso(until))
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
        total = len(suggestions)
        # 采纳率 = applied / total (含 open). 前端 renderer 期望 0~1 小数
        # (再 * 100 = %), 与 my-pr-agent reporting/renderer.py 一致.
        adoption_rate = round(adopted / total, 4) if total else 0.0
        return {
            "total": total,
            "state_counts": by_state,
            "severity_counts": by_severity,
            "action_counts": by_action,
            "adopted": adopted,
            "dismissed": dismissed,
            "adoption_rate": adoption_rate,
            # 兼容老前端: 直接给百分数字段
            "adoption_pct": round(adoption_rate * 100, 1),
        }

    def list_dismissals(
        self, *, project_id=None, mr_iid=None, since=None, until=None,
        rule_key=None, limit=200,
    ):
        clauses = ["s.state = 'dismissed'"]
        params = []
        if project_id is not None: clauses.append("s.project_id = ?"); params.append(project_id)
        if mr_iid is not None: clauses.append("s.mr_iid = ?"); params.append(mr_iid)
        if since: clauses.append("datetime(s.dismissed_at) >= datetime(?)"); params.append(_norm_iso(since))
        if until: clauses.append("datetime(s.dismissed_at) < datetime(?)"); params.append(_norm_iso(until))
        if rule_key:
            clauses.append("(',' || COALESCE(s.rule_keys,'') || ',') LIKE ?")
            params.append(f"%,{rule_key},%")
        where = " WHERE " + " AND ".join(clauses)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT s.* FROM suggestions s{where} ORDER BY s.dismissed_at DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def dismissals_by_rule(self, *, project_id=None, since=None):
        rows = self.list_dismissals(project_id=project_id, since=since, limit=10000)
        bucket = {}
        for row in rows:
            keys = [k.strip() for k in (row.get('rule_keys') or '').split(',') if k.strip()]
            if not keys: keys = ['(no_rule_key)']
            reason = (row.get('dismissed_reason') or '（未填写原因）').strip() or '（未填写原因）'
            for key in keys:
                slot = bucket.setdefault(key, {'rule_key': key, 'dismissal_count': 0, 'reasons': []})
                slot['dismissal_count'] += 1
                rs = next((r for r in slot['reasons'] if r['reason'] == reason), None)
                if rs: rs['count'] += 1
                else: slot['reasons'].append({'reason': reason, 'count': 1})
        for slot in bucket.values(): slot['reasons'].sort(key=lambda r: -r['count'])
        return sorted(bucket.values(), key=lambda r: -r['dismissal_count'])

    def distinct_rule_keys(self, *, project_id=None, mr_iid=None):
        with self._conn() as conn:
            clauses, params = [], []
            if project_id is not None: clauses.append('project_id = ?'); params.append(project_id)
            if mr_iid is not None: clauses.append('mr_iid = ?'); params.append(mr_iid)
            where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
            rows = conn.execute(
                f'SELECT rule_keys FROM suggestions{where} ORDER BY created_at DESC', params,
            ).fetchall()
        out = set()
        for r in rows:
            for k in (r['rule_keys'] or '').split(','):
                k = k.strip()
                if k: out.add(k)
        return sorted(out)

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
            clauses.append("datetime(started_at) >= datetime(?)")
            params.append(_norm_iso(since))
        if until:
            clauses.append("datetime(started_at) < datetime(?)")
            params.append(_norm_iso(until))
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
            clauses.append("datetime(started_at) >= datetime(?)")
            params.append(_norm_iso(since))
        if until:
            clauses.append("datetime(started_at) < datetime(?)")
            params.append(_norm_iso(until))
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
