"""全量档字段级 telemetry 审计的纯断言部分.

真实 DB / GitLab / API 只由 `scripts/e2e/run_e2e.py --tier full` 覆盖；
这里固定输入，验证每条断言在该失败时确实会失败。
"""
from __future__ import annotations

import urllib.parse

import pytest

from scripts.e2e import telemetry_audit as ta

PROJECT = 10
MR = 2
MR_SHA = "abc123"
AUTHOR = "root"


def _expect(**over):
    base = dict(
        project_id=PROJECT,
        mr_iid=MR,
        title="E2E 真实变更 r1",
        source_branch="e2e/change-r1",
        target_branch="main",
        state="opened",
        created_at="2026-11-08T10:00:00.000Z",
        head_sha=MR_SHA,
        author_username=AUTHOR,
        changed_files=("app/service.py",),
        discussion_ids=frozenset({"d1"}),
    )
    base.update(over)
    return ta.MrExpectation(**base)


def _mr_row(**over):
    base = dict(
        project_id=PROJECT, mr_iid=MR, title="E2E 真实变更 r1",
        author_username="Administrator@root", author_sticky="Administrator@root",
        source_branch="e2e/change-r1", target_branch="main", state="opened",
        created_at="2026-11-08T10:00:00+00:00", updated_at="2026-11-08T10:05:00+00:00",
        merged_at=None, description_generated=1,
        last_review_at="2026-11-08T10:01:00+00:00",
        last_activity_at="2026-11-08T10:03:00+00:00", no_more_review=0,
    )
    base.update(over)
    return base


def _run(**over):
    base = dict(
        id=1, project_id=PROJECT, mr_iid=MR, command="improve", triggered_by="note",
        actor_username="review-tester@root",
        started_at="2026-11-08T10:01:00+00:00",
        finished_at="2026-11-08T10:02:00+00:00",
        status="success", error=None, model="DeepSeek-V3.2",
        prompt_tokens=100, completion_tokens=20, total_tokens=120,
        duration_ms=60000, cost_credits=0.1, llm_provider="qodercli",
        rule_keys_cited="R-ERR,SSD-RULE-FORBIDDEN-WILDCARD-IMPORT", suggestion_count=1,
    )
    base.update(over)
    return base


def _sug(**over):
    base = dict(
        id=1, project_id=PROJECT, mr_iid=MR, note_id="d1",
        file_path="app/service.py", target_line=5, target_line_end=5,
        existing_code="from app.utils import *", improved_code="from app import utils",
        header="禁止 wildcard import", severity="medium", head_sha=MR_SHA,
        rule_keys="R-ERR", state="open", severity_source="rule", score=77,
        posted_at="2026-11-08T10:01:20+00:00",
        created_at="2026-11-08T10:01:30+00:00", updated_at=None,
        dismissed_at=None, dismissed_by=None, dismissed_reason=None,
        fingerprint="f1", cohort_key="c1",
    )
    base.update(over)
    return base


def _action(**over):
    base = dict(
        id=1, project_id=PROJECT, mr_iid=MR, suggestion_note_id="d1",
        file_path="app/service.py", target_line=5, action="dismissed",
        actor_username="review-tester@root", reason="不要 (e2e 验证)",
        validation_status="ok", adoption_source=None,
        head_sha_posted=MR_SHA, head_sha_current=None,
        created_at="2026-11-08T10:03:00+00:00",
    )
    base.update(over)
    return base


def _file_state(**over):
    base = dict(
        project_id=PROJECT, mr_iid=MR, file_path="app/service.py", blob_sha="blob1",
        head_sha=MR_SHA, suggestion_count=1,
        profile_fingerprint="profile1", ruleset_fingerprint="rules1",
        reviewed_at="2026-11-08T10:02:00+00:00",
    )
    base.update(over)
    return base


def _snapshot(**over):
    base = dict(
        mr=_mr_row(), runs=[_run()], suggestions=[_sug()], actions=[_action()],
        file_states=[_file_state()], project_runs=1, project_mrs=1,
        project_rule_keys=("R-ERR", "SSD-RULE-FORBIDDEN-WILDCARD-IMPORT"),
    )
    base.update(over)
    return ta.Snapshot(**base)


# ---------------------------------------------------------------------------
# mr_activity
# ---------------------------------------------------------------------------


def test_mr_activity_passes_on_faithful_fields() -> None:
    ok, detail = ta.audit_mr_activity(_snapshot(), _expect())
    assert ok, detail


