"""Webhook 路由 — 立即 200 + 命令链入队.

核心原则:
    - webhook handler 必须立即返回 200，避免 GitLab 重试
    - MR Hook → 入队命令链 (pr_commands: describe → improve)
    - Push Hook → 入队命令链 (push_commands: describe → improve)
    - Note Hook → 单命令入队
    - 死循环防护 + cooldown + bot 白名单 + MR 状态检查
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from reviewagent.logging_setup import logger
from reviewagent.webhook.auth import verify_webhook_token
from reviewagent.webhook.locks import locks
from reviewagent.webhook.parsers import (
    MRHookPayload,
    NoteHookPayload,
    extract_command,
)

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@router.post("/webhook", dependencies=[Depends(verify_webhook_token)])
async def webhook(request: Request) -> dict:
    payload = await request.json()
    object_kind = payload.get("object_kind", "")

    from reviewagent.workers.tasks import enqueue_mr_chain, enqueue_command_from_note

    logger.info("webhook.received object_kind={} event_type={}", object_kind, payload.get("event_type", ""))

    if object_kind in ("merge_request", "push"):
        return await _handle_code_change(payload, object_kind, enqueue_mr_chain)
    if object_kind == "note":
        return await _handle_note_hook(payload, enqueue_command_from_note)

    logger.info("webhook.ignored object_kind={}", object_kind)
    return {"status": "ignored", "object_kind": object_kind}


# ---------- MR / Push Hook ----------

def _upsert_mr_from_hook(payload: dict, mr: "MRHookPayload") -> None:
    """从 webhook payload 构造 MRRecord 并 upsert 到 SQLite.

    确保 merge/close 事件的 state 变更被持久化, 前端能看到最新状态.
    """
    from reviewagent.telemetry.models import MRRecord, _parse_dt
    from reviewagent.telemetry.store import get_store

    obj = payload.get("object_attributes", {})
    author = payload.get("user", {})
    name = (author.get("name") or "").strip()
    username = (author.get("username") or "").strip()
    author_display = f"{name}@{username}" if name else username

    record = MRRecord(
        project_id=mr.project_id,
        mr_iid=mr.mr_iid,
        title=mr.title,
        author_username=author_display,
        source_branch=mr.source_branch,
        target_branch=mr.target_branch,
        state=mr.state,
        created_at=_parse_dt(obj.get("created_at")),
        updated_at=_parse_dt(obj.get("updated_at")),
        merged_at=_parse_dt(obj.get("merged_at")),
    )
    store = get_store()
    store.upsert_mr(record)
    logger.info("webhook.mr_upsert project={} mr={} state={}", mr.project_id, mr.mr_iid, mr.state)


async def _handle_code_change(payload: dict, object_kind: str, enqueue_mr_chain) -> dict:
    from reviewagent.config import config

    if object_kind == "merge_request":
        mr = MRHookPayload.from_payload(payload)
    else:
        # Push hook: 从 GitLab 查该 branch 的 open MR
        return await _handle_push(payload, enqueue_mr_chain)

    # 1. bot 自我识别
    if locks.is_bot(mr.actor_username):
        logger.info("webhook.skip bot_self project={} mr={}", mr.project_id, mr.mr_iid)
        return {"status": "skipped", "reason": "bot_self_trigger"}

    # 2. 始终 upsert MR 元信息 (state/merged_at 都更新) — 确保 merge/close 事件被记录
    try:
        _upsert_mr_from_hook(payload, mr)
    except Exception as e:  # noqa: BLE001
        logger.warning("webhook.mr_upsert failed (non-fatal) project={} mr={}: {}", mr.project_id, mr.mr_iid, e)

    # 3. MR 状态：仅 opened 触发 review chain
    if mr.state and mr.state not in ("opened",):
        logger.info("webhook.skip state={} project={} mr={}", mr.state, mr.project_id, mr.mr_iid)
        return {"status": "skipped", "reason": f"state={mr.state}", "note": "state_updated"}

    # 4. 处理不同 action
    if mr.action == "update":
        # update 事件：仅当有新 commit 时才触发 push_commands
        if not locks.check_diff_head_changed(mr.project_id, mr.mr_iid, mr.head_sha):
            logger.info("webhook.skip action=update (no new commit) project={} mr={} head_sha={!r}",
                        mr.project_id, mr.mr_iid, mr.head_sha)
            return {"status": "skipped", "reason": "action=update no new commit"}
        logger.info("webhook.action=update new commit project={} mr={} head_sha={}",
                    mr.project_id, mr.mr_iid, mr.head_sha)
        commands = config.push_commands
    elif mr.action in ("open", "reopen"):
        # open / reopen 也走一次 diff_head check: 首次见 SHA → 写锁 + 触发; 重放同 SHA → 跳过
        # Why: 之前 open 路径不写 diff_head, 后续 update 重投递会看到 Redis 是空 → 误判为新 commit
        if not locks.check_diff_head_changed(mr.project_id, mr.mr_iid, mr.head_sha):
            logger.info(
                "webhook.skip action={} (no new commit) project={} mr={} head_sha={!r}",
                mr.action, mr.project_id, mr.mr_iid, mr.head_sha,
            )
            return {"status": "skipped", "reason": f"action={mr.action} same sha"}
        commands = config.pr_commands if object_kind == "merge_request" else config.push_commands
    else:
        logger.info("webhook.skip action={} project={} mr={}", mr.action, mr.project_id, mr.mr_iid)
        return {"status": "skipped", "reason": f"action={mr.action}"}

    # 5. head_sha 变化时 (UI apply / push) — 先 auto-detect 已应用建议
    # Why: 用户在 GitLab UI 点 Apply suggestion 后, 代码会变但不会触发
    #      note 事件, 之前的 /adopt 处理就跑了. 这里在 head_sha 变时
    #      主动探测所有 open suggestions, 把已被 UI apply 的转 state=applied.
    # 重要: 必须在 cooldown check 之前, 否则连续 2 次 head_sha 变化
    #       (21:32 MR update + 21:32:51 再次 MR update) 第二次会被 cooldown 跳过,
    #       永远不跑 auto_detect_applied → telemetry 看不到 applied 状态.
    if mr.action == "update":
        try:
            from reviewagent.commands.suggestion_actions import auto_detect_applied
            ad_result = auto_detect_applied(
                project_id=mr.project_id,
                mr_iid=mr.mr_iid,
                head_sha=mr.head_sha,
                actor_username=mr.actor_username or "auto-detect",
            )
            if ad_result.get("applied"):
                logger.info(
                    "webhook.auto_detect_applied project={} mr={} applied={}",
                    mr.project_id, mr.mr_iid, ad_result["applied"],
                )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "webhook.auto_detect_applied failed (non-fatal) project={} mr={}: {}",
                mr.project_id, mr.mr_iid, e,
            )

    # 6a. max review calls — 防无限循环
    skip_max, current_count = locks.should_skip_max_review_calls(
        mr.project_id, mr.mr_iid, commands,
    )
    if skip_max:
        from reviewagent.config import config as _config
        logger.info(
            "webhook.skip max_review_calls project={} mr={} count={} limit={}",
            mr.project_id, mr.mr_iid, current_count, _config.max_review_calls_per_mr,
        )
        # 一次性发 "no more review" 提示 (用 telemetry 记 flag, 避免重复刷屏)
        try:
            from reviewagent.telemetry.store import get_store
            store = get_store()
            with store._conn() as conn:
                row = conn.execute(
                    "SELECT 1 FROM mr_activity "
                    "WHERE project_id=? AND mr_iid=? AND title LIKE '%no_more_review%' "
                    "LIMIT 1",
                    (mr.project_id, mr.mr_iid),
                ).fetchone()
                already_posted = row is not None
            if not already_posted:
                msg = (
                    f"ℹ️ 本 MR 已达最大检视次数 ({current_count} 次, 上限 "
                    f"{_config.max_review_calls_per_mr}); 后续 push 不再触发自动检视. "
                    f"如需新一轮检视, 评论 `/improve` 手动触发."
                )
                try:
                    mr.gitlab if hasattr(mr, "gitlab") else None
                except Exception:
                    pass
                # 用直接的 gitlab client (mr 对象没暴露 .gitlab)
                from reviewagent.gitlab.client import GitLabClient
                _gl = GitLabClient()
                _gl.post_mr_comment(mr.project_id, mr.mr_iid, msg)
                # 在 mr_activity.title 末尾加个标记 (cosmetic)
                try:
                    with store._conn() as conn:
                        conn.execute(
                            "UPDATE mr_activity SET title = title || ' [no_more_review]' "
                            "WHERE project_id=? AND mr_iid=?",
                            (mr.project_id, mr.mr_iid),
                        )
                        conn.commit()
                except Exception:
                    pass
                logger.info(
                    "webhook.no_more_review_notice posted project={} mr={}",
                    mr.project_id, mr.mr_iid,
                )
        except Exception as e:
            logger.warning("webhook.no_more_review_notice failed (non-fatal): {}", e)
        return {"status": "skipped", "reason": f"max_review_calls={current_count}"}

    # 6b. cooldown
    first_cmd = commands[0]
    if locks.should_skip_cooldown(mr.project_id, mr.mr_iid, first_cmd):
        logger.info("webhook.skip cooldown project={} mr={} cmd={}", mr.project_id, mr.mr_iid, first_cmd)
        return {"status": "skipped", "reason": "cooldown"}

    # 7. 入队命令链
    job_ids = enqueue_mr_chain(
        commands=commands,
        project_id=mr.project_id,
        mr_iid=mr.mr_iid,
        triggered_by="webhook",
        actor_username=mr.actor_username,
    )
    logger.info("webhook.queued commands={} project={} mr={} jobs={}", list(commands), mr.project_id, mr.mr_iid, job_ids)
    return {"status": "queued", "commands": list(commands), "job_ids": job_ids}


# ---------- Push Hook ----------

async def _handle_push(payload: dict, enqueue_mr_chain) -> dict:
    """Push Hook: 从 branch 查找关联的 open MR，入队 push_commands."""
    from reviewagent.config import config
    from reviewagent.gitlab.client import GitLabError, client as gl

    ref = payload.get("ref", "")
    if not ref.startswith("refs/heads/"):
        logger.info("webhook.ignored push ref={}", ref)
        return {"status": "ignored", "reason": f"non-branch ref={ref}"}
    branch = ref.removeprefix("refs/heads/")
    project_id = (payload.get("project") or {}).get("id", 0)
    if not project_id:
        return {"status": "ignored", "reason": "no_project_id"}

    # 查该 branch 关联的 MR (opened + merged)
    # merged MR 的 branch 仍可能被继续 push (squash-merge + 继续开发), 也要触发一次 re-review
    mrs = []
    for st in ("opened", "merged"):
        try:
            chunk = gl.list_project_mrs(project_id, state=st, source_branch=branch)
        except GitLabError as e:
            logger.warning("push.lookup_mr state={} failed: {}", st, e)
            return {"status": "error", "reason": str(e)[:200]}
        # 加 state 标记, 后续逻辑可区分 (merged MR 的 re-review 不强制 describe)
        for m in chunk:
            m.setdefault("_state", st)
            mrs.append(m)
    if not mrs:
        logger.info("push.no_mr project={} branch={}", project_id, branch)
        return {"status": "ignored", "reason": "no_mr_for_branch"}

    # 对每个匹配的 MR 入队 push_commands 链
    results = []
    for mr in mrs:
        mr_iid = mr.get("iid")
        actor = (payload.get("user_username") or payload.get("user", {}).get("username", ""))
        if locks.is_bot(actor):
            continue
        # 格式化为 "名字@工号"
        actor_name = (payload.get("user_name") or "").strip()
        actor = f"{actor_name}@{actor}" if actor_name and actor else actor
        if locks.should_skip_cooldown(project_id, mr_iid, config.push_commands[0]):
            continue
        # push event 也可能隐含 user 改了代码 (手动 apply / 直接改源) —
        # 探测哪些 open suggestion 已被实际改写, 转 state=applied
        try:
            from reviewagent.commands.suggestion_actions import auto_detect_applied
            head_sha_push = mr.get("sha") or ""
            if head_sha_push:
                ad_result = auto_detect_applied(
                    project_id=project_id,
                    mr_iid=mr_iid,
                    head_sha=head_sha_push,
                    actor_username=actor or "auto-detect",
                )
                if ad_result.get("applied"):
                    logger.info(
                        "push.auto_detect_applied project={} mr={} applied={}",
                        project_id, mr_iid, ad_result["applied"],
                    )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "push.auto_detect_applied failed (non-fatal) project={} mr={}: {}",
                project_id, mr_iid, e,
            )
        job_ids = enqueue_mr_chain(
            commands=config.push_commands,
            project_id=project_id,
            mr_iid=mr_iid,
            triggered_by="push",
            actor_username=actor,
        )
        results.append({"mr_iid": mr_iid, "job_ids": job_ids})
        logger.info("push.queued commands={} project={} mr={} branch={} jobs={}",
                    list(config.push_commands), project_id, mr_iid, branch, job_ids)
    return {"status": "queued" if results else "skipped", "mrs": results}


# ---------- Note Hook ----------

async def _handle_note_hook(payload: dict, enqueue_command_from_note) -> dict:
    note = NoteHookPayload.from_payload(payload)
    if note is None:
        return {"status": "ignored", "reason": "not_mr_note"}

    if locks.is_bot(note.actor_username):
        return {"status": "skipped", "reason": "bot_self_trigger"}

    # GitLab system DiffNote: 用户在 UI 点击 "Apply suggestion" 时, GitLab 会发一条
    # type=DiffNote / system=true / body="changed this line in version N of the diff"
    # 的系统提示 (note 4037/4039/4041 风格). 这里直接匹配 DB 里同 file:line 的
    # suggestion 并标记为 applied, 不需要 LLM, 也不需要再走一次 /adopt 验证.
    if note.is_system and note.note_type == "DiffNote" and note.diff_file and note.diff_line > 0:
        # 仅匹配 "changed this line" (apply suggestion) / "added line" (新行) 这类行级 diff 提示
        if "changed this line" in (note.note_body or "") or "added line" in (note.note_body or ""):
            from reviewagent.commands.suggestion_actions import mark_suggestion_applied_by_diff
            marked = mark_suggestion_applied_by_diff(
                project_id=note.project_id,
                mr_iid=note.mr_iid,
                file_path=note.diff_file,
                target_line=note.diff_line,
                actor_username=note.actor_username,
                source_note_id=note.note_id,
            )
            if marked:
                logger.info(
                    "webhook.system_applied project={} mr={} file={} line={} note_id={}",
                    note.project_id, note.mr_iid, note.diff_file, note.diff_line, note.note_id,
                )
                return {"status": "applied", "via": "gitlab_ui", "suggestion_id": marked}
            # 没有匹配到 open suggestion (可能已被 /adopt/dismiss 处理) → 静默跳过,
            # 不需要让 push_commands 再跑一次
            return {"status": "ignored", "reason": "no_open_suggestion_at_line"}

    # /adopt /dismiss 走专门路径 (不是 MR 命令链, 而是针对 inline suggestion 的回复)
    from reviewagent.commands.suggestion_actions import extract_action
    action_info = extract_action(note.note_body)
    if action_info:
        action, reason = action_info
        # /adopt /dismiss 必须是对 inline suggestion 的回复 (DiffNote + 有 discussion_id)
        if note.note_type != "DiffNote" or not note.discussion_id:
            return {"status": "ignored", "reason": f"{action}_requires_diffnote"}
        if locks.should_skip_cooldown(note.project_id, note.mr_iid, action):
            return {"status": "skipped", "reason": "cooldown"}
        # 入队
        from reviewagent.workers.tasks import enqueue_suggestion_action
        job_id = enqueue_suggestion_action(
            action=action,
            project_id=note.project_id,
            mr_iid=note.mr_iid,
            suggestion_note_id=note.discussion_id,
            actor_username=note.actor_username,
            reason=reason,
        )
        logger.info(
            "webhook.queued action={} project={} mr={} discussion={} job={}",
            action, note.project_id, note.mr_iid, note.discussion_id, job_id,
        )
        return {"status": "queued", "action": action, "job_id": job_id}

    cmd = extract_command(note.note_body)
    if not cmd:
        return {"status": "ignored", "reason": "no_command"}

    if locks.should_skip_cooldown(note.project_id, note.mr_iid, cmd):
        return {"status": "skipped", "reason": "cooldown"}

    job_id = enqueue_command_from_note(
        command=cmd,
        project_id=note.project_id,
        mr_iid=note.mr_iid,
        triggered_by="note",
        actor_username=note.actor_username,
    )
    logger.info("webhook.queued note_cmd={} project={} mr={} job={}", cmd, note.project_id, note.mr_iid, job_id)
    return {"status": "queued", "command": cmd, "job_id": job_id}