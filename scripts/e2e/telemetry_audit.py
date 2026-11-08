"""全量档字段级 telemetry 审计：真实 GitLab 产物 × DB 落库 × 读接口回显.

覆盖 6 张表/面：
    telemetry:mr_activity   webhook/API 入参 → mr_activity 字段
    telemetry:review_runs   run 生命周期 + rule_keys_cited / suggestion_count
    telemetry:suggestions   score / severity_source / note_id 与 GitLab discussion 对齐
    telemetry:actions       adopt/dismiss 审计字段 + 与 suggestions 的状态联动
    telemetry:file_state    增量复用指纹 + 检视文件覆盖
    telemetry:api_params    读接口过滤/分页/聚合与 DB 直读一致

纯断言函数（audit_*）不触网，单测在 tests/test_e2e_telemetry_audit.py；
`run()` 只做取数编排，供 `scripts/e2e/run_e2e.py --tier full` 调用。
"""
from __future__ import annotations

import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from scripts.e2e import report, stack
from scripts.e2e.report import FeatureResult

TERMINAL = {"success", "failed", "skipped", "timeout"}
ALLOWED_STATES = {"open", "applied", "dismissed", "resolved", "superseded"}
SEVERITIES = {"low", "medium", "high", "critical"}
DISMISSAL_ACTIONS = {"adopted", "dismissed"}
# 命令名 -> 落库的 action 值（/adopt 写 adopted）
REQUIRED_ACTION_MAP = {"adopt": "adopted", "dismiss": "dismissed"}

CHECK_ORDER = ("mr_activity", "review_runs", "suggestions", "actions", "file_state", "api_params")


@dataclass(frozen=True)
class MrExpectation:
    """真实 GitLab MR 的期望值（API 返回，非 webhook 合成值）."""

    project_id: int
    mr_iid: int
    title: str
    source_branch: str
    target_branch: str
    state: str
    created_at: str | None
    head_sha: str
    author_username: str
    changed_files: tuple[str, ...]
    discussion_ids: frozenset[str]
    discussion_bodies: Mapping[str, str] = field(default_factory=dict)


@dataclass
class Snapshot:
    """审计时点的 DB 直读快照（先于任何 HTTP 取数，保证数据只多不少）."""

    mr: dict | None
    runs: list[dict]
    suggestions: list[dict]
    actions: list[dict]
    file_states: list[dict]
    project_runs: int = 0
    project_mrs: int = 0
    project_rule_keys: tuple[str, ...] = ()

    @property
    def running(self) -> int:
        return sum(1 for run in self.runs if run.get("status") == "running")


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith(" UTC"):
        text = text[: -len(" UTC")] + "+00:00"
    text = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _split_rule_keys(value: Any) -> list[str]:
    return [key.strip() for key in str(value or "").split(",") if key.strip()]


def _rule_keys_of(rows: list[dict]) -> set[str]:
    keys: set[str] = set()
    for row in rows:
        keys.update(_split_rule_keys(row.get("rule_keys")))
    return keys


def _result(problems: list[str], notes: list[str]) -> tuple[bool, str]:
    if problems:
        return False, "; ".join(problems[:8])
    return True, "; ".join(notes[:6]) or "ok"


def _short(note_id: Any) -> str:
    return str(note_id or "")[:8] or "?"


def _count_problem(label: str, actual: int, expected: int, settled: bool) -> str | None:
    """settled 时要求完全一致；后台仍在写时只要求不小于快照."""
    if actual == expected:
        return None
    if not settled and actual > expected:
        return None
    return f"{label} total={actual} db={expected}"


# ---------------------------------------------------------------------------
# mr_activity
# ---------------------------------------------------------------------------


