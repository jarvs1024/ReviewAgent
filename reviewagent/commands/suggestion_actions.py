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
from reviewagent.commands._common import publish_overview
from reviewagent.logging_setup import logger
from reviewagent.telemetry.store import get_store

# ---------- 解析 ----------
# 命令关键字的边界: 前后不允许出现 ASCII 字母/数字/下划线 (下划线算 word char)
# - 中文 / 空格 / 标点 不在边界排除集里 → `/dismiss测试`, `/dismiss log`, `/dismiss 关闭这条` 都允许
# - `/dismissed`, `dismissal`, `/dismiss_xxx` 会被拒 (避免误匹配 dismiss 的子串)
_DISMISS_RE = re.compile(r"(?<![A-Za-z0-9_])dismiss(?![A-Za-z0-9_])", re.IGNORECASE)
_ADOPT_RE = re.compile(r"(?<![A-Za-z0-9_])adopt(?![A-Za-z0-9_])", re.IGNORECASE)
# 包裹字符: 开头/结尾的空白 / 斜杠 / 各种标点 / 全角符号 / 破折号 / 下划线 / 括号 都剥掉
_WRAPPER_STRIP = re.compile(
    r"""^[\s/\\?\"'\u2018\u2019\u201c\u201d,;:。,;:：!\-—_()【】《》「」『』]+"""
    r"""|"""
    r"""[\s/\\?\"'\u2018\u2019\u201c\u201d,;:。,;:：!\-—_()【】《》「」『』]+$"""
)
# 内部多空白 → 单空白; 兼容 tab / 全角空格 / 换行
_INTERNAL_WS = re.compile(r"[\s\u3000]+")
# 中间散落的 "/" 紧跟空白 (命令语法残留) → 去掉
_INTERNAL_SLASH = re.compile(r"/[\s\u3000]+|[\s\u3000]+/")


def _normalize_reason(text: str) -> str:
    """归一化 reason: 剥包裹 + 去掉中间散落的 "/" + 压多空白 + strip.

    处理链:
      - 剥掉开头/结尾的 空白/标点/全角符号 (见 _WRAPPER_STRIP)
      - 去掉中间紧贴空白的 "/" (命令语法残留, "some random /dismiss foo"
        → reason 不应含 "random / foo")
      - 压内部多空白为单空白 (tab / 全角空格 / 换行也兼容)
      - 最终 strip
    """
    if not text:
        return ""
    cleaned = _WRAPPER_STRIP.sub("", text)
    cleaned = _INTERNAL_SLASH.sub(" ", cleaned)
    cleaned = _INTERNAL_WS.sub(" ", cleaned)
    return cleaned.strip()


def extract_action(body: str) -> tuple[str, str] | None:
    """从 note body 提取 action (adopt/dismiss) 和原因.

    Returns: ("adopt"|"dismiss", reason) 或 None

    兼容:
      - `/dismiss` → ("dismiss", "")
      - `/dismiss log` → ("dismiss", "log")
      - `/dismiss测试` → ("dismiss", "测试")
      - `/dismiss   log   reason` → ("dismiss", "log reason")
      - `/dismiss - 关闭这条理由` → ("dismiss", "关闭这条理由")
      - `/Dismiss LOG` (大小写不敏感) → ("dismiss", "LOG")
      - `/dismissed` / `dismissal` 不匹配 (子串被 word boundary 排除)
    """
    if not body:
        return None
    # adopt 优先于 dismiss (允许 "/adopt 用 dismiss 风格重写" 这种情形)
    m = _ADOPT_RE.search(body)
    if m:
        word = m.group(0)
        before, _sep, after = body.partition(word)
        reason = _normalize_reason(before + after)
        return ("adopt", reason)
    m = _DISMISS_RE.search(body)
    if m:
        word = m.group(0)
        before, _sep, after = body.partition(word)
        reason = _normalize_reason(before + after)
        return ("dismiss", reason)
    return None



def _late_detect_single(
    *,
    sug: dict[str, Any],
    head_sha: str,
    project_id: int,
    mr_iid: int,
    actor_username: str,
) -> str:
    """对单条 state='resolved' + resolution_source='gitlab_resolve' 的 suggestion
    重跑一次 exact_match / region_changed / token_fallback, 命中就翻 applied.

    Returns:
        "applied"  — 已翻 applied
        "unchanged" — 代码未落地, 保持 resolved
        "error"    — 文件读取 / 比较失败

    Why 独立 helper:
        跟主循环共用 _exact_improved_code_near_target / _target_region_changed /
        _token_adoption_match 三层判定, 但不需要重写 gitlab.resolve_discussion
        (已经 resolved 了, GitLab 那边的 thread 已经是 ✓ 状态).
    """
    gl = GitLabClient()
    store = get_store()


def _find_latest_apply_commit(
    apply_commits,
    *,
    head_sha: str,
) -> str:
    """Batch4: 在 MR commit 列表里找最接近 head_sha 的 'Apply ...' commit.

    GitLab 用户点 Apply suggestion 后产生的 commit title 形如:
        "Apply 1 suggestion(s) to 1 file(s)"
    返回短 SHA (前 8 位) 写进 applied_commit_sha 便于审计.

    容错: apply_commits 不是 list 或元素不是 dict (测试 mock) → 返回空串.
    """
    if not apply_commits or not isinstance(apply_commits, list) or not head_sha:
        return ""
    head_short = head_sha[:8]
    target = ""
    for c in apply_commits:
        if not isinstance(c, dict):
            continue
        sid = (c.get("short_id") or "").strip()
        title = (c.get("title") or "").strip()
        if not sid or not title:
            continue
        if not title.startswith("Apply "):
            continue
        if head_short and head_short in (c.get("id") or ""):
            return sid
        if not target:
            target = sid
    return target


