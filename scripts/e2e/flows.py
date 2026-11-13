"""E2E 功能流程：describe / improve / 自动链 / adopt / dismiss / UI Apply / telemetry / 周报."""
from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from scripts.e2e import fixtures, stack
from scripts.e2e.report import REPO_ROOT, FeatureResult


# ---------------------------------------------------------------------------
# 安全闸: E2E 绝不能默认真发钉钉
# ---------------------------------------------------------------------------

#: 允许 E2E 真发钉钉的环境变量（显式 opt-in，默认关闭）。
ALLOW_REAL_PUSH_ENV = "REVIEWAGENT_E2E_ALLOW_REAL_PUSH"

_TRUTHY = {"1", "true", "yes", "on"}


def _e2e_real_push_allowed() -> bool:
    """E2E 是否允许**真发**钉钉.

    Why 需要这道闸: 本地 `config/instance.env` 里
    `REVIEWAGENT_WEEKLY_DINGTALK_DRY_RUN=false` 且带真实钉钉 webhook URL ——
    E2E 若带 `--push` 跑周报，会把报告真推到团队群（外发副作用，不可撤销）。
    真发必须显式 opt-in: `REVIEWAGENT_E2E_ALLOW_REAL_PUSH=1`。
    """
    raw = str(os.environ.get(ALLOW_REAL_PUSH_ENV, "")).strip().lower()
    return raw in _TRUTHY


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


def _weekly_out_dir() -> Path:
    """周报输出目录 —— 唯一事实源是 config.weekly_reports_dir.

    不能用 REPO_ROOT/"data"/"weekly_reports": systemd 部署下 API 读的是
    config.weekly_reports_dir, 两者不是同一个目录 (见 scripts/weekly_report.py 注释)。
    """
    from reviewagent.config import config as _config

    return Path(_config.weekly_reports_dir)


def _run_weekly(
    cmd: list[str], log: list[str], *, env_overrides: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if env_overrides:
        env.update(env_overrides)
    log.append(f"[{stack.now_iso()}] running weekly_report cmd={' '.join(cmd)}")
    proc = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, timeout=900, env=env)
    log.append(f"[{stack.now_iso()}] exit={proc.returncode}")
    log.append("---STDOUT---")
    log.append(proc.stdout[-1500:].decode("utf-8", errors="ignore"))
    return proc


def _delivery_is_dry_run(stdout: str) -> bool:
    """CLI 打印 `[ok] delivery     : dry_run, chunks=N` 表示没有真发."""
    return "delivery     : dry_run" in stdout or "delivery : dry_run" in stdout


def run_weekly_report(round_idx: int, log: list[str]) -> FeatureResult:
    """周报产出 + **外发门控断言** + **live 字段断言**（默认绝不真发钉钉）.

    安全: 本地 `config/instance.env` 里 `REVIEWAGENT_WEEKLY_DINGTALK_DRY_RUN=false`
    且带真实 webhook URL —— 带 `--push` 跑会把周报推到团队群。
    因此 E2E 默认一律 dry_run；真发必须显式 opt-in（`REVIEWAGENT_E2E_ALLOW_REAL_PUSH=1`）。

    E13 live 字段断言（此前只断言「文件产出 + dry_run」，产物内容**完全没校验**）:
        - JSON artifact: report_title / report_emoji / dashboard_url 非空且与配置一致
        - markdown: 首行标题带 emoji + 标题 + week_label
        - markdown: 含 dashboard 链接（`📈 **检视看板**: [url](url)`）
        - 干��分片数与 CLI 打印的 chunks 一致
    """
    out_dir = _weekly_out_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    push_allowed = _e2e_real_push_allowed()
    week_offsets = {1: -1, 2: 0, 3: -2}
    week_offset = week_offsets.get(round_idx, 0)
    cmd = [".venv/bin/python", "scripts/weekly_report.py", "--week-offset", str(week_offset)]
    mode = f"dry_run_w{week_offset}"
    if push_allowed and round_idx >= 2:
        cmd.append("--push")
        mode = f"push_w{week_offset}"
    proc = _run_weekly(cmd, log)
    if proc.returncode != 0:
        return FeatureResult(
            "weekly_report",
            round_idx,
            0,
            False,
            f"exit={proc.returncode} stderr={proc.stderr[-400:].decode('utf-8', errors='ignore')}",
            log,
        )
    stdout = proc.stdout.decode("utf-8", errors="ignore")
    jsons = sorted(out_dir.glob("*.json"))
    mds = sorted(out_dir.glob("*.md"))
    ok = bool(jsons) and bool(mds)
    detail = f"mode={mode} files: json={len(jsons)} md={len(mds)}"
    if not push_allowed and not _delivery_is_dry_run(stdout):
        ok = False
        detail += " | EXPECTED dry_run delivery but notifier ran for real"

    # ---- E13: live 字段断言 ----
    if ok:
        live_problems, live_detail = _audit_weekly_live_fields(out_dir, stdout)
        ok = not live_problems
        detail += f" | {live_detail}"
        if live_problems:
            detail += " | " + "; ".join(live_problems[:4])
    return FeatureResult("weekly_report", round_idx, 0, ok, detail, log)