def audit_mr_activity(snap: Snapshot, expect: MrExpectation) -> tuple[bool, str]:
    row = snap.mr
    if not row:
        return False, "no mr_activity row"
    problems: list[str] = []
    title = str(row.get("title") or "")
    if title != expect.title:
        problems.append(f"title={title!r} != gitlab {expect.title!r}")
    if "[no_more_review]" in title:
        problems.append("title polluted with [no_more_review]")
    for attr in ("source_branch", "target_branch", "state"):
        actual = row.get(attr)
        wanted = getattr(expect, attr)
        if actual != wanted:
            problems.append(f"{attr}={actual!r} != gitlab {wanted!r}")

    created = _dt(row.get("created_at"))
    expected_created = _dt(expect.created_at)
    if created is None:
        problems.append("created_at missing")
    elif expected_created is not None and abs((created - expected_created).total_seconds()) > 2:
        problems.append(f"created_at={created.isoformat()} != gitlab {expected_created.isoformat()}")

    author = str(row.get("author_username") or "")
    if expect.author_username and not (
        author == expect.author_username or author.endswith(f"@{expect.author_username}")
    ):
        problems.append(f"author_username={author!r} not gitlab author {expect.author_username!r}")
    sticky = str(row.get("author_sticky") or "")
    if not sticky:
        problems.append("author_sticky missing")
    elif expect.author_username and not (
        sticky == expect.author_username or sticky.endswith(f"@{expect.author_username}")
    ):
        problems.append(f"author_sticky={sticky!r} not gitlab author {expect.author_username!r}")
    if not row.get("last_activity_at"):
        problems.append("last_activity_at missing")

    return _result(
        problems,
        [f"title/branches/state/created_at/author ok; last_activity={str(row.get('last_activity_at'))[:19]}"],
    )


# ---------------------------------------------------------------------------
# review_runs
# ---------------------------------------------------------------------------


def audit_review_runs(snap: Snapshot, expect: MrExpectation) -> tuple[bool, str]:
    runs = snap.runs
    if not runs:
        return False, "no review_runs recorded"
    problems: list[str] = []
    windows: list[str] = []
    for run in runs:
        tag = f"{run.get('command')}@{str(run.get('started_at'))[:19]}"
        status = str(run.get("status") or "")
        if not run.get("started_at"):
            problems.append(f"{tag}: started_at missing")
        if not run.get("command"):
            problems.append(f"{tag}: command missing")
        if not run.get("triggered_by"):
            problems.append(f"{tag}: triggered_by missing")
        if status not in TERMINAL and status != "running":
            problems.append(f"{tag}: status={status!r}")
        if status == "running":
            continue
        started, finished = _dt(run.get("started_at")), _dt(run.get("finished_at"))
        if finished is None:
            problems.append(f"{tag}: finished_at missing for status={status}")
            continue
        if started is not None and finished < started:
            problems.append(f"{tag}: finished_at < started_at")
        total = int(run.get("total_tokens") or 0)
        tokens = int(run.get("prompt_tokens") or 0) + int(run.get("completion_tokens") or 0)
        if total != tokens:
            problems.append(f"{tag}: total_tokens={total} != prompt+completion={tokens}")
        if status == "success":
            if not run.get("model"):
                problems.append(f"{tag}: model missing")
            if not run.get("llm_provider"):
                problems.append(f"{tag}: llm_provider missing")
        if run.get("triggered_by") == "note" and not run.get("actor_username"):
            problems.append(f"{tag}: actor_username missing for note-triggered run")

        if status != "success" or run.get("command") != "improve" or started is None:
            continue
        in_window = [
            sug for sug in snap.suggestions
            if (created := _dt(sug.get("created_at"))) is not None and started <= created < finished
        ]
        if not in_window:
            continue
        count = int(run.get("suggestion_count") or 0)
        if count < len(in_window):
            problems.append(
                f"{tag}: suggestion_count={count} < db suggestions={len(in_window)}"
            )
        cited = set(_split_rule_keys(run.get("rule_keys_cited")))
        missing = _rule_keys_of(in_window) - cited
        if missing:
            problems.append(f"{tag}: rule_keys_cited missing {sorted(missing)[:3]}")
        windows.append(f"{tag}->{count}")

    if not any(run.get("status") == "success" for run in runs):
        problems.append("no successful run")
    if snap.suggestions and not windows:
        problems.append("suggestions exist but no improve run recorded suggestion_count/rule_keys_cited")

    statuses = sorted({str(run.get("status")) for run in runs})
    return _result(
        problems,
        [f"{len(runs)} runs status={statuses}; improve_windows={windows or 'none'}"],
    )


# ---------------------------------------------------------------------------
# suggestions
# ---------------------------------------------------------------------------


