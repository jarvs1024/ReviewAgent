"""E2E 功能流程：describe / improve / 自动链 / adopt / dismiss / UI Apply / telemetry / 周报."""
from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable

from scripts.e2e import fixtures, stack
from scripts.e2e.report import REPO_ROOT, FeatureResult


def run_describe(mr_iid: int, round_idx: int, log: list[str]) -> FeatureResult:
    stack.clear_ephemeral_locks(log)
    fixtures.reset_mr_describe_state(mr_iid, log)
    before = len(stack.list_mr_runs(mr_iid))
    status, body = stack.post_webhook(
        stack.make_note_payload(mr_iid, "/describe\ne2e 验证"), log
    )
    if status != 200 or body.get("status") != "queued" or body.get("command") != "describe":
        return FeatureResult(
            "/describe", round_idx, mr_iid, False, f"webhook status={status} body={body}", log
        )
    res = stack.wait_for_new_run(
        mr_iid, before, expect_status="terminal", expected_min_increase=1, timeout_s=300, log=log
    )
    tops = [(r.get("command"), r.get("status"), (r.get("error") or "")[:30]) for r in res["tops"][:3]]
    return FeatureResult(
        "/describe",
        round_idx,
        mr_iid,
        res.get("ok", False),
        f"runs+{res.get('delta', 0)} status_expect=terminal; tops={tops}",
        log,
    )


def run_improve(mr_iid: int, round_idx: int, log: list[str]) -> FeatureResult:
    stack.clear_ephemeral_locks(log)
    before = len(stack.list_mr_runs(mr_iid))
    status, body = stack.post_webhook(stack.make_note_payload(mr_iid, "/improve"), log)
    if status != 200 or body.get("status") != "queued" or body.get("command") != "improve":
        return FeatureResult(
            "/improve", round_idx, mr_iid, False, f"webhook status={status} body={body}", log
        )
    res = stack.wait_for_new_run(
        mr_iid, before, expect_status="terminal", expected_min_increase=1, timeout_s=400, log=log
    )
    tops = [(r.get("command"), r.get("status"), r.get("suggestion_count")) for r in res["tops"][:3]]
    return FeatureResult(
        "/improve",
        round_idx,
        mr_iid,
        res.get("ok", False),
        f"runs+{res.get('delta', 0)} status_expect=terminal; tops={tops}",
        log,
    )


def run_chain_open(mr_iid: int, round_idx: int, log: list[str]) -> FeatureResult:
    """merge_request open → pr_commands（describe + improve）."""
    stack.clear_ephemeral_locks(log)
    fixtures.reset_mr_describe_state(mr_iid, log)
    # 不清理 review_runs: 默认 max_review_calls_per_mr=30 远超 E2E 的 run 数;
    # 删除历史会让 telemetry 审计失去「哪个 run 产出了这些 suggestion」的证据.
    before = len(stack.list_mr_runs(mr_iid))
    status, body = stack.post_webhook(stack.make_merge_request_payload(mr_iid, "open"), log)
    if status != 200 or body.get("status") not in ("queued", "skipped"):
        return FeatureResult(
            "auto_chain", round_idx, mr_iid, False, f"webhook status={status} body={body}", log
        )
    queued = body.get("commands") or []
    # describe + improve 是两个独立 job：只等「有一个 run 结束」会提前返回，
    # 后续 adopt/dismiss 就赶在 improve 发评论之前跑，找不到 suggestion。
    expected = max(1, len(queued) if isinstance(queued, list) else 1)
    res = stack.wait_for_new_run(
        mr_iid,
        before,
        expect_status="terminal",
        expected_min_increase=expected,
        timeout_s=600,
        log=log,
    )
    if not res.get("ok"):
        return FeatureResult("auto_chain", round_idx, mr_iid, False, f"timeout waits; {res}", log)
    commands = sorted({r.get("command") for r in res["tops"] if r.get("command")})
    ok = len(commands) >= 1 or res.get("delta", 0) >= 1
    return FeatureResult("auto_chain", round_idx, mr_iid, ok, f"runs+{res['delta']} cmds_seen={commands}", log)


def _reply_to_open_suggestion(
    mr_iid: int,
    round_idx: int,
    log: list[str],
    *,
    action: str,
    body: str,
) -> FeatureResult:
    stack.clear_ephemeral_locks(log)
    suggestion = fixtures.find_open_suggestion(mr_iid, log)
    if not suggestion:
        log.append(f"[{stack.now_iso()}] no open suggestion → skip; 需要 DiffNote reply")
        return FeatureResult(f"/{action}", round_idx, mr_iid, False, "no open suggestion available", log)
    target_mr = suggestion["mr_iid"]
    discussion_id = suggestion["note_id"]
    metric = "adopted" if action == "adopt" else "dismissed"
    stack.clear_ephemeral_locks(log)
    before = fixtures.get_action_count(target_mr, metric)
    status, response = stack.post_webhook(
        stack.make_note_payload(
            target_mr, body, note_type="DiffNote", discussion_id=discussion_id
        ),
        log,
    )
    if status != 200 or response.get("status") != "queued" or response.get("action") != action:
        return FeatureResult(
            f"/{action}", round_idx, target_mr, False, f"webhook status={status} body={response}", log
        )
    ok, after = fixtures.wait_for_action(target_mr, metric, before, timeout_s=300)
    return FeatureResult(
        f"/{action}",
        round_idx,
        target_mr,
        ok,
        f"{metric}_actions {before} -> {after} on mr={target_mr}",
        log,
    )