def test_mr_activity_fails_when_title_polluted() -> None:
    snap = _snapshot(mr=_mr_row(title="E2E 真实变更 r1 [no_more_review]"))
    ok, detail = ta.audit_mr_activity(snap, _expect())
    assert ok is False and "no_more_review" in detail


def test_mr_activity_fails_when_author_is_event_actor() -> None:
    snap = _snapshot(mr=_mr_row(author_username="review-tester@someone", author_sticky="") )
    ok, detail = ta.audit_mr_activity(snap, _expect())
    assert ok is False and "author" in detail


def test_mr_activity_fails_when_created_at_missing() -> None:
    snap = _snapshot(mr=_mr_row(created_at=None))
    ok, detail = ta.audit_mr_activity(snap, _expect())
    assert ok is False and "created_at" in detail


# ---------------------------------------------------------------------------
# review_runs
# ---------------------------------------------------------------------------


def test_review_runs_passes_with_window_and_new_fields() -> None:
    ok, detail = ta.audit_review_runs(_snapshot(), _expect())
    assert ok, detail


def test_review_runs_fails_when_suggestion_count_not_written() -> None:
    snap = _snapshot(runs=[_run(suggestion_count=0, rule_keys_cited=None)])
    ok, detail = ta.audit_review_runs(snap, _expect())
    assert ok is False and "suggestion_count" in detail


def test_review_runs_fails_when_rule_keys_cited_missing() -> None:
    snap = _snapshot(runs=[_run(rule_keys_cited=None)])
    ok, detail = ta.audit_review_runs(snap, _expect())
    assert ok is False and "rule_keys_cited" in detail


def test_review_runs_fails_when_success_run_unfinished() -> None:
    snap = _snapshot(runs=[_run(finished_at=None)])
    ok, detail = ta.audit_review_runs(snap, _expect())
    assert ok is False and "finished_at" in detail


# ---------------------------------------------------------------------------
# suggestions
# ---------------------------------------------------------------------------


def test_suggestions_passes_with_score_and_severity_source() -> None:
    ok, detail = ta.audit_suggestions(_snapshot(), _expect())
    assert ok, detail


def test_suggestions_fail_when_score_missing() -> None:
    snap = _snapshot(suggestions=[_sug(score=None)])
    ok, detail = ta.audit_suggestions(snap, _expect())
    assert ok is False and "score" in detail


def test_suggestions_fail_when_note_not_in_gitlab_discussions() -> None:
    snap = _snapshot(suggestions=[_sug(note_id="ghost")])
    ok, detail = ta.audit_suggestions(snap, _expect())
    assert ok is False and "discussion" in detail


def test_suggestions_fail_when_file_outside_changed_set() -> None:
    snap = _snapshot(suggestions=[_sug(file_path="app/other.py")])
    ok, detail = ta.audit_suggestions(snap, _expect())
    assert ok is False and "change" in detail


def test_suggestions_allow_empty_improved_code_for_line_deletion() -> None:
    """空 improved_code = 删除整行（policies/python.py 支持的合法形态）."""
    snap = _snapshot(suggestions=[
        _sug(improved_code="", existing_code="from app.utils import *")
    ])
    expect = _expect(discussion_bodies={
        "d1": "删除通配符导入\n\n```suggestion:-0+0\n\n```\n"
    })
    ok, detail = ta.audit_suggestions(snap, expect)
    assert ok, detail


def test_suggestions_fail_when_empty_improved_code_has_no_deletion_target() -> None:
    snap = _snapshot(suggestions=[
        _sug(improved_code="", existing_code="   ")
    ])
    ok, detail = ta.audit_suggestions(snap, _expect())
    assert ok is False and "improved_code" in detail


def test_suggestions_fail_when_empty_improved_code_has_no_suggestion_block() -> None:
    snap = _snapshot(suggestions=[
        _sug(improved_code="", existing_code="from app.utils import *")
    ])
    expect = _expect(discussion_bodies={"d1": "只有文字说明，没有 suggestion 块"})
    ok, detail = ta.audit_suggestions(snap, expect)
    assert ok is False and "suggestion" in detail


def test_suggestions_fail_when_improved_code_diverges_from_posted_note() -> None:
    expect = _expect(discussion_bodies={
        "d1": "```suggestion:-0+0\nfrom app import something_else\n```"
    })
    ok, detail = ta.audit_suggestions(_snapshot(), expect)
    assert ok is False and "diverges" in detail