def _improved_code_problems(sug: dict, body: str) -> list[str]:
    """improved_code 与 GitLab note 正文对齐.

    空 improved_code 是合法形态（删除整段 existing，见 policies/python.py 的
    suggestion 组装），但必须满足：existing_code 非空 + note 里有 suggestion 块。
    非空时必须原样出现在 note 正文里，防止 DB 与真实发布内容漂移.
    """
    tag = _short(sug.get("note_id"))
    improved = str(sug.get("improved_code") or "").strip("\n")
    if improved.strip():
        if body and improved not in body:
            return [f"{tag}: improved_code diverges from posted note"]
        return []
    if not str(sug.get("existing_code") or "").strip():
        return [f"{tag}: improved_code missing (no existing_code to delete)"]
    if body and "```suggestion" not in body:
        return [f"{tag}: empty improved_code but note has no suggestion block"]
    return []


def audit_suggestions(snap: Snapshot, expect: MrExpectation) -> tuple[bool, str]:
    suggestions = snap.suggestions
    if not suggestions:
        return False, "no suggestions recorded"
    problems: list[str] = []
    changed = set(expect.changed_files)
    for sug in suggestions:
        tag = _short(sug.get("note_id"))
        if sug.get("file_path") not in changed:
            problems.append(f"{tag}: file={sug.get('file_path')!r} not in MR changes")
        if not int(sug.get("target_line") or 0):
            problems.append(f"{tag}: target_line missing")
        end = int(sug.get("target_line_end") or 0)
        if end and end < int(sug.get("target_line") or 0):
            problems.append(f"{tag}: target_line_end < target_line")
        if str(sug.get("severity") or "").lower() not in SEVERITIES:
            problems.append(f"{tag}: severity={sug.get('severity')!r}")
        if expect.head_sha and sug.get("head_sha") != expect.head_sha:
            problems.append(f"{tag}: head_sha does not match MR head")
        if str(sug.get("note_id")) not in expect.discussion_ids:
            problems.append(f"{tag}: note_id missing from GitLab discussions")
        if sug.get("state") not in ALLOWED_STATES:
            problems.append(f"{tag}: state={sug.get('state')!r}")
        if sug.get("severity_source") not in ("rule", "llm"):
            problems.append(f"{tag}: severity_source={sug.get('severity_source')!r}")
        if not isinstance(sug.get("score"), int):
            problems.append(f"{tag}: score={sug.get('score')!r}")
        if sug.get("severity_source") == "rule" and not _split_rule_keys(sug.get("rule_keys")):
            problems.append(f"{tag}: severity_source=rule without rule_keys")
        problems.extend(
            _improved_code_problems(sug, expect.discussion_bodies.get(str(sug.get("note_id")), ""))
        )
        if not sug.get("posted_at") or not sug.get("created_at"):
            problems.append(f"{tag}: posted_at/created_at missing")

    severities = sorted({str(s.get("severity")) for s in suggestions})
    sources = sorted({str(s.get("severity_source")) for s in suggestions})
    return _result(
        problems,
        [f"{len(suggestions)} suggestions; severities={severities}; sources={sources}"],
    )


# ---------------------------------------------------------------------------
# suggestion_actions
# ---------------------------------------------------------------------------


