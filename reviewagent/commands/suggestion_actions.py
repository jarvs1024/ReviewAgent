"""/adopt 和 /dismiss 命令 — 处理 reviewer 对 inline suggestion 的回复.

背景:
    reviewer 收到 improve 发出的 inline suggestion 后, 可以:
    1. 直接点 "Apply suggestion" 按钮 (GitLab UI 处理)
    2. 自己改代码然后回复 `/adopt [理由]` (手动采纳)
    3. 不采纳, 回复 `/dismiss [理由]` (关闭建议)

本模块处理 (2) 和 (3).

设计要点 (来自 pr-agent pr_agent/servers/gitlab_webhook.py):
    - /dismiss: 解析 user 原因, resolve discussion, 记录 telemetry
    - /adopt: 验证 user 真的改了目标行 (比较 posted_sha 和 current_sha 下的 file content),
             通过后 resolve discussion, 记录 telemetry
             验证失败: 在 discussion 下回复友好提示
"""
from __future__ import annotations

import re
from typing import Any

from reviewagent.gitlab.client import GitLabError, GitLabClient
from reviewagent.logging_setup import logger
from reviewagent.telemetry.store import get_store


# ---------- 解析 ----------
_DISMISS_RE = re.compile(r"(?<![A-Za-z0-9])dismiss(?![A-Za-z0-9])", re.IGNORECASE)
_ADOPT_RE = re.compile(r"(?<![A-Za-z0-9])adopt(?![A-Za-z0-9])", re.IGNORECASE)
_WRAPPER_STRIP = re.compile(
    r"^[\s/\\?\"'\u2018\u2019\u201c\u201d,;:。,;:：!\-—_()]+"
    r"|"
    r"[\s/\\?\"'\u2018\u2019\u201c\u201d,;:。,;:：!\-—_()]+$"
)


def extract_action(body: str) -> tuple[str, str] | None:
    """从 note body 提取 action (adopt/dismiss) 和原因.

    Returns: (\"adopt\"|\"dismiss\", reason) 或 None
    """
    if not body:
        return None
    # adopt 优先于 dismiss (允许 "/adopt 用 dismiss 风格重写" 这种情形)
    m = _ADOPT_RE.search(body)
    if m:
        word = m.group(0)
        before, _sep, after = body.partition(word)
        reason = _WRAPPER_STRIP.sub("", (before + after)).strip()
        return ("adopt", reason)
    m = _DISMISS_RE.search(body)
    if m:
        word = m.group(0)
        before, _sep, after = body.partition(word)
        reason = _WRAPPER_STRIP.sub("", (before + after)).strip()
        return ("dismiss", reason)
    return None



def _target_region_changed(
    posted_content: str,
    current_content: str,
    *,
    line: int,
    line_end: int,
    context_lines: int = 2,
) -> bool:
    """判断目标行区段是否被修改 (用 LCS 对齐窗口, 再对比目标区段本身).

    Returns: True 表示目标行被修改.
    """
    if not posted_content or not current_content:
        return False
    posted_lines = posted_content.splitlines()
    current_lines = current_content.splitlines()

    lo = max(0, line - 1 - context_lines)
    hi = min(len(posted_lines), line_end + context_lines)
    if lo >= hi or line - 1 >= len(posted_lines):
        return False
    posted_window = posted_lines[lo:hi]
    target_lines_posted = posted_lines[max(0, line - 1): min(len(posted_lines), line_end)]

    def _lcs_len(a, b):
        if len(a) > len(b):
            a, b = b, a
        prev = [0] * (len(a) + 1)
        for i in range(1, len(b) + 1):
            cur = [0] * (len(a) + 1)
            for j in range(1, len(a) + 1):
                if a[j-1] == b[i-1]:
                    cur[j] = prev[j-1] + 1
                else:
                    cur[j] = max(prev[j], cur[j-1])
            prev = cur
        return prev[-1]

    best_lcs = 0
    best_offset = 0
    w = len(posted_window)
    for start in range(0, max(1, len(current_lines) - w + 1)):
        lcs = _lcs_len(posted_window, current_lines[start:start + w])
        if lcs > best_lcs:
            best_lcs = lcs
            best_offset = start

    target_start_in_current = best_offset + (line - 1 - lo)
    target_end_in_current = target_start_in_current + len(target_lines_posted)
    target_end_in_current = min(target_end_in_current, len(current_lines))
    if target_start_in_current >= len(current_lines):
        return True
    target_lines_current = current_lines[target_start_in_current:target_end_in_current]

    def _norm(lines):
        return [l.strip() for l in lines]

    return _norm(target_lines_posted) != _norm(target_lines_current)