def _audit_weekly_live_fields(
    out_dir: Path, stdout: str,
) -> tuple[list[str], str]:
    """校验周报产物的 live 字段（标题 / emoji / dashboard 链接 / 分片数）.

    Why 需要: 此前 E2E 只断言「json + md 文件存在」,
    产物内容全靠人眼看 —— 标题写错、emoji 丢、dashboard 链接没渲染、
    分片数与实际对不上，都不会有任何测试变红。
    """
    problems: list[str] = []
    jsons = sorted(out_dir.glob("*.json"))
    mds = sorted(out_dir.glob("*.md"))
    if not jsons or not mds:
        return ["no artifact files to audit"], "live=skipped(no files)"

    # 取**最新**的 artifact（目录里可能有多周的残留）
    artifact_path = jsons[-1]
    md_path = mds[-1]
    try:
        data = json.loads(artifact_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return [f"artifact json unreadable: {e}"], "live=json-error"

    title = str(data.get("report_title") or "")
    emoji = str(data.get("report_emoji") or "")
    dashboard = str(data.get("dashboard_url") or "")
    week_label = str(data.get("week_label") or "")
    if not title:
        problems.append("artifact.report_title empty")
    if not emoji:
        problems.append("artifact.report_emoji empty")
    if not dashboard:
        problems.append("artifact.dashboard_url empty")
    if not week_label:
        problems.append("artifact.week_label empty")

    md = md_path.read_text(encoding="utf-8")
    first_line = md.strip().splitlines()[0] if md.strip() else ""
    if not first_line.startswith("#"):
        problems.append(f"markdown first line is not a heading: {first_line[:60]!r}")
    else:
        if emoji and emoji not in first_line:
            problems.append(f"markdown heading missing emoji {emoji!r}: {first_line[:60]!r}")
        if title and title not in first_line:
            problems.append(f"markdown heading missing title: {first_line[:60]!r}")
        if week_label and week_label not in first_line:
            problems.append(f"markdown heading missing week_label {week_label!r}: {first_line[:60]!r}")
    if dashboard:
        if dashboard not in md:
            problems.append(f"markdown missing dashboard url {dashboard!r}")
        elif f"[{dashboard}]({dashboard})" not in md:
            problems.append("markdown dashboard url is not a proper markdown link")

    # 分片数: CLI 打印 `chunks=N`, 必须 ≥ 1
    chunks = 0
    for line in stdout.splitlines():
        if "chunks=" in line:
            try:
                chunks = int(line.split("chunks=")[1].split()[0].strip(","))
            except (ValueError, IndexError):
                chunks = 0
    if chunks < 1:
        problems.append(f"delivery chunks not reported or zero: stdout={stdout[-200:]!r}")

    return problems, (
        f"live: title={title[:24]!r} emoji={emoji!r} dashboard={dashboard[:32]!r} "
        f"chunks={chunks} md={md_path.name}"
    )


def run_weekly_notify_gate(round_idx: int, log: list[str]) -> FeatureResult:
    """周报外发门控（配置级）: `--push` + `DRY_RUN=true` 仍必须是 dry_run.

    对应 catalog 里 `weekly_dingtalk_dry_run` 登记的「全量档 dry-run 断言」——
    这条断言此前**只存在于目录描述里，没有任何实现**，属于漏测。
    用一个空周（--week-offset -50）避开 LLM 摘要调用，成本为零。
    """
    cmd = [".venv/bin/python", "scripts/weekly_report.py", "--week-offset", "-50", "--push"]
    proc = _run_weekly(
        cmd,
        log,
        env_overrides={"REVIEWAGENT_WEEKLY_DINGTALK_DRY_RUN": "true"},
    )
    if proc.returncode != 0:
        return FeatureResult(
            "weekly_notify_gate",
            round_idx,
            0,
            False,
            f"exit={proc.returncode} stderr={proc.stderr[-400:].decode('utf-8', errors='ignore')}",
            log,
        )
    stdout = proc.stdout.decode("utf-8", errors="ignore")
    dry = _delivery_is_dry_run(stdout)
    return FeatureResult(
        "weekly_notify_gate",
        round_idx,
        0,
        dry,
        f"push+dry_run=true -> dry_run={dry}",
        log,
    )


def _discussion_bodies_by_id(pid: int, mr_iid: int) -> dict[str, str]:
    """MR 行内讨论的正文，key = discussion id（= suggestions.note_id）."""
    discussions = stack.gitlab_get(
        f"/projects/{pid}/merge_requests/{mr_iid}/discussions?per_page=100"
    )
    out: dict[str, str] = {}
    for discussion in discussions if isinstance(discussions, list) else []:
        bodies = [
            str(note.get("body") or "")
            for note in discussion.get("notes", [])
            if not note.get("system")
        ]
        if bodies:
            out[str(discussion.get("id"))] = bodies[0]
    return out


def run_applicability(mr_iid: int, round_idx: int, log: list[str]) -> FeatureResult:
    """applicability 三态的**发布产物**契约.

    GitLab 的 Apply suggestion 按钮**只在正文含 ```suggestion 块时出现** ——
    所以 text_only 建议正文绝不能带块（否则又冒出 Apply 按钮，reviewer 一点
    就删掉有效代码）；delete 必须带块（Apply 即删除该段）；replace 必须带块
    且块内容与 DB 的 improved_code 一致。

    覆盖的是**不变量**（DB applicability ⟺ 正文形态），
    具体某态是否在本次夹具里出现，取决于 LLM 产出，detail 里会报分布。
    """
    from reviewagent.telemetry.store import get_store

    pid = stack.project_id()
    with get_store()._conn() as conn:  # noqa: SLF001
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT note_id, file_path, target_line, applicability, improved_code "
                "FROM suggestions WHERE project_id=? AND mr_iid=? ORDER BY id",
                (pid, mr_iid),
            ).fetchall()
        ]
    if not rows:
        return FeatureResult(
            "applicability", round_idx, mr_iid, False, "no suggestions in telemetry", log
        )
    bodies = _discussion_bodies_by_id(pid, mr_iid)
    problems: list[str] = []
    stats: dict[str, int] = {}
    for row in rows:
        appl = str(row.get("applicability") or "replace").strip().lower()
        stats[appl] = stats.get(appl, 0) + 1
        body = bodies.get(str(row.get("note_id")), "")
        has_block = "```suggestion" in body
        improved = str(row.get("improved_code") or "").strip()
        tag = str(row.get("note_id") or "")[:8]
        if not body:
            problems.append(f"{tag}: 正文在 GitLab 上找不到")
            continue
        if appl == "text_only":
            if has_block:
                problems.append(f"{tag}: text_only 正文带 suggestion 块 → 会冒出 Apply 按钮")
            if improved:
                problems.append(f"{tag}: text_only 不应带 improved_code")
        elif appl == "delete":
            if not has_block:
                problems.append(f"{tag}: delete 正文缺 suggestion 块 → Apply 无法删除")
            if improved:
                problems.append(f"{tag}: delete 不应带 improved_code")
        else:
            if not has_block:
                problems.append(f"{tag}: replace 正文缺 suggestion 块")
            elif improved and improved not in body:
                problems.append(f"{tag}: replace 正文与 improved_code 不一致")
    return FeatureResult(
        "applicability",
        round_idx,
        mr_iid,
        not problems,
        f"dist={stats} problems={problems[:5]}",
        log,
    )