def audit_actions(snap: Snapshot, *, required: set[str] | None = None) -> tuple[bool, str]:
    required = required or set()
    actions = snap.actions
    problems: list[str] = []
    by_note = {str(sug.get("note_id")): sug for sug in snap.suggestions}
    counts: dict[str, int] = {}

    for action in actions:
        tag = f"{action.get('action')}:{_short(action.get('suggestion_note_id'))}"
        name = str(action.get("action") or "")
        counts[name] = counts.get(name, 0) + 1
        if name not in DISMISSAL_ACTIONS:
            problems.append(f"{tag}: unexpected action")
        if not action.get("suggestion_note_id"):
            problems.append(f"{tag}: suggestion_note_id missing")
        if not action.get("actor_username"):
            problems.append(f"{tag}: actor_username missing")
        if not action.get("validation_status"):
            problems.append(f"{tag}: validation_status missing")
        if not action.get("head_sha_posted"):
            problems.append(f"{tag}: head_sha_posted missing")
        if not action.get("created_at"):
            problems.append(f"{tag}: created_at missing")

        sug = by_note.get(str(action.get("suggestion_note_id")))
        if sug is None:
            problems.append(f"{tag}: note_id not present in suggestions")
            continue
        if action.get("head_sha_posted") and action["head_sha_posted"] != sug.get("head_sha"):
            problems.append(f"{tag}: head_sha_posted != suggestion.head_sha")
        if name == "dismissed":
            if sug.get("state") != "dismissed":
                problems.append(f"{tag}: suggestion state={sug.get('state')!r} (expected dismissed)")
            if not sug.get("dismissed_at"):
                problems.append(f"{tag}: suggestion dismissed_at missing")
            reason = str(action.get("reason") or "")
            if reason and reason != str(sug.get("dismissed_reason") or ""):
                problems.append(f"{tag}: dismissed_reason not mirrored on suggestion")
        if name == "adopted":
            # /adopt 校验通过 (或已落地) 才翻 applied; 校验失败时 suggestion 仍是 open
            validation = str(action.get("validation_status") or "")
            applied = validation in ("ok", "late-detect-apply") or validation.startswith("already-")
            if applied and sug.get("state") not in ("applied", "resolved"):
                problems.append(f"{tag}: suggestion state={sug.get('state')!r} (expected applied/resolved)")

    for required_action in sorted(required):
        stored = REQUIRED_ACTION_MAP.get(required_action, required_action)
        if not counts.get(stored):
            problems.append(f"missing {required_action} action")

    return _result(
        problems,
        [f"{len(actions)} actions {counts or '{}'}"],
    )


# ---------------------------------------------------------------------------
# reviewed_file_state
# ---------------------------------------------------------------------------


def audit_file_states(snap: Snapshot, expect: MrExpectation) -> tuple[bool, str]:
    states = snap.file_states
    if not states:
        return False, "no reviewed_file_state rows"
    problems: list[str] = []
    for row in states:
        tag = str(row.get("file_path"))
        if not row.get("blob_sha"):
            problems.append(f"{tag}: blob_sha missing")
        if not row.get("head_sha"):
            problems.append(f"{tag}: head_sha missing")
        elif expect.head_sha and row["head_sha"] != expect.head_sha:
            problems.append(f"{tag}: head_sha != MR head")
        if not row.get("profile_fingerprint"):
            problems.append(f"{tag}: profile_fingerprint missing")
        if not row.get("ruleset_fingerprint"):
            problems.append(f"{tag}: ruleset_fingerprint missing")
        if not row.get("reviewed_at"):
            problems.append(f"{tag}: reviewed_at missing")

    covered = {str(row.get("file_path")) for row in states}
    suggestion_files = {str(sug.get("file_path")) for sug in snap.suggestions}
    missing = suggestion_files - covered
    if missing:
        problems.append(f"suggestion files missing file_state: {sorted(missing)[:3]}")

    return _result(problems, [f"{len(states)} files reviewed; suggestion files covered={not missing}"])


# ---------------------------------------------------------------------------
# 读接口参数
# ---------------------------------------------------------------------------


def _query(**params: Any) -> str:
    clean = {key: value for key, value in params.items() if value is not None}
    return urllib.parse.urlencode(clean)