def _late_detect_single(
    *,
    sug: dict[str, Any],
    head_sha: str,
    project_id: int,
    mr_iid: int,
    actor_username: str,
) -> str:
    """对单条 state='resolved' + resolution_source='gitlab_resolve' 的 suggestion
    重跑一次 exact_match / region_changed / token_fallback, 命中就翻 applied.

    Returns:
        "applied"  — 已翻 applied
        "unchanged" — 代码未落地, 保持 resolved
        "error"    — 文件读取 / 比较失败
    """
    gl = GitLabClient()
    store = get_store()
    note_id = sug.get("note_id") or ""
    file_path = sug.get("file_path") or ""
    target_line = int(sug.get("target_line") or 0)
    target_line_end = int(sug.get("target_line_end") or target_line)
    existing_code = sug.get("existing_code") or ""
    if not (note_id and file_path and target_line):
        return "unchanged"

    current_content = gl.get_file_at_sha(project_id, file_path, head_sha)
    if current_content is None:
        logger.info(
            "auto_detect_applied late_detect skip (no file) project={} mr={} note={} file={}",
            project_id, mr_iid, note_id[:8], file_path,
        )
        return "error"

    posted_content = gl.get_file_at_sha(
        project_id, file_path, sug.get("head_sha") or head_sha,
    )

    sug_improved = (sug.get("improved_code") or "").strip()

    # 第 1 层: exact_match (建议代码完整出现在目标行附近) — 强证据, 直接采纳.
    exact_match = _exact_improved_code_near_target(
        current_content,
        sug_improved,
        line=target_line,
        line_end=target_line_end,
    )

    # 第 2 层: 严格 token 匹配 (用户改了格式/空白/等效写法, 但核心 token 落地).
    # 与旧版关键区别: 不再把 _target_region_changed 单独作为采纳证据 — 那个判定
    # 太多假阳性 (同文件其它 suggestion 触发 region 改 / 行号漂移都会命中).
    # 新判定需要: 足够数量的新 token 命中 + 旧 token 残余 < 30%.
    token_hit = False
    token_details: dict[str, Any] = {}
    if not exact_match and sug_improved:
        try:
            token_hit, token_details = _strict_token_adoption_match(
                posted_content if posted_content is not None else existing_code,
                current_content,
                line=target_line,
                line_end=target_line_end,
                improved_code=sug_improved,
                existing_code=existing_code or None,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "auto_detect_applied late_detect strict_token failed note={} file={} err={}",
                note_id[:8], file_path, e,
            )
            return "error"
    changed = exact_match or token_hit

    if not changed:
        # region_changed 单独不再算采纳 — 仅用于审计/日志.
        try:
            region_changed = _target_region_changed(
                posted_content if posted_content is not None else existing_code,
                current_content,
                line=target_line,
                line_end=target_line_end,
            )
        except Exception:  # noqa: BLE001
            region_changed = False
        if region_changed:
            logger.info(
                "auto_detect_applied late_detect region_only note={} file={} line={} "
                "(region_changed 但 exact/token 都不命中 → 保持 resolved)",
                note_id[:8], file_path, target_line,
            )
        return "unchanged"

    # 命中 → 翻 applied + 记 action (state 二次校验: 用户可能中途 /adopt)
    current = store.get_suggestion_by_note_id(note_id)
    if current is None or current.get("state") != "resolved":
        cur_state = (current or {}).get("state") or "missing"
        logger.info(
            "auto_detect_applied late_detect skip (state changed) project={} mr={} note={} state={}",
            project_id, mr_iid, note_id[:8], cur_state,
        )
        return "unchanged"

    store.update_suggestion_state(
        note_id, "applied", actor_username=actor_username,
        adoption_source="late_detect",
    )
    store.record_suggestion_action(
        project_id=project_id, mr_iid=mr_iid,
        suggestion_note_id=note_id, file_path=file_path,
        target_line=target_line, action="adopted",
        actor_username=actor_username,
        reason="late_detect: resolved 状态后代码落地, 翻 applied",
        validation_status="late-detect-apply",
        adoption_source="late_detect",
        head_sha_posted=sug.get("head_sha"),
        head_sha_current=head_sha,
    )
    logger.info(
        "auto_detect_applied late_detect flip_to_applied project={} mr={} note={} file={} line={}",
        project_id, mr_iid, note_id[:8], file_path, target_line,
    )
    return "applied"


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


# Fallback: token-level 采纳判定. 当 strip 严格比对失败时使用.
# 适用: 用户改了格式/空白, 或用了等效写法 (如 if x is None vs if not x),
# 但引入的关键标识符仍在目标行附近出现. 这种"采纳"严格文本比对看不出来.
_KEYWORDS = {
    "if", "else", "elif", "for", "while", "return", "def", "class",
    "import", "from", "as", "with", "try", "except", "finally",
    "raise", "pass", "in", "is", "not", "and", "or",
    "True", "False", "None", "lambda", "yield", "global", "nonlocal",
    "assert", "del", "break", "continue", "self", "cls",
}


def _extract_identifiers(code: str) -> set[str]:
    """从代码中提取标识符, 排除 Python 关键字."""
    if not code:
        return set()
    return set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", code)) - _KEYWORDS


def _exact_improved_code_near_target(
    current_content: str,
    improved_code: str,
    *,
    line: int,
    line_end: int,
    context_lines: int = 8,
) -> bool:
    """检查建议代码是否完整出现在目标行附近，忽略行首尾空白."""
    improved_lines = [item.strip() for item in improved_code.splitlines() if item.strip()]
    if not improved_lines:
        return False
    current_lines = current_content.splitlines()
    start = max(0, line - 1 - context_lines)
    end = min(len(current_lines), line_end + context_lines + len(improved_lines))
    nearby = [item.strip() for item in current_lines[start:end]]
    width = len(improved_lines)
    return any(
        nearby[index:index + width] == improved_lines
        for index in range(max(0, len(nearby) - width + 1))
    )