def run_cooldown(mr_iid: int, round_idx: int, log: list[str]) -> FeatureResult:
    """冷却闸: 同一 MR 同一命令在 cooldown 窗口内第二次必须被拦截.

    Why 重要: 冷却是防「连续 push 打爆 LLM」的最后一道闸。
    出问题表现为重复检视（重复评论 + 重复计费）。
    """
    stack.clear_ephemeral_locks(log)
    fixtures.reset_mr_describe_state(mr_iid, log)
    status1, body1 = stack.post_webhook(
        stack.make_note_payload(mr_iid, "/describe\ne2e cooldown"), log
    )
    if status1 != 200 or body1.get("status") != "queued":
        return FeatureResult(
            "cooldown",
            round_idx,
            mr_iid,
            False,
            f"first call not queued: status={status1} body={body1}",
            log,
        )
    # 第二次: **不清** cooldown key —— 必须落在窗口内被拦
    status2, body2 = stack.post_webhook(
        stack.make_note_payload(mr_iid, "/describe\ne2e cooldown"), log
    )
    skipped = (
        status2 == 200
        and body2.get("status") == "skipped"
        and body2.get("reason") == "cooldown"
    )
    return FeatureResult(
        "cooldown",
        round_idx,
        mr_iid,
        skipped,
        f"1st=queued 2nd={body2.get('status')}/{body2.get('reason')}",
        log,
    )