def audit_api_params(
    snap: Snapshot,
    expect: MrExpectation,
    probe: Any,
    *,
    settled: bool = True,
) -> tuple[bool, str]:
    problems: list[str] = []
    notes: list[str] = []
    pid, mr = expect.project_id, expect.mr_iid

    def _get(path: str) -> dict:
        status, body = probe.get(path)
        if status != 200 or not isinstance(body, dict):
            problems.append(f"{path.split('?')[0]} -> {status}")
            return {}
        return body

    # 1) /runs：total 必须是整表计数，不是当前页大小
    db_improve = [run for run in snap.runs if run.get("command") == "improve"]
    body = _get(f"/runs?{_query(project_id=pid, mr_iid=mr, command='improve', limit=1)}")
    if body:
        total = int(body.get("total") or 0)
        page = body.get("runs") or []
        problem = _count_problem("/runs", total, len(db_improve), settled)
        if problem:
            problems.append(problem)
        if len(page) != min(1, len(db_improve)):
            problems.append(f"/runs page={len(page)} != 1")
        notes.append(f"runs total={total} page={len(page)}")

    # 2) /mrs：时间窗口必须按 created_at，不能按 updated_at
    created = _dt((snap.mr or {}).get("created_at"))
    if created is None:
        problems.append("/mrs window: mr_activity.created_at missing")
    else:
        inside = _get(f"/mrs?{_query(project_id=pid, since=(created - timedelta(hours=1)).isoformat(), until=(created + timedelta(hours=1)).isoformat())}")
        if inside and mr not in [row.get("mr_iid") for row in inside.get("mrs") or []]:
            problems.append("/mrs window: MR missing inside created_at window")
        outside = _get(f"/mrs?{_query(project_id=pid, since=(created + timedelta(hours=2)).isoformat(), until=(created + timedelta(hours=3)).isoformat())}")
        if outside and mr in [row.get("mr_iid") for row in outside.get("mrs") or []]:
            problems.append("/mrs window: MR returned outside created_at window (updated_at leak)")
        notes.append("mrs created_at window ok")

    # 3) /dismissals：总数/rule_key/offset/until 全部走参数
    db_dismissed = [sug for sug in snap.suggestions if sug.get("state") == "dismissed"]
    body = _get(f"/dismissals?{_query(project_id=pid, mr_iid=mr, limit=1)}")
    if body:
        total = int(body.get("total") or 0)
        problem = _count_problem("/dismissals", total, len(db_dismissed), settled)
        if problem:
            problems.append(problem)
        page = body.get("dismissals") or []
        if len(page) != min(1, len(db_dismissed)):
            problems.append(f"/dismissals page={len(page)} != 1")
        notes.append(f"dismissals total={total}")

    keyed = [sug for sug in db_dismissed if _split_rule_keys(sug.get("rule_keys"))]
    if keyed:
        key = _split_rule_keys(keyed[0].get("rule_keys"))[0]
        body = _get(f"/dismissals?{_query(project_id=pid, mr_iid=mr, rule_key=key, limit=50)}")
        if body:
            rows = body.get("dismissals") or []
            bad = [row.get("note_id") for row in rows if key not in _split_rule_keys(row.get("rule_keys"))]
            if bad:
                problems.append(f"/dismissals rule_key={key} leaked rows {bad[:3]}")
            if str(keyed[0].get("note_id")) not in {str(row.get("note_id")) for row in rows}:
                problems.append(f"/dismissals rule_key={key} missing expected note")
        bogus = _get(f"/dismissals?{_query(project_id=pid, mr_iid=mr, rule_key='__none__', limit=50)}")
        if bogus and int(bogus.get("total") or 0) != 0:
            problems.append("/dismissals rule_key=__none__ should be empty")
        notes.append(f"dismissals rule_key={key} ok")

    dismissed_times = [
        stamp for sug in db_dismissed if (stamp := _dt(sug.get("dismissed_at"))) is not None
    ]
    if len(db_dismissed) >= 2 and dismissed_times:
        # until 走 dismissed_at 升序边界: 取最早一条之前, 必须一条都不返回
        until = (min(dismissed_times) - timedelta(seconds=1)).isoformat()
        body = _get(f"/dismissals?{_query(project_id=pid, mr_iid=mr, until=until, limit=50)}")
        if body and int(body.get("total") or 0) != 0:
            problems.append("/dismissals until filter ignored")
        page0 = _get(f"/dismissals?{_query(project_id=pid, mr_iid=mr, limit=1, offset=0)}")
        page1 = _get(f"/dismissals?{_query(project_id=pid, mr_iid=mr, limit=1, offset=1)}")
        if page0 and page1:
            note0 = [str(row.get("note_id")) for row in page0.get("dismissals") or []]
            note1 = [str(row.get("note_id")) for row in page1.get("dismissals") or []]
            if note0 == note1:
                problems.append("/dismissals offset ignored (same page)")
        notes.append("dismissals until/offset ok")

    # 4) /metrics/rules：真实规则键，不是 severity 兜底
    body = _get(f"/metrics/rules?{_query(project_id=pid, top_n=100)}")
    if body:
        api_keys = {str(row.get("rule_key")) for row in body.get("rules") or []}
        db_keys = set(snap.project_rule_keys)
        if settled:
            if api_keys != db_keys:
                problems.append(f"/metrics/rules {sorted(api_keys)[:3]} != db {sorted(db_keys)[:3]}")
        elif not db_keys <= api_keys:
            problems.append(f"/metrics/rules missing db keys {sorted(db_keys - api_keys)[:3]}")
        notes.append(f"rules keys={len(api_keys)}")

    # 5) /metrics/authors：MR 数与 run 数都要从对应表聚合
    body = _get(f"/metrics/authors?{_query(project_id=pid)}")
    if body:
        authors = body.get("authors") or []
        mrs_sum = sum(int(row.get("mrs") or 0) for row in authors)
        runs_sum = sum(int(row.get("runs") or 0) for row in authors)
        if settled:
            if mrs_sum != snap.project_mrs:
                problems.append(f"/metrics/authors mrs={mrs_sum} db={snap.project_mrs}")
            if runs_sum != snap.project_runs:
                problems.append(f"/metrics/authors runs={runs_sum} db={snap.project_runs}")
        else:
            if mrs_sum < snap.project_mrs or runs_sum < snap.project_runs:
                problems.append(f"/metrics/authors counts below db mrs={mrs_sum}/{snap.project_mrs} runs={runs_sum}/{snap.project_runs}")
        notes.append(f"authors mrs={mrs_sum} runs={runs_sum}")

    return _result(problems, notes)