# ---------------------------------------------------------------------------
# actions
# ---------------------------------------------------------------------------


def test_actions_pass_with_head_sha_and_state_sync() -> None:
    snap = _snapshot(
        suggestions=[_sug(state="dismissed", dismissed_at="2026-11-08T10:03:00+00:00",
                          dismissed_by="review-tester@root", dismissed_reason="不要 (e2e 验证)")],
    )
    ok, detail = ta.audit_actions(snap, required={"dismiss"})
    assert ok, detail


def test_actions_fail_when_head_sha_posted_missing() -> None:
    snap = _snapshot(
        suggestions=[_sug(state="dismissed", dismissed_at="2026-11-08T10:03:00+00:00",
                          dismissed_reason="不要 (e2e 验证)")],
        actions=[_action(head_sha_posted=None)],
    )
    ok, detail = ta.audit_actions(snap, required=set())
    assert ok is False and "head_sha_posted" in detail


def test_actions_fail_when_required_action_missing() -> None:
    ok, detail = ta.audit_actions(_snapshot(actions=[]), required={"adopt"})
    assert ok is False and "adopt" in detail


def test_actions_fail_when_note_id_not_recorded() -> None:
    snap = _snapshot(actions=[_action(suggestion_note_id="ghost")])
    ok, detail = ta.audit_actions(snap, required=set())
    assert ok is False and "suggestions" in detail


def test_actions_allow_adopt_validation_failure_to_stay_open() -> None:
    """真实 /adopt 在代码未改时会校验失败, suggestion 保持 open."""
    snap = _snapshot(
        suggestions=[_sug(state="open")],
        actions=[_action(action="adopted", validation_status="target-unchanged")],
    )
    ok, detail = ta.audit_actions(snap, required={"adopt"})
    assert ok, detail


def test_actions_fail_when_successful_adopt_leaves_state_open() -> None:
    snap = _snapshot(
        suggestions=[_sug(state="open")],
        actions=[_action(action="adopted", validation_status="ok")],
    )
    ok, detail = ta.audit_actions(snap, required={"adopt"})
    assert ok is False and "applied" in detail


# ---------------------------------------------------------------------------
# reviewed_file_state
# ---------------------------------------------------------------------------


def test_file_states_pass_with_fingerprints() -> None:
    ok, detail = ta.audit_file_states(_snapshot(), _expect())
    assert ok, detail


def test_file_states_fail_when_profile_fingerprint_missing() -> None:
    snap = _snapshot(file_states=[_file_state(profile_fingerprint=None)])
    ok, detail = ta.audit_file_states(snap, _expect())
    assert ok is False and "profile_fingerprint" in detail


def test_file_states_fail_when_suggestion_file_not_reviewed() -> None:
    snap = _snapshot(
        suggestions=[_sug(file_path="app/other.py")],
        file_states=[_file_state(file_path="app/service.py")],
    )
    ok, detail = ta.audit_file_states(snap, _expect())
    assert ok is False and "file_state" in detail


# ---------------------------------------------------------------------------
# read API params
# ---------------------------------------------------------------------------


class _Probe:
    """记录请求路径并按 handler 返回 (status, body)."""

    def __init__(self, handler):
        self.handler = handler
        self.calls: list[str] = []

    def get(self, path: str):
        self.calls.append(path)
        return self.handler(path)


def _query(path: str) -> tuple[str, dict[str, list[str]]]:
    parsed = urllib.parse.urlsplit(path)
    return parsed.path, urllib.parse.parse_qs(parsed.query)


def _good_api(path: str):
    route, params = _query(path)
    if route == "/runs":
        assert params.get("limit") == ["1"]
        return 200, {"total": 1, "runs": [{"id": 1}]}
    if route == "/mrs":
        since = (params.get("since") or [""])[0]
        if since.startswith("2026-11-08T12"):
            return 200, {"total": 0, "mrs": []}
        return 200, {"total": 1, "mrs": [{"mr_iid": MR}]}
    if route == "/dismissals":
        if params.get("rule_key") == ["__none__"]:
            return 200, {"total": 0, "dismissals": []}
        if params.get("until"):
            return 200, {"total": 0, "dismissals": []}
        if params.get("offset") == ["1"]:
            return 200, {"total": 2, "dismissals": [{"note_id": "d9", "rule_keys": "R-ERR"}]}
        return 200, {"total": 2, "dismissals": [{"note_id": "d1", "rule_keys": "R-ERR"}]}
    if route == "/metrics/rules":
        return 200, {"rules": [
            {"rule_key": "R-ERR", "count": 1},
            {"rule_key": "SSD-RULE-FORBIDDEN-WILDCARD-IMPORT", "count": 1},
        ]}
    if route == "/metrics/authors":
        return 200, {"authors": [
            {"author": "review-tester@root", "mrs": 1, "runs": 1},
        ]}
    raise AssertionError(f"unexpected path {path}")