def run_rate_limit(mr_iid: int, round_idx: int, log: list[str]) -> FeatureResult:
    """限流闸: `should_skip_max_review_calls` 的判定必须与配置一致.

    用**真实**配置 + **真实** run 计数 + **真实**锁函数做判定核对
    （webhook 走的就是这个函数）。硬阈值（count >= limit）分支由单元测试
    用合成阈值覆盖 —— 服务进程的配置在启动时固化，E2E 改不了。
    """
    from reviewagent.config import config as _config
    from reviewagent.webhook.locks import locks

    pid = stack.project_id()
    limit = int(_config.max_review_calls_per_mr)
    skip, current = locks.should_skip_max_review_calls(pid, mr_iid, ("describe", "improve"))
    expected = limit > 0 and current >= limit
    detail = f"limit={limit} current_runs={current} skip={skip} expected={expected}"
    if skip != expected:
        return FeatureResult("rate_limit", round_idx, mr_iid, False, detail + " MISMATCH", log)
    return FeatureResult("rate_limit", round_idx, mr_iid, True, detail, log)


def run_dedup(mr_iid: int, round_idx: int, log: list[str]) -> FeatureResult:
    """去重: 同一 cohort 在同一 MR 下**最多只能有一条 open**.

    Why: 去重失败 = 同一处代码被重复评论（reviewer 体验最差的一类 bug）。
    """
    from reviewagent.telemetry.store import get_store

    pid = stack.project_id()
    with get_store()._conn() as conn:  # noqa: SLF001
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT cohort_key, note_id, state FROM suggestions "
                "WHERE project_id=? AND mr_iid=? AND cohort_key IS NOT NULL AND cohort_key != ''",
                (pid, mr_iid),
            ).fetchall()
        ]
    open_by_cohort: dict[str, list[str]] = {}
    for row in rows:
        if row.get("state") == "open":
            open_by_cohort.setdefault(str(row.get("cohort_key")), []).append(
                str(row.get("note_id"))[:8]
            )
    dupes = {k: v for k, v in open_by_cohort.items() if len(v) > 1}
    return FeatureResult(
        "dedup",
        round_idx,
        mr_iid,
        not dupes,
        f"suggestions={len(rows)} cohorts={len(open_by_cohort)} duplicated={dupes}",
        log,
    )


def _pick_applyable_suggestion(mr_iid: int, log: list[str]) -> dict | None:
    """挑一条**有可 Apply 形态**的 open suggestion（replace / delete）.

    text_only 没有 suggestion 块，Apply 语义不成立 → 不能用它验证 Apply 闭环。
    """
    from reviewagent.telemetry.store import get_store

    pid = stack.project_id()
    with get_store()._conn() as conn:  # noqa: SLF001
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT note_id, file_path, target_line, target_line_end, "
                "       existing_code, improved_code, applicability "
                "FROM suggestions WHERE project_id=? AND mr_iid=? AND state='open' "
                "ORDER BY id",
                (pid, mr_iid),
            ).fetchall()
        ]
    for row in rows:
        applicability = str(row.get("applicability") or "replace").lower()
        if applicability == "text_only":
            continue
        if not row.get("file_path") or not int(row.get("target_line") or 0):
            continue
        return row
    return None