# ---------------------------------------------------------------------------
# 取数编排（全量档入口）
# ---------------------------------------------------------------------------


def settle_runs(project_id: int, mr_iid: int, log: list[str], *, timeout_s: int = 240) -> bool:
    """等 running run 收尾，避免审计与后台检视互相踩数据."""
    from reviewagent.telemetry.store import get_store

    store = get_store()
    deadline = time.time() + timeout_s
    running = -1
    while time.time() < deadline:
        with store._conn() as conn:  # noqa: SLF001
            row = conn.execute(
                "SELECT COUNT(*) FROM review_runs "
                "WHERE project_id=? AND mr_iid=? AND status='running'",
                (project_id, mr_iid),
            ).fetchone()
        running = int(row[0]) if row else 0
        if running == 0:
            log.append(f"[{stack.now_iso()}] telemetry settle: no running runs")
            return True
        time.sleep(3)
    log.append(
        f"[{stack.now_iso()}] WARN telemetry settle timeout: {running} running runs "
        f"after {timeout_s}s; count assertions relaxed"
    )
    return False


def snapshot_db(project_id: int, mr_iid: int) -> Snapshot:
    from reviewagent.telemetry.store import get_store

    store = get_store()
    with store._conn() as conn:  # noqa: SLF001
        mr_row = conn.execute(
            "SELECT * FROM mr_activity WHERE project_id=? AND mr_iid=?",
            (project_id, mr_iid),
        ).fetchone()
        runs = [dict(row) for row in conn.execute(
            "SELECT * FROM review_runs WHERE project_id=? AND mr_iid=? ORDER BY started_at, id",
            (project_id, mr_iid),
        ).fetchall()]
        suggestions = [dict(row) for row in conn.execute(
            "SELECT * FROM suggestions WHERE project_id=? AND mr_iid=? ORDER BY created_at, id",
            (project_id, mr_iid),
        ).fetchall()]
        actions = [dict(row) for row in conn.execute(
            "SELECT * FROM suggestion_actions WHERE project_id=? AND mr_iid=? ORDER BY created_at, id",
            (project_id, mr_iid),
        ).fetchall()]
        file_states = [dict(row) for row in conn.execute(
            "SELECT * FROM reviewed_file_state WHERE project_id=? AND mr_iid=? ORDER BY file_path",
            (project_id, mr_iid),
        ).fetchall()]
        project_runs = int(conn.execute(
            "SELECT COUNT(*) FROM review_runs WHERE project_id=?", (project_id,)
        ).fetchone()[0])
        project_mrs = int(conn.execute(
            "SELECT COUNT(*) FROM mr_activity WHERE project_id=?", (project_id,)
        ).fetchone()[0])
        key_rows = conn.execute(
            "SELECT rule_keys FROM suggestions WHERE project_id=? AND rule_keys IS NOT NULL",
            (project_id,),
        ).fetchall()
    keys: set[str] = set()
    for row in key_rows:
        keys.update(_split_rule_keys(row["rule_keys"]))
    return Snapshot(
        mr=dict(mr_row) if mr_row else None,
        runs=runs,
        suggestions=suggestions,
        actions=actions,
        file_states=file_states,
        project_runs=project_runs,
        project_mrs=project_mrs,
        project_rule_keys=tuple(sorted(keys)),
    )