# ---------- handlers ----------
def process_dismiss(
    *,
    project_id: int,
    mr_iid: int,
    suggestion_note_id: str,
    actor_username: str,
    reason: str,
) -> dict[str, Any]:
    """处理 /dismiss 命令.

    Returns: {\"action\": \"dismissed\"|\"dismiss-skipped\"|\"dismiss-failed\", \"reason\": str}
    """
    gl = GitLabClient()
    store = get_store()

    # 1. 检查 suggestion 当前状态 (避免 clobber 已 terminal 的状态)
    sug = store.get_suggestion_by_note_id(suggestion_note_id)
    if sug is not None and sug.get("state") not in ("open",):
        logger.info(
            "/dismiss skipped state={} note_id={}",
            sug.get("state"), suggestion_note_id,
        )
        return {"action": "dismiss-skipped", "reason": f"state={sug.get('state')}"}

    # 2. Resolve discussion
    ok = gl.resolve_discussion(project_id, mr_iid, suggestion_note_id)
    if not ok:
        logger.warning(
            "/dismiss resolve failed project={} mr={} discussion={}",
            project_id, mr_iid, suggestion_note_id,
        )
        return {"action": "dismiss-failed", "reason": "resolve_failed"}

    # 3. 更新 suggestion state (dismissed + 写入 reason)
    store.update_suggestion_state(
        suggestion_note_id, "dismissed",
        actor_username=actor_username, dismissed_reason=reason or None,
    )

    # 4. 记录 telemetry
    store.record_suggestion_action(
        project_id=project_id,
        mr_iid=mr_iid,
        suggestion_note_id=suggestion_note_id,
        file_path=(sug or {}).get("file_path"),
        target_line=(sug or {}).get("target_line"),
        action="dismissed",
        actor_username=actor_username,
        reason=reason,
        validation_status="ok",
    )

    logger.info(
        "/dismiss resolved project={} mr={} discussion={} reason={!r}",
        project_id, mr_iid, suggestion_note_id, reason[:50] if reason else "",
    )

    # 5. 回复确认 (让 reviewer 看到反馈)
    if reason:
        reply = f"✅ 已关闭建议，原因：{reason}\n\n_理由已记录，用于改进后续建议。_"
    else:
        reply = "✅ 已关闭建议。\n\n_理由已记录，用于改进后续建议。_"
    gl.reply_to_discussion(project_id, mr_iid, suggestion_note_id, reply)

    return {"action": "dismissed", "reason": reason}