def _dismissed_snapshot(**over):
    over.setdefault("suggestions", [
        _sug(state="dismissed", dismissed_at="2026-11-08T10:03:00+00:00",
             dismissed_by="review-tester@root", dismissed_reason="不要 (e2e 验证)"),
        _sug(id=2, note_id="d2", file_path="app/service.py", state="dismissed",
             dismissed_at="2026-11-08T10:04:00+00:00", rule_keys=""),
    ])
    over.setdefault("actions", [])
    over.setdefault("project_runs", 1)
    over.setdefault("project_mrs", 1)
    return _snapshot(**over)


def test_api_params_pass_with_real_db_values() -> None:
    probe = _Probe(_good_api)
    ok, detail = ta.audit_api_params(_dismissed_snapshot(), _expect(), probe)
    assert ok, detail
    routes = {_query(p)[0] for p in probe.calls}
    assert {"/runs", "/mrs", "/dismissals", "/metrics/rules", "/metrics/authors"} <= routes


def test_api_params_fail_when_runs_total_is_page_size() -> None:
    def handler(path):
        route, _ = _query(path)
        if route == "/runs":
            return 200, {"total": 1, "runs": [{"id": 1}]}
        return _good_api(path)

    snap = _dismissed_snapshot(runs=[_run(), _run(id=2, started_at="2026-11-08T10:05:00+00:00",
                                                  finished_at="2026-11-08T10:06:00+00:00")])
    ok, detail = ta.audit_api_params(snap, _expect(), _Probe(handler))
    assert ok is False and "/runs" in detail


def test_api_params_fail_when_mrs_window_uses_updated_at() -> None:
    def handler(path):
        route, params = _query(path)
        if route == "/mrs":
            # 复现旧 bug: 无论窗口都返回 MR (按 updated_at 过滤)
            return 200, {"total": 1, "mrs": [{"mr_iid": MR}]}
        return _good_api(path)

    ok, detail = ta.audit_api_params(_dismissed_snapshot(), _expect(), _Probe(handler))
    assert ok is False and "/mrs" in detail


def test_api_params_fail_when_rule_key_filter_ignored() -> None:
    def handler(path):
        route, params = _query(path)
        if route == "/dismissals" and params.get("rule_key") == ["__none__"]:
            return 200, {"total": 2, "dismissals": [{"note_id": "d1", "rule_keys": "R-ERR"}]}
        return _good_api(path)

    ok, detail = ta.audit_api_params(_dismissed_snapshot(), _expect(), _Probe(handler))
    assert ok is False and "rule_key" in detail


def test_api_params_until_covers_earliest_dismissed_at() -> None:
    """until 边界必须按最早 dismissed_at, 不能按 created_at 顺序取首条."""
    seen: list[str] = []

    def handler(path):
        route, params = _query(path)
        if route == "/dismissals" and params.get("until"):
            seen.append(params["until"][0])
            return 200, {"total": 1, "dismissals": [{"note_id": "d0"}]}
        return _good_api(path)

    snap = _dismissed_snapshot(suggestions=[
        _sug(state="dismissed", dismissed_at="2026-11-08T10:03:00+00:00"),
        _sug(id=2, note_id="d2", file_path="app/service.py", state="dismissed",
             dismissed_at="2026-11-08T10:00:00+00:00", rule_keys=""),
    ])
    ok, detail = ta.audit_api_params(snap, _expect(), _Probe(handler))
    assert ok is False and "until" in detail
    assert seen and seen[0] < "2026-11-08T10:00:00+00:00"


@pytest.mark.parametrize("path_marker", ["/metrics/rules"])
def test_api_params_fail_when_rule_metrics_fall_back_to_severity(path_marker: str) -> None:
    def handler(path):
        route, _ = _query(path)
        if route == path_marker:
            return 200, {"rules": [{"rule_key": "high", "count": 3}]}
        return _good_api(path)

    ok, detail = ta.audit_api_params(_dismissed_snapshot(), _expect(), _Probe(handler))
    assert ok is False and "rule" in detail
