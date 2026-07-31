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

    # 2. MR 状态：仅 opened
    if mr.state and mr.state not in ("opened",):
        logger.info("webhook.skip state={} project={} mr={}", mr.state, mr.project_id, mr.mr_iid)
        return {"status": "skipped", "reason": f"state={mr.state}"}

    # 3. 处理不同 action
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
        commands = config.pr_commands if object_kind == "merge_request" else config.push_commands
    else:
        logger.info("webhook.skip action={} project={} mr={}", mr.action, mr.project_id, mr.mr_iid)
        return {"status": "skipped", "reason": f"action={mr.action}"}

    # 4. cooldown
    first_cmd = commands[0]
    if locks.should_skip_cooldown(mr.project_id, mr.mr_iid, first_cmd):
        logger.info("webhook.skip cooldown project={} mr={} cmd={}", mr.project_id, mr.mr_iid, first_cmd)
        return {"status": "skipped", "reason": "cooldown"}

    # 5. 入队命令链
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