def _token_adoption_match(
    posted_content: str,
    current_content: str,
    *,
    line: int,
    line_end: int,
    improved_code: str,
    existing_code: str | None = None,
    context_lines: int = 5,
    threshold: float = 0.5,
) -> bool:
    """Fallback: improved_code 引入的"新 token"在 current 文件目标行 ±context_lines 范围内
    出现比例 >= threshold → 算采纳.

    Why: 严格 strip 比对会漏掉"用户改了格式但语义一致"或"等效写法"的情况.
    用 token 重叠 + 位置限定 (目标行附近) 兜底.

    Token 选择: improved 引入的"新 token" (排除 existing_code 已有的 + 关键字),
    避免像 'open' / 'path' / 'read' 这种原本就有的 token 误判.

    例子:
      existing: "open(path).read()"
      improved: "with open(path) as f:\n    return f.read()"
      引入新 token: 只看 improved 中存在但 existing 没有的标识符, 例如 {"with", "fp"} 或 "f"
      (实际 with 被 keywords 排除, 只剩 "f" — 这就是新变量)
      current 目标行 ±5 行内出现 "f" (作为新变量引用) → 算采纳

    Returns: True 表示 fallback 判定为采纳.
    """
    if not improved_code or not current_content:
        return False
    improved_tokens = _extract_identifiers(improved_code)
    if not improved_tokens:
        return False
    # 排除 existing_code 已有的 token, 只关注"建议新引入"
    if existing_code:
        existing_tokens = _extract_identifiers(existing_code)
        new_tokens = improved_tokens - existing_tokens
    else:
        new_tokens = improved_tokens
    if not new_tokens:
        return False
    current_lines = current_content.splitlines()
    if line_end < line:
        line_end = line
    lo = max(0, line - 1 - context_lines)
    hi = min(len(current_lines), line_end + context_lines)
    if lo >= hi:
        return False
    window = "\n".join(current_lines[lo:hi])
    window_tokens = _extract_identifiers(window)
    hit = len(new_tokens & window_tokens)
    return hit / len(new_tokens) >= threshold


def _strict_token_adoption_match(
    posted_content: str,
    current_content: str,
    *,
    line: int,
    line_end: int,
    improved_code: str,
    existing_code: str | None = None,
    context_lines: int = 5,
    new_token_ratio: float = 0.8,
    coverage_floor: int = 2,
) -> tuple[bool, dict[str, Any]]:
    """严格版 token 采纳判定 — Batch1 收紧采纳口径使用.

    与 _token_adoption_match 的区别:
      - new_token_ratio 默认 0.8 (旧版 0.5) — 避免单 token 凑比率假阳性.
      - 必须满足 coverage_floor (新 token 命中下限), 单 token 命中不算采纳.
      - 旧 token 必须在窗口里基本消失 (允许 < 30% 公共残留).
      - 返回 (hit, details) 让 caller 写审计字段.

    Returns:
        (True, details)  (False, details)
    """
    if not improved_code or not current_content:
        return False, {"reason": "empty_input"}
    improved_tokens = _extract_identifiers(improved_code)
    if not improved_tokens:
        return False, {"reason": "no_improved_tokens"}
    if existing_code:
        existing_tokens = _extract_identifiers(existing_code)
    else:
        existing_tokens = set()
    new_tokens = improved_tokens - existing_tokens
    old_tokens = existing_tokens - improved_tokens
    if not new_tokens:
        return False, {"reason": "no_new_tokens"}
    current_lines = current_content.splitlines()
    if line_end < line:
        line_end = line
    lo = max(0, line - 1 - context_lines)
    hi = min(len(current_lines), line_end + context_lines)
    if lo >= hi:
        return False, {"reason": "empty_window"}
    window = "\n".join(current_lines[lo:hi])
    window_tokens = _extract_identifiers(window)
    if old_tokens:
        old_remaining = old_tokens & window_tokens
        old_total = len(old_tokens)
        old_remaining_ratio = len(old_remaining) / old_total
    else:
        old_total = 0
        old_remaining_ratio = 0.0
    new_hits = new_tokens & window_tokens
    new_hit_count = len(new_hits)
    new_total = len(new_tokens)
    new_ratio = new_hit_count / new_total if new_total else 0.0
    details = {
        "new_token_total": new_total,
        "new_token_hits": new_hit_count,
        "new_token_ratio": round(new_ratio, 3),
        "old_token_total": old_total,
        "old_token_remaining_ratio": round(old_remaining_ratio, 3),
    }
    if new_hit_count < coverage_floor:
        details["reason"] = "below_coverage_floor"
        return False, details
    if new_ratio < new_token_ratio:
        details["reason"] = "below_new_token_ratio"
        return False, details
    if old_tokens and old_remaining_ratio > 0.3:
        details["reason"] = "old_tokens_remain"
        return False, details
    details["reason"] = "ok"
    return True, details