def process_adopt(
    *,
    project_id: int,
    mr_iid: int,
    suggestion_note_id: str,
    actor_username: str,
    reason: str,
) -> dict[str, Any]:
    """处理 /adopt 命令.

    Returns: {\"action\": \"adopted\"|\"adopt-validation-failed\"|\"adopt-failed\", \"reason\": str, \"validation\": str}
    """
    gl = GitLabClient()
    store = get_store()

    # 1. 找 suggestion 记录
    sug = store.get_suggestion_by_note_id(suggestion_note_id)
    if sug is None:
        # 没找到 = improve 没记录过这条 suggestion (可能是历史的)
        # 仍然尝试 resolve (让用户至少能看到反馈)
        logger.info(
            "/adopt: no suggestion record for note_id={}, allowing resolve anyway",
            suggestion_note_id,
        )
        gl.resolve_discussion(project_id, mr_iid, suggestion_note_id)
        gl.reply_to_discussion(
            project_id, mr_iid, suggestion_note_id,
            "✅ 已采纳建议 (无历史记录, 跳过验证)。",
        )
        return {"action": "adopted-unchecked", "reason": "no_record"}

    # 2. 检查 suggestion 状态
    if sug.get("state") not in ("open",):
        logger.info(
            "/adopt skipped state={} note_id={}",
            sug.get("state"), suggestion_note_id,
        )
        return {"action": "adopt-skipped", "reason": f"state={sug.get('state')}"}

    # 3. 验证目标行是否被修改
    head_sha_posted = sug.get("head_sha") or ""
    file_path = sug.get("file_path") or ""
    target_line = int(sug.get("target_line") or 0)
    target_line_end = int(sug.get("target_line_end") or target_line)

    if not (head_sha_posted and file_path and target_line):
        logger.warning(
            "/adopt missing metadata file={} line={} head_sha={}",
            file_path, target_line, head_sha_posted[:8],
        )
        return {"action": "adopt-failed", "reason": "metadata_incomplete"}

    # 取当前 MR 的 head_sha
    try:
        refs = gl.get_mr_diff_refs(project_id, mr_iid)
        head_sha_current = refs.get("head_sha") or ""
    except GitLabError as e:
        logger.warning("/adopt get_mr_diff_refs failed: {}", e)
        return {"action": "adopt-failed", "reason": "refs_unavailable"}

    if not head_sha_current:
        return {"action": "adopt-failed", "reason": "refs_empty"}

    if head_sha_current == head_sha_posted:
        # 没有新 commit → 用户还没改代码, 不算采纳
        reply = (
            "未检测到这条建议对应位置的代码修改，暂不能标记为手工采纳。"
            "请先提交修改，再回复 `/adopt [说明]`。"
        )
        gl.reply_to_discussion(project_id, mr_iid, suggestion_note_id, reply)
        store.record_suggestion_action(
            project_id=project_id,
            mr_iid=mr_iid,
            suggestion_note_id=suggestion_note_id,
            file_path=file_path,
            target_line=target_line,
            action="adopted",
            actor_username=actor_username,
            reason=reason,
            validation_status="same-head",
            head_sha_posted=head_sha_posted,
            head_sha_current=head_sha_current,
        )
        return {"action": "adopt-validation-failed", "reason": "same_head",
                "validation": "same-head"}

    # 4. 取两个 SHA 的文件内容, 比较目标行
    posted_content = gl.get_file_at_sha(project_id, file_path, head_sha_posted)
    current_content = gl.get_file_at_sha(project_id, file_path, head_sha_current)

    if not posted_content or not current_content:
        reply = (
            "暂时无法验证这条建议对应位置的代码修改 (文件读取失败)，"
            "请稍后重试 `/adopt`。"
        )
        gl.reply_to_discussion(project_id, mr_iid, suggestion_note_id, reply)
        store.record_suggestion_action(
            project_id=project_id,
            mr_iid=mr_iid,
            suggestion_note_id=suggestion_note_id,
            file_path=file_path,
            target_line=target_line,
            action="adopted",
            actor_username=actor_username,
            reason=reason,
            validation_status="content-unavailable",
            head_sha_posted=head_sha_posted,
            head_sha_current=head_sha_current,
        )
        return {"action": "adopt-validation-failed", "reason": "content_unavailable",
                "validation": "content-unavailable"}

    changed = _target_region_changed(
        posted_content, current_content,
        line=target_line, line_end=target_line_end,
        context_lines=2,
    )

    if not changed:
        reply = (
            f"未检测到这条建议对应位置（`{file_path}:{target_line}-{target_line_end}`）"
            "的代码修改，暂不能标记为手工采纳。"
            "请先在该位置提交修改，再回复 `/adopt [说明]`。"
        )
        gl.reply_to_discussion(project_id, mr_iid, suggestion_note_id, reply)
        store.record_suggestion_action(
            project_id=project_id,
            mr_iid=mr_iid,
            suggestion_note_id=suggestion_note_id,
            file_path=file_path,
            target_line=target_line,
            action="adopted",
            actor_username=actor_username,
            reason=reason,
            validation_status="target-unchanged",
            head_sha_posted=head_sha_posted,
            head_sha_current=head_sha_current,
        )
        return {"action": "adopt-validation-failed", "reason": "target_unchanged",
                "validation": "target-unchanged"}

    # 5. 通过验证 → resolve + 记录
    ok = gl.resolve_discussion(project_id, mr_iid, suggestion_note_id)
    if not ok:
        return {"action": "adopt-failed", "reason": "resolve_failed"}

    store.update_suggestion_state(
        suggestion_note_id, "applied", actor_username=actor_username
    )
    store.record_suggestion_action(
        project_id=project_id,
        mr_iid=mr_iid,
        suggestion_note_id=suggestion_note_id,
        file_path=file_path,
        target_line=target_line,
        action="adopted",
        actor_username=actor_username,
        reason=reason,
        validation_status="ok",
        head_sha_posted=head_sha_posted,
        head_sha_current=head_sha_current,
    )

    if reason:
        reply = f"✅ 已采纳建议 (检测到目标行有修改)，原因：{reason}\n\n_理由已记录，用于改进后续建议。_"
    else:
        reply = "✅ 已采纳建议 (检测到目标行有修改)。\n\n_理由已记录，用于改进后续建议。_"
    gl.reply_to_discussion(project_id, mr_iid, suggestion_note_id, reply)

    logger.info(
        "/adopt resolved project={} mr={} discussion={} reason={!r}",
        project_id, mr_iid, suggestion_note_id, reason[:50] if reason else "",
    )

    reimprove_job = _maybe_enqueue_reimprove(
        project_id=project_id, mr_iid=mr_iid, actor_username=actor_username,
    )
    if reimprove_job:
        return {"action": "adopted", "reason": reason, "validation": "ok", "reimprove_job": reimprove_job}
    return {"action": "adopted", "reason": reason, "validation": "ok"}


