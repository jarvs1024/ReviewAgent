"""Webhook 路由 — 立即 200 + 入队.

核心原则:
    - webhook handler 必须立即返回 200，避免 GitLab 重试
    - 实际工作通过 RQ 异步执行
    - 死循环防护 + cooldown + bot 白名单 + per-MR 锁
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
    """健康检查端点."""
    return {"status": "ok"}


@router.post("/webhook", dependencies=[Depends(verify_webhook_token)])
async def webhook(request: Request) -> dict:
    """GitLab webhook 入口.

    支持的事件:
        - Merge Request Hook: action=open → /describe; action=update → /describe
        - Note Hook: MR 评论含 /describe /review /improve → 入对应队列
    """
    payload = await request.json()
    object_kind = payload.get("object_kind", "")

    # 延迟导入 RQ tasks 避免循环依赖
    from reviewagent.workers.tasks import enqueue_describe, enqueue_command_from_note

    if object_kind == "merge_request":
        return await _handle_mr_hook(payload, enqueue_describe)
    if object_kind == "note":
        return await _handle_note_hook(payload, enqueue_command_from_note)

    logger.info("webhook.ignored object_kind={}", object_kind)
    return {"status": "ignored", "object_kind": object_kind}


async def _handle_mr_hook(payload: dict, enqueue_describe) -> dict:
    mr = MRHookPayload.from_payload(payload)

    # bot 自我识别（防回环）
    if locks.is_bot(mr.actor_username):
        logger.info("webhook.skip bot_self project={} mr={}", mr.project_id, mr.mr_iid)
        return {"status": "skipped", "reason": "bot_self_trigger"}

    # 仅 open / update 触发 /describe
    if mr.action not in ("open", "update", "reopen"):
        logger.info("webhook.skip action={} project={} mr={}",
                    mr.action, mr.project_id, mr.mr_iid)
        return {"status": "skipped", "reason": f"action={mr.action}"}

    # cooldown
    if locks.should_skip_cooldown(mr.project_id, mr.mr_iid, "describe"):
        logger.info("webhook.skip cooldown project={} mr={}", mr.project_id, mr.mr_iid)
        return {"status": "skipped", "reason": "cooldown"}

    # 入队
    job_id = enqueue_describe(
        project_id=mr.project_id,
        mr_iid=mr.mr_iid,
        triggered_by="webhook",
        actor_username=mr.actor_username,
    )
    logger.info("webhook.queued describe project={} mr={} job={}",
                mr.project_id, mr.mr_iid, job_id)
    return {"status": "queued", "command": "describe", "job_id": job_id}


async def _handle_note_hook(payload: dict, enqueue_command_from_note) -> dict:
    note = NoteHookPayload.from_payload(payload)
    if note is None:
        return {"status": "ignored", "reason": "not_mr_note"}

    # bot 自我识别
    if locks.is_bot(note.actor_username):
        return {"status": "skipped", "reason": "bot_self_trigger"}

    cmd = extract_command(note.note_body)
    if not cmd:
        return {"status": "ignored", "reason": "no_command"}

    # cooldown（per-MR per-command）
    if locks.should_skip_cooldown(note.project_id, note.mr_iid, cmd):
        return {"status": "skipped", "reason": "cooldown"}

    job_id = enqueue_command_from_note(
        command=cmd,
        project_id=note.project_id,
        mr_iid=note.mr_iid,
        triggered_by="note",
        actor_username=note.actor_username,
    )
    logger.info("webhook.queued note_cmd={} project={} mr={} job={}",
                cmd, note.project_id, note.mr_iid, job_id)
    return {"status": "queued", "command": cmd, "job_id": job_id}