# ---------- handlers ----------
def process_dismiss(
    *,
    project_id: int,
    mr_iid: int,
    suggestion_note_id: str,
    actor_username: str,
    reason: str,
    file_path: str = "",
    target_line: int = 0,
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

    # 4.5. 立即刷新 MR 顶部"检视汇总" (与下一次 improve run 之间无延迟)
    # Why: 之前只在 improve.py 末尾刷一次, /dismiss 不走 improve pipeline,
    #      汇总停留在上一刻, 用户看到 stale 数据 (MR 247 实测偏差).
    try:
        publish_overview(
            project_id=project_id, mr_iid=mr_iid,
            inline_posted_count=0,
            run_late_detect=False,
            gitlab=gl,
        )
    except Exception as _e:  # noqa: BLE001
        logger.warning("/dismiss.overview_refresh failed (non-fatal): {}", _e)

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


# ---------- /adopt 内置辅助: note_id 短重试 + file:line 兜底 (Fix C) ----------
_ADOPT_LOOKUP_RETRY_DELAYS = (0.0, 0.25, 0.5)


def _lookup_suggestion_with_retry(
    store,
    *,
    suggestion_note_id: str,
    file_path: str = "",
    target_line: int = 0,
    project_id: int,
    mr_iid: int,
):
    """按 note_id 查, 找不到重试一次, 最后按 file:line 兜底.

    Returns: 命中 suggestion dict, 或 None.
    总耗时上限 0.75s, 不阻塞 RQ job 太久.
    """
    for delay in _ADOPT_LOOKUP_RETRY_DELAYS:
        if delay > 0:
            import time as _t
            _t.sleep(delay)
        sug = store.get_suggestion_by_note_id(suggestion_note_id)
        if sug is not None:
            return sug

    if file_path and target_line > 0:
        sug = store.find_open_suggestion_by_line(
            project_id=project_id, mr_iid=mr_iid,
            file_path=file_path, target_line=target_line,
            window=3,
        )
        if sug is not None:
            logger.info(
                "/adopt: note_id miss but file:line hit ({}, L{}, suggestion.id={}); falling back",
                file_path, target_line, sug.get("id"),
            )
            return sug

    return None


def process_adopt(
    *,
    project_id: int,
    mr_iid: int,
    suggestion_note_id: str,
    actor_username: str,
    reason: str,
    file_path: str = "",
    target_line: int = 0,
) -> dict[str, Any]:
    """处理 /adopt 命令.

    Returns: {\"action\": \"adopted\"|\"adopt-validation-failed\"|\"adopt-failed\", \"reason\": str, \"validation\": str}
    """
    gl = GitLabClient()
    store = get_store()

    # 1. 找 suggestion 记录 (重试 1 次, file:line 兜底 — 见 _lookup_suggestion_with_retry)
    sug = _lookup_suggestion_with_retry(
        store, suggestion_note_id=suggestion_note_id,
        file_path=file_path, target_line=target_line,
        project_id=project_id, mr_iid=mr_iid,
    )
    if sug is None:
        # 真没找到 — 历史 MR / 跨 project 数据 / 人工发的 note 等
        # 仍然尝试 resolve (让用户至少能看到反馈)
        logger.info(
            "/adopt: no suggestion record for note_id={} file={} line={}, allowing resolve anyway",
            suggestion_note_id, file_path or "-", target_line or 0,
        )
        gl.resolve_discussion(project_id, mr_iid, suggestion_note_id)
        gl.reply_to_discussion(
            project_id, mr_iid, suggestion_note_id,
            "✅ 已采纳建议 (无历史记录, 跳过验证)。",
        )
        return {"action": "adopted-unchecked", "reason": "no_record"}

    # 命中 file:line 兜底 (note_id 没找到但 file:line 命中) → 同步把真实 note_id 回填
    if sug.get("note_id") != suggestion_note_id:
        try:
            store.update_suggestion_note_id(sug["id"], suggestion_note_id)
            logger.info(
                "/adopt: backfilled note_id suggestion.id={} old={} new={}",
                sug["id"], sug.get("note_id"), suggestion_note_id,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("/adopt backfill note_id failed: {}", e)

    # 2. 检查 suggestion 状态
    #    state=applied/dismissed: 之前已被处理过 (auto_detect 或用户 /adopt / /dismiss).
    #    用户的 /adopt 仍然要被记账: 1) 显式 reply 告诉用户"已经被处理",
    #    2) 把用户提供的 reason 写进 suggestion_actions 让审计可追溯,
    #    3) 触发 re-improve (用户主动发 /adopt 通常意味着他/她想看最新 diff 状态).
    if sug.get("state") not in ("open",):
        state = sug.get("state") or "unknown"
        logger.info(
            "/adopt skipped state={} note_id={} actor={} reason={!r}",
            state, suggestion_note_id, actor_username, (reason or "")[:50],
        )
        # 1. reply 反馈: 简洁说明当前状态 + /adopt reason 的去向
        if state == "applied":
            reply_lines = [
                "ℹ️ 这条建议已被自动检测采纳 (push 时代码改动触发系统识别)。"
            ]
            if reason:
                reply_lines.append(
                    f"你的 /adopt 理由: `{reason}` 将用于改进后续建议质量。"
                )
            else:
                reply_lines.append(
                    "如需记录采纳思路, 用 `/adopt 你的理由`。"
                )
        elif state == "dismissed":
            reply_lines = [
                "ℹ️ 这条建议之前已被 /dismiss 关闭, 不会再次标记为采纳。"
                "如要重新打开讨论, 建议直接 push 新 commit 让系统重新检视。"
            ]
        else:
            reply_lines = [f"ℹ️ 这条建议当前状态: {state}。"]
        try:
            gl.reply_to_discussion(
                project_id, mr_iid, suggestion_note_id, "\n".join(reply_lines)
            )
        except GitLabError as e:
            logger.warning("/adopt.reply_for_skipped_state failed: {}", e)
        # 2. 写审计: 即使 state 已是 applied, 用户的 /adopt 行为也入 audit
        #    (validation_status=already-{state} 区分 auto_detect 的 ui-apply)
        sug_file = sug.get("file_path") or ""
        sug_line = int(sug.get("target_line") or 0)
        # 0. resolve discussion: /adopt 表示用户已经处理这条建议 (无论是
        #    state=applied 还是 dismissed), thread 应该自动关闭对勾.
        #    Why: 之前只 reply 用户但没调 resolve, 用户仍要手动点对勾按钮.
        #         用户的 /adopt 行为 = "我处理完了", 必须自动 resolve.
        try:
            gl.resolve_discussion(project_id, mr_iid, suggestion_note_id)
        except GitLabError as e:
            logger.warning("/adopt.resolve_for_skipped_state failed: {}", e)
        store.record_suggestion_action(
            project_id=project_id,
            mr_iid=mr_iid,
            suggestion_note_id=suggestion_note_id,
            file_path=sug_file,
            target_line=sug_line,
            action="adopted",
            actor_username=actor_username,
            reason=reason or f"user /adopt after state={state}",
            validation_status=f"already-{state}",
        )
        # 3. 触发 re-improve: 用户主动表达意愿, 给他/她看到最新 diff
        reimprove_job = _maybe_enqueue_reimprove(
            project_id=project_id, mr_iid=mr_iid, actor_username=actor_username,
        )
        result = {"action": "adopt-skipped", "reason": f"state={state}"}
        if reimprove_job:
            result["reimprove_job"] = reimprove_job
        return result

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
    # Fallback: 严格 strip 比对失败时, 用 token 重叠判定"用户改了格式但语义一致"
    # 的情况. 例如建议加 `with open(...) as f:`, 用户写了等效写法.
    if not changed:
        sug_improved = (sug.get("improved_code") or "").strip()
        if sug_improved:
            changed = _token_adoption_match(
                posted_content, current_content,
                line=target_line, line_end=target_line_end,
                improved_code=sug_improved,
                existing_code=(sug.get("existing_code") or "").strip() or None,
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
        suggestion_note_id, "applied", actor_username=actor_username,
        adoption_source="adopt_command",
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
        adoption_source="adopt_command",
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

    # 立即刷新 MR 顶部"检视汇总" — /adopt 完成后用户能秒级看到状态变化.
    # Why: 同 process_dismiss, 不等下一次 improve run 才更新.
    # 失败非致命 (后续 re-improve 也会再刷一次).
    try:
        publish_overview(
            project_id=project_id, mr_iid=mr_iid,
            inline_posted_count=0,
            run_late_detect=False,
            gitlab=gl,
        )
    except Exception as _e:  # noqa: BLE001
        logger.warning("/adopt.overview_refresh failed (non-fatal): {}", _e)

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


# ---------- GitLab UI "Apply suggestion" 自动同步 ----------
def mark_suggestion_applied_by_diff(
    *,
    project_id: int,
    mr_iid: int,
    file_path: str,
    target_line: int,
    actor_username: str,
    source_note_id: int | None = None,
) -> int | None:
    """把 file:line 处仍 open 的 suggestion 标记为 applied.

    Trigger: 用户在 GitLab UI 点击 "Apply suggestion" — GitLab 会发系统 DiffNote
    "changed this line in version N of the diff", position 指向具体 file:line.
    这里直接命中 DB 里同 file:line 的 open suggestion, 把它转成 applied,
    不再依赖 /adopt 命令, 也不再触发 re-improve (因为 push webhook 已经触发过了).

    Returns: 被更新的 suggestion id, 或 None (没有 open suggestion 匹配).
    """
    store = get_store()
    try:
        sug = store.find_open_suggestion_by_line(
            project_id=project_id,
            mr_iid=mr_iid,
            file_path=file_path,
            target_line=target_line,
        )
    except Exception as e:  # pragma: no cover
        logger.warning("system_applied lookup failed: {}", e)
        return None
    if sug is None:
        return None
    note_id = sug.get("note_id")
    if not note_id:
        return None
    try:
        store.update_suggestion_state(
            note_id, "applied", actor_username=actor_username,
            adoption_source="ui_apply",
        )
        # 2. 把同 cohort 旧条 superseded — 避免 "applied 1 + 旧 open 1" 的 cohort 重复
        # Why: push → improve → 旧 cohort note_id 仍 open, 调一次 supersede 让 build_overview
        #      只看到最新一条 (applied), 不再双发.
        try:
            cohort_key = sug.get("cohort_key") or ""
            if cohort_key:
                store.supersede_stale_in_cohort(
                    project_id=project_id, mr_iid=mr_iid,
                    cohort_key=cohort_key, keep_note_id=note_id,
                )
        except Exception as _e:  # noqa: BLE001
            logger.warning("system_applied.supersede_in_cohort failed (non-fatal): {}", _e)
        store.record_suggestion_action(
            project_id=project_id,
            mr_iid=mr_iid,
            suggestion_note_id=note_id,
            file_path=file_path,
            target_line=target_line,
            action="adopted",
            actor_username=actor_username,
            reason="adopted via GitLab UI",
            validation_status="gitlab-ui-apply",
            adoption_source="ui_apply",
        )
    except Exception as e:  # pragma: no cover
        logger.warning("system_applied update failed: {}", e)
        return None
    logger.info(
        "system_applied project={} mr={} file={} line={} suggestion={} via_note={}",
        project_id, mr_iid, file_path, target_line, sug.get("id"), source_note_id,
    )
    # 立即刷新 MR 顶部"检视汇总" — UI Apply 后秒级反映到汇总表.
    # Why: 同 process_dismiss / process_adopt. 不等 push webhook 触发的下一轮检视.
    try:
        publish_overview(
            project_id=project_id, mr_iid=mr_iid,
            inline_posted_count=0,
            run_late_detect=False,
        )
    except Exception as _e:  # noqa: BLE001
        logger.warning("system_applied.overview_refresh failed (non-fatal): {}", _e)
    return sug.get("id")


# ---------- Sync GitLab resolved → DB resolved (webhook 触发) ----------
def sync_resolved_from_gitlab(
    *,
    project_id: int,
    mr_iid: int,
    actor_username: str = "gitlab-resolve",
) -> dict[str, Any]:
    """把 GitLab 已 resolved 但 DB 仍 open 的 suggestion 标 resolved.

    Trigger: webhook 收到 GitLab 系统 note
      - "marked this discussion as resolved" (单条 ✓)
      - "resolved all threads" (批量 ✓)
    用户在 UI 直接点解决主题 (没改代码), 没 push webhook 触发, DB 永远停在 open.

    Why: 之前依赖 push 触发 reconcile, 但"只点 ✓ 不改代码"的纯 UI 操作
    不会触发 push, 留下孤儿 (MR 247 e50f4c0d4d4e 实测).

    与 auto_detect_applied 的区别:
    - auto_detect_applied: 跑在 push webhook, 同时检测 code 落地 + discussion 状态,
      命中时直接翻 applied; 不命中才标 resolved.
    - 本函数: 跑在 resolve webhook, 只对 state=open 的 suggestion 做一次"是否被
      GitLab UI resolve"的扫, 命中就标 resolved (永远不进 applied — 没有 code 落地).

    Returns: {"scanned": int, "updated": int, "note_ids": list[str]}
    """
    result = _scan_and_mark_resolved_silent(
        project_id=project_id, mr_iid=mr_iid,
        actor_username=actor_username,
        adoption_source="gitlab_resolve",
        reason="GitLab UI 直接解决主题 (无 push 触发)",
        validation_status="gitlab-resolve",
    )

    if result["updated"]:
        # 立即刷新 MR 顶部"检视汇总"
        try:
            publish_overview(
                project_id=project_id, mr_iid=mr_iid,
                inline_posted_count=0,
                run_late_detect=False,
            )
        except Exception as _e:  # noqa: BLE001
            logger.warning("sync_resolved.overview_refresh failed (non-fatal): {}", _e)

    return result


# ---------- Silent reconcile: 扫描 + 标 resolved, 不调用 publish_overview ----------
def _scan_and_mark_resolved_silent(
    *,
    project_id: int,
    mr_iid: int,
    actor_username: str,
    adoption_source: str,
    reason: str,
    validation_status: str,
) -> dict[str, Any]:
    """扫描 MR 全部 state=open suggestion, 把 GitLab UI 已 resolved 的标 resolved.

    不调用 publish_overview (供 publish_overview 自己调用, 避免递归).
    被两个上层函数共用:
      - sync_resolved_from_gitlab (webhook 触发, 末尾会调 publish_overview)
      - publish_overview.pre_reconcile (publish_overview 顶部调, 用于 catch-up
        "用户在 UI 点 ✓ 但 GitLab 没发 webhook" 这种孤儿状态)

    Returns: {"scanned": int, "updated": int, "note_ids": list[str]}
    """
    gl = GitLabClient()
    store = get_store()

    open_sugs = store.list_open_suggestions(project_id=project_id, mr_iid=mr_iid)
    result: dict[str, Any] = {
        "scanned": len(open_sugs),
        "updated": 0,
        "note_ids": [],
    }

    for sug in open_sugs:
        note_id = sug.get("note_id") or ""
        if not note_id:
            continue
        try:
            resolved = gl.is_discussion_resolved(project_id, mr_iid, note_id)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "reconcile_resolved.is_discussion_resolved failed note={} err={}",
                note_id[:8], e,
            )
            continue
        if resolved is True:
            # expected_states=("open",) 原子化 state guard:
            # 若用户 /dismiss 在 list_open_suggestions → update_suggestion_state 之间
            # 把 state 改成 dismissed, SQL WHERE state IN ('open') 命中 0 行 → 跳过,
            # 不会覆盖 dismissed → resolved (MR264 回归修复).
            updated = store.update_suggestion_state(
                note_id, "resolved", actor_username=actor_username,
                adoption_source=adoption_source,
                expected_states=("open",),
            )
            if not updated:
                logger.info(
                    "reconcile_resolved skipped (state changed) project={} mr={} note={} "
                    "src={} — likely dismissed/applied during scan",
                    project_id, mr_iid, note_id[:8], adoption_source,
                )
                continue
            store.record_suggestion_action(
                project_id=project_id, mr_iid=mr_iid,
                suggestion_note_id=note_id,
                file_path=sug.get("file_path"),
                target_line=sug.get("target_line"),
                action="resolved",
                actor_username=actor_username,
                reason=reason,
                validation_status=validation_status,
                adoption_source=adoption_source,
            )
            result["updated"] += 1
            result["note_ids"].append(note_id)
            logger.info(
                "reconcile_resolved project={} mr={} note={} src={} file={} line={}",
                project_id, mr_iid, note_id[:8], adoption_source,
                sug.get("file_path"), sug.get("target_line"),
            )

    return result


# ---------- Auto-detect UI-applied suggestions ----------

def auto_detect_applied(
    *,
    project_id: int,
    mr_iid: int,
    head_sha: str,
    actor_username: str = "auto-detect",
) -> dict[str, Any]:
    """当 head_sha 变化 (push / MR update) 时, 探测哪些 open suggestion
    已被用户在 GitLab UI "Apply suggestion" 了.

    Returns: {
        "scanned": int,   # 检查的总条数
        "applied": int,   # 自动转 state=applied 的条数
        "unchanged": int, # 目标行未变, 保留 open
        "errors": int,    # 探测失败 (文件找不到 / 权限等)
        "applied_note_ids": [str, ...],
    }

    Why: 之前用户 UI apply 不会触发 webhook note 事件, 导致 telemetry
    state 永远停留在 open. 现在 MR 每次有 head_sha 变化 (push, UI apply,
    force-push, merge from web) 都会先跑一次探测, 把已应用的转 applied,
    再触发后续的 describe+improve (作为二次检视).
    """
    gl = GitLabClient()
    store = get_store()

    # Batch4: 一次性拉取 MR 全部 commit, 用于把 suggestion 关联到 Apply suggestion commit.
    # 不在循环内每条拉一次 (N+1 问题). miss 时直接 pass — 旧逻辑不受影响.
    _apply_commits: list[dict[str, Any]] = []
    try:
        _apply_commits = gl.list_mr_commits(project_id, mr_iid)
    except Exception as e:  # noqa: BLE001
        logger.info("auto_detect_applied.list_mr_commits failed (non-fatal): {}", e)

    open_sugs = store.list_open_suggestions(project_id=project_id, mr_iid=mr_iid)
    result: dict[str, Any] = {
        "scanned": len(open_sugs),
        "applied": 0,
        "unchanged": 0,
        "errors": 0,
        "resolved": 0,
        "applied_note_ids": [],
        "late_apply": 0,           # resolved → applied 翻转数
        "late_apply_note_ids": [], # 翻转的 note_id 列表
    }
    for sug in open_sugs:
        note_id = sug.get("note_id") or ""
        file_path = sug.get("file_path") or ""
        target_line = int(sug.get("target_line") or 0)
        target_line_end = int(sug.get("target_line_end") or target_line)
        existing_code = sug.get("existing_code") or ""
        if not (note_id and file_path and target_line and existing_code):
            continue

        resolved = gl.is_discussion_resolved(project_id, mr_iid, note_id)

        # 拿当前 head_sha 下的文件内容
        current_content = gl.get_file_at_sha(project_id, file_path, head_sha)
        if current_content is None:
            # 文件可能已被删除 / 移到别的位置
            result["errors"] += 1
            logger.info(
                "auto_detect_applied skip (no file) project={} mr={} note={} file={}",
                project_id, mr_iid, note_id[:8], file_path,
            )
            continue

        # 拿 posted 时代 (sug.head_sha) 的整个文件, 跟 current_content 做 file-level diff
        # Why: telemetry 存的 existing_code 只是 target line(s) 几行, 不够
        # _target_region_changed 用 LCS 找 offset (假设 posted_content 是整个文件).
        # 直接用 posted 时代整个文件 vs 当前整个文件, 精确判断"目标行是否被改".
        posted_content = gl.get_file_at_sha(
            project_id, file_path, sug.get("head_sha") or head_sha
        )

        sug_improved = (sug.get("improved_code") or "").strip()
        exact_match = _exact_improved_code_near_target(
            current_content,
            sug_improved,
            line=target_line,
            line_end=target_line_end,
        )

        # unresolved discussion 只接受建议代码精确落地，避免把同位置的其它修改误算采纳。
        if resolved is not True:
            if not exact_match:
                result["unchanged"] += 1
                if resolved is None:
                    result["errors"] += 1
                continue
            changed = True
        else:
            changed = exact_match

        # resolved discussion: Batch1 收紧采纳口径.
        # 旧逻辑: exact_match=False 时, region_changed 单独即可算采纳 (太多假阳性).
        # 新逻辑: 必须 exact_match 或严格 token 匹配; region_changed 仅审计.
        token_hit = False
        if not changed and sug_improved:
            try:
                token_hit, _token_details = _strict_token_adoption_match(
                    posted_content if posted_content is not None else existing_code,
                    current_content,
                    line=target_line,
                    line_end=target_line_end,
                    improved_code=sug_improved,
                    existing_code=existing_code or None,
                )
                if token_hit:
                    logger.info(
                        "auto_detect_applied strict_token note={} file={} line={}",
                        note_id[:8], file_path, target_line,
                    )
            except Exception as e:  # noqa: BLE001
                result["errors"] += 1
                logger.warning(
                    "auto_detect_applied strict_token failed note={} file={} err={}",
                    note_id[:8], file_path, e,
                )
                continue
            changed = token_hit

        if not changed and resolved is True:
            # 审计: region_changed 单独不足以采纳, 但要记录到日志便于排查
            try:
                region_changed = _target_region_changed(
                    posted_content if posted_content is not None else existing_code,
                    current_content,
                    line=target_line,
                    line_end=target_line_end,
                )
            except Exception:  # noqa: BLE001
                region_changed = False
            if region_changed:
                logger.info(
                    "auto_detect_applied region_only note={} file={} line={} "
                    "(region_changed 但 exact/token 都不命中 → 保持当前状态)",
                    note_id[:8], file_path, target_line,
                )

        # delete_range 特殊判定: improved_code 为空 + existing_code 非空 →
        # 这条建议是"删除一段代码", 必须验证 target 行在 current 文件中**消失**
        # 才算采纳. 否则用户只是改了 L33 内容, 不算"删".
        if not changed:
            sug_existing = (sug.get("existing_code") or "").strip()
            sug_improved_full = (sug.get("improved_code") or "").strip()
            if not sug_improved_full and sug_existing and current_content is not None:
                # 用 posted_content 拿到 target 行的实际内容, 检查 current 是否还有这段
                src_content = posted_content if posted_content is not None else current_content
                src_lines = src_content.splitlines()
                lo = max(0, target_line - 1)
                hi = min(len(src_lines), target_line_end)
                if lo < hi:
                    deleted_block = "\n".join(src_lines[lo:hi]).strip()
                    if deleted_block and deleted_block not in current_content:
                        changed = True
                        logger.info(
                            "auto_detect_applied delete_range_confirmed note={} file={} line={}",
                            note_id[:8], file_path, target_line,
                        )
                    elif deleted_block:
                        logger.info(
                            "auto_detect_applied delete_range_not_confirmed note={} file={} line={} (block still present)",
                            note_id[:8], file_path, target_line,
                        )

        if not changed:
            # GitLab “解决主题”只表示 Discussion 被关闭，不能直接算采纳。
            # 只有前面的代码比对确认目标已落地时才进入 applied；否则单独记录为
            # resolved，避免覆盖同 diff 手动修改的自动采纳判定。
            if resolved is True:
                current = store.get_suggestion_by_note_id(note_id)
                if current and current.get("state") == "open":
                    store.update_suggestion_state(
                        note_id, "resolved", actor_username=actor_username,
                        adoption_source="gitlab_resolve",
                    )
                    store.record_suggestion_action(
                        project_id=project_id, mr_iid=mr_iid,
                        suggestion_note_id=note_id, file_path=file_path,
                        target_line=target_line, action="resolved",
                        actor_username=actor_username,
                        reason="GitLab 直接解决主题，未检测到建议代码落地",
                        validation_status="gitlab-resolve",
                        adoption_source="gitlab_resolve",
                        head_sha_posted=sug.get("head_sha"),
                        head_sha_current=head_sha,
                    )
                    result.setdefault("resolved", 0)
                    result["resolved"] += 1
                continue
            result["unchanged"] += 1
            continue

        # 二次校验: 用户可能在 auto_detect 处理这条 (slow 文件读取 / diff 计算)
        # 期间通过 /dismiss 把 state 改成 dismissed, 此时不应再覆盖为 applied.
        # Race 修复 Fix D: 重新 fetch 当前 state, 若非 open 则跳过.
        # Why: list_open_suggestions 在循环开始时一次性 fetch, 单条处理可能耗
        #      1-2s (网络往返 + LCS diff), 用户 /dismiss 命中后这条会被标 dismissed.
        #      若不重检, dismiss 状态会被 silently 覆盖成 applied, 数据失真.
        current = store.get_suggestion_by_note_id(note_id)
        if current is None or current.get("state") != "open":
            cur_state = (current or {}).get("state") or "missing"
            logger.info(
                "auto_detect_applied skip (state changed mid-scan) project={} mr={} note={} state={}",
                project_id, mr_iid, note_id[:8], cur_state,
            )
            if cur_state == "dismissed":
                result["dismissed_during_scan"] = result.get("dismissed_during_scan", 0) + 1
            else:
                result["unchanged"] += 1
            continue

        # 已应用 → resolve + 记录 + 改 state
        try:
            gl.resolve_discussion(project_id, mr_iid, note_id)
        except GitLabError as e:
            logger.warning("auto_detect_applied resolve failed: {}", e)

        adoption_source = "ui_apply" if resolved is True else "manual_change"
        # Batch1/4: 写证据等级 + Apply commit 短 SHA (审计用)
        adoption_evidence = "exact_match" if exact_match else "strict_token"
        applied_commit_sha = _find_latest_apply_commit(
            _apply_commits, head_sha=head_sha,
        )
        store.update_suggestion_state(
            note_id, "applied", actor_username=actor_username,
            adoption_source=adoption_source,
            adoption_evidence=adoption_evidence,
            applied_commit_sha=applied_commit_sha or None,
        )
        store.record_suggestion_action(
            project_id=project_id,
            mr_iid=mr_iid,
            suggestion_note_id=note_id,
            file_path=file_path,
            target_line=target_line,
            action="adopted",
            actor_username=actor_username,
            reason="auto-detected: user adopted via GitLab UI before reply /adopt",
            validation_status="ui-apply",
            adoption_source=adoption_source,
            head_sha_posted=sug.get("head_sha"),
            head_sha_current=head_sha,
        )
        result["applied"] += 1
        result["applied_note_ids"].append(note_id)
        logger.info(
            "auto_detect_applied project={} mr={} note={} file={} line={} "
            "evidence={} commit={}",
            project_id, mr_iid, note_id[:8], file_path, target_line,
            adoption_evidence, applied_commit_sha or "-",
        )

    # ---------- Late detect: 把 state='resolved' 且 resolution_source='gitlab_resolve'
    # 的误分类 suggestions 翻回 applied.
    #
    # 背景: 用户可能先在 GitLab UI 点「解决主题」关掉 discussion (这时 bot 还不知道
    # 是否采纳), 然后才 push commit 让代码落地. 当时 auto_detect 跑那一遍
    # exact_match 没命中 (因为 head_sha 还停在 push 前) → bot 把它标 resolved.
    # 后续 push 触发再跑时, 这条 suggestion 已不在 list_open_suggestions 里, 永远
    # 不会被重检, 数据就一直停在"已关闭 (未分类)"但实际代码已采纳的状态.
    #
    # 修复: 在 open 扫完后, 再扫一遍 resolution_source='gitlab_resolve' 的 resolved
    # suggestions, 用同一套 exact_match / region_changed / token_fallback 重新判定.
    # 命中就翻 applied + 记一条 action=adopted, adoption_source='late_detect',
    # validation_status='late-detect-apply', 保留前一条 resolved action 作历史.
    #
    # Why 只扫 'gitlab_resolve': /adopt 流程走的是 adoption_source='adopt_command',
    # /dismiss 状态是 dismissed 也不会落进 resolved, 这两类都不应被覆盖回 applied.
    late_sugs = store.list_resolved_suggestions(project_id=project_id, mr_iid=mr_iid)
    for sug in late_sugs:
        late_result = _late_detect_single(
            sug=sug,
            head_sha=head_sha,
            project_id=project_id,
            mr_iid=mr_iid,
            actor_username=actor_username,
        )
        if late_result == "applied":
            result["late_apply"] += 1
            result["late_apply_note_ids"].append(sug.get("note_id"))
        elif late_result == "error":
            result["errors"] += 1

    if result.get("applied") or result.get("late_apply") or result.get("resolved"):
        logger.info(
            "auto_detect_applied summary project={} mr={} {}",
            project_id, mr_iid, result,
        )
    # 修 MR262: 用户点 ✓ 后 auto_detect 标 resolved 但没刷检视汇总.
    # 现在跟 /dismiss / /adopt / ui_apply / sync_resolved 一样, 状态一变就
    # 立即刷一次. 用 run_late_detect=False 避免与 publish_overview 自身的
    # late_detect 递归 (auto_detect_applied 会被 publish_overview 调用).
    if result.get("applied") or result.get("resolved") or result.get("late_apply"):
        try:
            publish_overview(
                project_id=project_id, mr_iid=mr_iid,
                inline_posted_count=0,
                run_late_detect=False,
            )
        except Exception as _e:  # noqa: BLE001
            logger.warning(
                "auto_detect_applied.overview_refresh failed (non-fatal): {}", _e,
            )
    return result