# ---------- /adopt 自动重检 ----------
# 在 /adopt 通过验证后, 用同样的 MR head 触发一次 /improve,
# 这样 reviewer 采纳一条建议后能立刻看到 diff 的最新状态, 不必手动 @reviewagent。
# cooldown 由 `locks.should_skip_cooldown` 控制: 用户连续 /adopt 不会无限循环。
def _maybe_enqueue_reimprove(
    *, project_id: int, mr_iid: int, actor_username: str,
) -> str | None:
    """成功 /adopt 后入队一次 re-improve。返回 job_id 或 None (cooldown 内跳过 / 入队失败)."""
    try:
        from reviewagent.workers.tasks import enqueue_improve
        from reviewagent.webhook.locks import locks
    except Exception as e:  # pragma: no cover
        logger.warning("/adopt.reimprove import failed (non-fatal): {}", e)
        return None
    try:
        if locks.should_skip_cooldown(project_id, mr_iid, "improve"):
            logger.info(
                "/adopt.reimprove skip cooldown project={} mr={}",
                project_id, mr_iid,
            )
            return None
    except Exception as e:
        logger.warning("/adopt.reimprove cooldown check failed (fail-open): {}", e)
    try:
        job_id = enqueue_improve(
            project_id=project_id,
            mr_iid=mr_iid,
            triggered_by="adopt",
            actor_username=actor_username,
        )
        logger.info(
            "/adopt.reimprove queued project={} mr={} job={}",
            project_id, mr_iid, job_id,
        )
        return job_id
    except Exception as e:
        logger.warning("/adopt.reimprove enqueue failed (non-fatal): {}", e)
        return None