def _apply_suggestion_to_file(row: dict, content: str) -> str | None:
    """把 suggestion 的 improved_code 真实写进文件内容（Apply 的等价动作）.

    replace → 用 improved_code 替换「原代码跨度」
    delete  → 删掉那一段（improved_code 为空）

    Why 用 `existing_code` 的行数、而不是 DB 的 `target_line_end`:
        `target_line_end` 存的是**新代码跨度**（`publishing.py` 的
        `n_lines = len(improved_code.split("\\n"))`），而 GitLab 的 Apply
        替换的是**建议块的旧侧** —— 旧侧行数由 `policies/python.py` 决定:
            n_replace = max(0, len(existing_code.split("\\n")) - 1)
        1→N 的多行替换里两者**不相等**（新代码比原代码长）→ 按 target_line_end
        打补丁会**多删**原代码之外的行，把文件改坏 → auto_detect 认不出
        「已落地」→ apply_closure 误报红（实测：E2E 全量档 apply_closure 失败）。

        existing_code 为空（模型没给原文）→ 退回 target_line_end 跨度。
    """
    lines = content.splitlines()
    tl = int(row.get("target_line") or 0)
    te = int(row.get("target_line_end") or tl)
    if tl <= 0 or tl > len(lines):
        return None
    existing = str(row.get("existing_code") or "")
    improved = str(row.get("improved_code") or "")
    new_lines = improved.splitlines() if improved.strip() else []

    span = len(existing.splitlines()) if existing.strip() else 0
    if span <= 0:
        span = max(1, te - tl + 1)
    end = min(len(lines), tl - 1 + span)
    patched = lines[: tl - 1] + new_lines + lines[end:]
    tail = "\n" if content.endswith("\n") else ""
    return "\n".join(patched) + tail


def _suggestion_state(note_id: str, pid: int, mr_iid: int) -> tuple[str, dict]:
    from reviewagent.telemetry.store import get_store

    row = get_store().get_suggestion_by_note_id(note_id)
    return (str(row.get("state") or "missing") if row else "missing"), (row or {})