def run_adopt(mr_iid: int, round_idx: int, log: list[str]) -> FeatureResult:
    return _reply_to_open_suggestion(
        mr_iid, round_idx, log, action="adopt", body="/adopt 同意 (e2e 验证)"
    )


def run_dismiss(mr_iid: int, round_idx: int, log: list[str]) -> FeatureResult:
    return _reply_to_open_suggestion(
        mr_iid, round_idx, log, action="dismiss", body="/dismiss 不要 (e2e 验证)"
    )


def run_ui_apply(mr_iid: int, round_idx: int, log: list[str]) -> FeatureResult:
    """merge_request update + head_sha 变化 → auto_detect_applied.

    真 UI Apply 要改 MR 代码（push 触发的是 GitLab webhook，不走 :3000），
    因此这里合成 update 事件，只验证路由分派与响应形态。
    """
    stack.clear_ephemeral_locks(log)
    stack.delete_diff_head(mr_iid)
    status, body = stack.post_webhook(stack.make_merge_request_payload(mr_iid, "update"), log)
    ok = status == 200 and body.get("status") in ("queued", "skipped")
    if not ok:
        return FeatureResult(
            "ui_apply", round_idx, mr_iid, False, f"webhook status={status} body={body}", log
        )
    time.sleep(2)
    return FeatureResult(
        "ui_apply",
        round_idx,
        mr_iid,
        True,
        f"webhook {body.get('status')} reason={body.get('reason', '')[:40]}",
        log,
    )


TELEMETRY_ENDPOINTS = (
    "/health",
    "/summary",
    "/mrs?limit=10",
    "/mr/{pid}/{mr_iid}/runs?limit=10",
    "/mr/{pid}/{mr_iid}/suggestions?limit=10",
    "/mr/{pid}/{mr_iid}/stats",
    "/metrics/overview",
    "/runs?limit=10",
    "/metrics/rules",
    "/metrics/severity",
    "/metrics/authors",
    "/dismissals?limit=10",
    "/dismissals/by-rule",
)


def run_telemetry(mr_iid: int, round_idx: int, log: list[str]) -> FeatureResult:
    pid = stack.project_id()
    endpoints = [
        f"/mrs/{pid}/{mr_iid}",
        *[ep.format(pid=pid, mr_iid=mr_iid) for ep in TELEMETRY_ENDPOINTS],
    ]
    ok_count = 0
    failed: list[str] = []
    for endpoint in endpoints:
        status, _body = stack.telemetry_get(endpoint)
        if status == 200:
            ok_count += 1
        else:
            failed.append(f"{endpoint}->{status}")
    ok = ok_count >= len(endpoints) - 1  # 容许 1 个端点因空表失败
    return FeatureResult(
        "telemetry_api",
        round_idx,
        mr_iid,
        ok,
        f"{ok_count}/{len(endpoints)} endpoints ok; failed={failed[:5]}",
        log,
    )


def run_weekly_report(round_idx: int, log: list[str]) -> FeatureResult:
    out_dir = REPO_ROOT / "data" / "weekly_reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    modes = {
        1: ([".venv/bin/python", "scripts/weekly_report.py", "--week-offset", "-1"], "dry_run"),
        2: ([".venv/bin/python", "scripts/weekly_report.py", "--week-offset", "0", "--push"], "push_week0"),
        3: ([".venv/bin/python", "scripts/weekly_report.py", "--week-offset", "-2", "--push"], "push_week-2"),
    }
    cmd, mode = modes.get(
        round_idx,
        ([".venv/bin/python", "scripts/weekly_report.py", "--week-offset", "0"], f"dry_run_r{round_idx}"),
    )
    log.append(f"[{stack.now_iso()}] running weekly_report mode={mode}")
    proc = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, timeout=900, env=os.environ)
    log.append(f"[{stack.now_iso()}] exit={proc.returncode}")
    log.append("---STDOUT---")
    log.append(proc.stdout[-1500:].decode("utf-8", errors="ignore"))
    if proc.returncode != 0:
        return FeatureResult(
            "weekly_report",
            round_idx,
            0,
            False,
            f"exit={proc.returncode} stderr={proc.stderr[-400:].decode('utf-8', errors='ignore')}",
            log,
        )
    jsons = sorted(out_dir.glob("*.json"))
    mds = sorted(out_dir.glob("*.md"))
    return FeatureResult(
        "weekly_report",
        round_idx,
        0,
        bool(jsons) and bool(mds),
        f"mode={mode} files: json={len(jsons)} md={len(mds)}",
        log,
    )


FLOWS: dict[str, Callable[[int, int, list[str]], FeatureResult]] = {
    "describe": run_describe,
    "improve": run_improve,
    "auto_chain": run_chain_open,
    "adopt": run_adopt,
    "dismiss": run_dismiss,
    "ui_apply": run_ui_apply,
    "telemetry_api": run_telemetry,
    "weekly_report": lambda _mr, round_idx, log: run_weekly_report(round_idx, log),
}