def fetch_expectation(project_id: int, mr_iid: int, log: list[str]) -> MrExpectation:
    mr = stack.gitlab_get(f"/projects/{project_id}/merge_requests/{mr_iid}")
    changes = stack.gitlab_get(f"/projects/{project_id}/merge_requests/{mr_iid}/changes")
    files = {
        str(entry.get("new_path") or entry.get("old_path") or "")
        for entry in changes.get("changes") or []
        if not entry.get("deleted_file")
    }
    discussions = stack.gitlab_get(
        f"/projects/{project_id}/merge_requests/{mr_iid}/discussions?per_page=100"
    )
    ids = {
        str(item.get("id"))
        for item in discussions if isinstance(discussions, list)
    }
    bodies = {
        str(item.get("id")): "\n".join(
            str(note.get("body") or "")
            for note in item.get("notes") or []
            if not note.get("system")
        )
        for item in discussions if isinstance(discussions, list)
    }
    author = str((mr.get("author") or {}).get("username") or "")
    log.append(
        f"[{stack.now_iso()}] telemetry expect pid={project_id} mr={mr_iid} "
        f"sha={str(mr.get('sha'))[:10]} files={len(files)} discussions={len(ids)} author={author}"
    )
    return MrExpectation(
        project_id=project_id,
        mr_iid=mr_iid,
        title=str(mr.get("title") or ""),
        source_branch=str(mr.get("source_branch") or ""),
        target_branch=str(mr.get("target_branch") or ""),
        state=str(mr.get("state") or ""),
        created_at=mr.get("created_at"),
        head_sha=str(mr.get("sha") or ""),
        author_username=author,
        changed_files=tuple(sorted(path for path in files if path)),
        discussion_ids=frozenset(ids),
        discussion_bodies=bodies,
    )


class _HttpProbe:
    """HTTP probe：走 stack.telemetry_get，测试里可替换为假实现."""

    def get(self, path: str) -> tuple[int, Any]:
        return stack.telemetry_get(path)


def run(
    mr_iid: int,
    round_idx: int,
    project_id: int,
    *,
    features: set[str] | None = None,
    log: list[str] | None = None,
    settle_timeout_s: int = 240,
) -> list[FeatureResult]:
    """全量档入口：settle → DB 快照 → GitLab 期望 → 6 项断言."""
    log = log if log is not None else []
    required = {name for name in ("adopt", "dismiss") if features and name in features}
    settled = settle_runs(project_id, mr_iid, log, timeout_s=settle_timeout_s)
    snap = snapshot_db(project_id, mr_iid)

    results: list[FeatureResult] = []

    def _emit(name: str, check: Callable[[], tuple[bool, str]]) -> None:
        try:
            ok, detail = check()
        except Exception as exc:  # noqa: BLE001 - 单项异常要进报告
            ok, detail = False, f"{type(exc).__name__}: {exc}"
            log.append(f"[{stack.now_iso()}] telemetry {name} EXC: {type(exc).__name__}: {exc}")
        result = FeatureResult(f"telemetry:{name}", round_idx, mr_iid, ok, detail, log)
        report.print_result(result)
        results.append(result)

    try:
        expect = fetch_expectation(project_id, mr_iid, log)
    except Exception as exc:  # noqa: BLE001
        message = f"fetch_expectation failed: {type(exc).__name__}: {exc}"
        for name in CHECK_ORDER:
            _emit(name, lambda: (False, message))
        return results

    _emit("mr_activity", lambda: audit_mr_activity(snap, expect))
    _emit("review_runs", lambda: audit_review_runs(snap, expect))
    _emit("suggestions", lambda: audit_suggestions(snap, expect))
    _emit("actions", lambda: audit_actions(snap, required=required))
    _emit("file_state", lambda: audit_file_states(snap, expect))
    _emit(
        "api_params",
        lambda: audit_api_params(snap, expect, _HttpProbe(), settled=settled),
    )
    return results


__all__ = [
    "CHECK_ORDER",
    "MrExpectation",
    "Snapshot",
    "audit_actions",
    "audit_api_params",
    "audit_file_states",
    "audit_mr_activity",
    "audit_review_runs",
    "audit_suggestions",
    "fetch_expectation",
    "run",
    "settle_runs",
    "snapshot_db",
]