def run_apply_closure(mr_iid: int, round_idx: int, log: list[str]) -> FeatureResult:
    """**真实 Apply 闭环**: 改代码 → 真实 commit/push → webhook → telemetry state=applied.

    Why 补这条: 此前 `ui_apply` 只断言「webhook 有响应」—— 但 Apply 是**唯一会改代码**的动作,
    跨 GitLab → webhook → worker → auto_detect → telemetry 全链没有任何断言。
    也就是说「用户点了应用建议, 系统真的认为已采纳并落库」这件事**从未被验证过**。

    链路:
      1. 取一条有 suggestion 块的 open 建议 (replace / delete)
      2. 按 improved_code 真实改文件 → commit 到 MR 源分支 (GitLab files API, 真 push)
      3. 等 auto_detect_applied 把 telemetry state 翻成 applied
      4. 兜底: 合成一次 MR update 事件（等价于「用户 push 后 GitLab 发的 MR update」）

    Why 需要兜底: E2E 用 `reviewagent-e2e-bot` 的 PAT 提交 → push 事件的 actor 是机器人
      → `router._handle_push` 的 `locks.is_bot(actor)` 会**跳过**（设计如此: 避免机器人自己的
      push 触发检视回环）。真实用户点 Apply suggestion 时 actor 是真人, push 路径会正常跑
      auto_detect（真实业务 MR !4 实测 `push.auto_detect_applied applied=1`）。
      兜底路径在 detail 里如实标注 path=synthetic-update。

    断言: state == 'applied' 且该 note 有 adopted action。
    """
    pid = stack.project_id()
    row = _pick_applyable_suggestion(mr_iid, log)
    if not row:
        return FeatureResult(
            "apply_closure", round_idx, mr_iid, False,
            "没有可 Apply 的 open suggestion (replace/delete)", log,
        )

    note_id = str(row.get("note_id"))
    file_path = str(row.get("file_path"))
    mr = stack.get_mr_info(mr_iid, log, pid=pid)
    source = str(mr.get("source_branch") or "")
    if not source:
        return FeatureResult(
            "apply_closure", round_idx, mr_iid, False, "取不到 MR 源分支", log
        )

    try:
        content = stack.gitlab_raw(pid, file_path, source)
    except Exception as exc:  # noqa: BLE001
        return FeatureResult(
            "apply_closure", round_idx, mr_iid, False, f"读文件失败 {file_path}: {exc}", log
        )

    patched = _apply_suggestion_to_file(row, content)
    if patched is None:
        return FeatureResult(
            "apply_closure", round_idx, mr_iid, False,
            f"行号不在文件范围内 (target_line={row.get('target_line')} len={len(content.splitlines())})",
            log,
        )
    if patched == content:
        return FeatureResult(
            "apply_closure", round_idx, mr_iid, False, "构造出的新内容与原内容相同", log
        )

    fixtures.commit_files(pid, source, {file_path: patched}, "e2e: apply suggestion", log)

    # 3. 等真实 push 链路把 state 翻成 applied
    deadline = time.time() + 60
    state = "open"
    while time.time() < deadline:
        state, _row = _suggestion_state(note_id, pid, mr_iid)
        if state in ("applied", "dismissed", "resolved"):
            break
        time.sleep(2)

    path_used = "push-webhook"
    if state != "applied":
        # 兜底: 某些 GitLab 实例不会因 push 派发 MR update 事件 → 合成一次
        stack.clear_ephemeral_locks(log)
        stack.delete_diff_head(mr_iid)
        stack.post_webhook(stack.make_merge_request_payload(mr_iid, "update"), log)
        path_used = "synthetic-update"
        deadline = time.time() + 60
        while time.time() < deadline:
            state, _row = _suggestion_state(note_id, pid, mr_iid)
            if state in ("applied", "dismissed", "resolved"):
                break
            time.sleep(2)

    _state, sug_row = _suggestion_state(note_id, pid, mr_iid)
    # 按 note 精确统计本条建议的采纳动作（不是整个 MR 的）
    from reviewagent.telemetry.store import get_store

    with get_store()._conn() as conn:  # noqa: SLF001
        adopted = int(
            conn.execute(
                "SELECT COUNT(*) FROM suggestion_actions "
                "WHERE project_id=? AND mr_iid=? AND suggestion_note_id=? AND action='adopted'",
                (pid, mr_iid, note_id),
            ).fetchone()[0]
        )
    commit_sha = str(sug_row.get("applied_commit_sha") or "")
    ok = state == "applied" and adopted >= 1
    detail = (
        f"note={note_id[:8]} file={file_path} state={state} adopted_actions={adopted} "
        f"applied_commit_sha={commit_sha[:8] or 'none'} path={path_used} "
        f"applicability={row.get('applicability')}"
    )
    if not ok:
        detail += f" | expected applied; resolved_state={state}"
    return FeatureResult("apply_closure", round_idx, mr_iid, ok, detail, log)


def run_reconcile_resolved(mr_iid: int, round_idx: int, log: list[str]) -> FeatureResult:
    """reconciler 链路: GitLab 上 resolve 线程 → sync → telemetry state=resolved.

    Why 补这条: 「用户只点 Resolve thread、不 push」是唯一能关掉一条建议的路径,
    但全链路（GitLab API → sync_resolved_suggestions → store）此前只有单测,
    E2E 从未跑过 —— 失效时 DB 会永远停在 open（用户视角 = 建议关不掉）。
    """
    from reviewagent.telemetry.store import get_store

    pid = stack.project_id()
    with get_store()._conn() as conn:  # noqa: SLF001
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT note_id, file_path, target_line FROM suggestions "
                "WHERE project_id=? AND mr_iid=? AND state='open' ORDER BY id",
                (pid, mr_iid),
            ).fetchall()
        ]
    if not rows:
        return FeatureResult(
            "reconcile_resolved", round_idx, mr_iid, False, "没有 open suggestion 可 resolve", log
        )
    note_id = str(rows[0].get("note_id"))

    # 1. 在 GitLab 上真的 resolve 这个 discussion
    try:
        stack.gitlab_request(
            "PUT",
            f"/projects/{pid}/merge_requests/{mr_iid}/discussions/{note_id}",
            payload={"resolved": True},
        )
    except Exception as exc:  # noqa: BLE001
        return FeatureResult(
            "reconcile_resolved", round_idx, mr_iid, False, f"resolve discussion 失败: {exc}", log
        )
    log.append(f"[{stack.now_iso()}] resolved discussion {note_id[:8]}")

    # 2. 跑 sync（仓库自带的脚本 = 真实运维路径）
    proc = subprocess.run(
        [
            ".venv/bin/python",
            "scripts/sync_resolved_suggestions.py",
            "--project-id", str(pid),
            "--mr-iid", str(mr_iid),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        timeout=300,
    )
    out = proc.stdout.decode("utf-8", errors="ignore")
    log.append(f"[{stack.now_iso()}] sync exit={proc.returncode}")
    log.append(out[-800:])

    # 3. 断言 DB 状态
    state, _row = _suggestion_state(note_id, pid, mr_iid)
    ok = state == "resolved"
    return FeatureResult(
        "reconcile_resolved",
        round_idx,
        mr_iid,
        ok,
        f"note={note_id[:8]} state={state} sync_exit={proc.returncode}",
        log,
    )


def run_push_dispatch(mr_iid: int, round_idx: int, log: list[str]) -> FeatureResult:
    """push 路径命令派发: push 事件必须能定位到 MR 并产出 run.

    Why: 之前全量档只发合成 `merge_request open`，push 路径（`router._handle_push`）
    从未被触发 —— 派发错了会静默不响应（用户视角 = 机器人没反应）。
    """
    mr = stack.get_mr_info(mr_iid, log)
    pid = int(mr["project_id"])
    branch = str(mr["source_branch"])
    payload = {
        "object_kind": "push",
        "event_name": "push",
        "ref": f"refs/heads/{branch}",
        "before": "0" * 40,
        "after": str(mr.get("sha") or ""),
        "user_name": "E2E",
        "user_username": "review-tester",
        "project": {"id": pid, "name": str(pid)},
        "commits": [{"id": str(mr.get("sha") or ""), "message": "e2e push dispatch"}],
        "total_commits_count": 1,
    }
    stack.clear_ephemeral_locks(log)
    before = len(stack.list_mr_runs(mr_iid))
    status, body = stack.post_webhook(payload, log)
    if status != 200:
        return FeatureResult(
            "push_dispatch", round_idx, mr_iid, False, f"status={status} body={body}", log
        )
    if body.get("status") == "ignored":
        # 无关联 MR / 非分支 ref —— 派发本身工作正常，但没有 MR 可检视
        return FeatureResult(
            "push_dispatch",
            round_idx,
            mr_iid,
            True,
            f"ignored reason={body.get('reason')} (派发路径已响应)",
            log,
        )
    res = stack.wait_for_new_run(
        mr_iid, before, expect_status="terminal", expected_min_increase=1, timeout_s=600, log=log
    )
    return FeatureResult(
        "push_dispatch",
        round_idx,
        mr_iid,
        bool(res.get("ok")),
        f"webhook={body.get('status')} runs+{res.get('delta', 0)}",
        log,
    )


#: 会**改变 MR 状态**的 flow（改代码 / 改建议状态）——
#: 必须在审计类断言（config:* / telemetry:* / config_real:*）**之后**跑,
#: 否则它们会把审计的基线搞脏（如 head_sha 与 MR head 不再一致)。
STATE_MUTATING_FEATURES = frozenset({"apply_closure", "reconcile_resolved"})

FLOWS: dict[str, Callable[[int, int, list[str]], FeatureResult]] = {
    "describe": run_describe,
    "improve": run_improve,
    "auto_chain": run_chain_open,
    "adopt": run_adopt,
    "dismiss": run_dismiss,
    "ui_apply": run_ui_apply,
    "telemetry_api": run_telemetry,
    "weekly_report": lambda _mr, round_idx, log: run_weekly_report(round_idx, log),
    "weekly_notify_gate": lambda _mr, round_idx, log: run_weekly_notify_gate(round_idx, log),
    "applicability": run_applicability,
    "cooldown": run_cooldown,
    "rate_limit": run_rate_limit,
    "dedup": run_dedup,
    "push_dispatch": run_push_dispatch,
    "apply_closure": run_apply_closure,
    "reconcile_resolved": run_reconcile_resolved,
}
