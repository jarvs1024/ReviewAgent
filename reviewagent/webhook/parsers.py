"""Webhook payload 解析 + 命令提取."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


def _display_name(user: dict) -> str:
    """从 GitLab user 对象生成 '中文名字@工号' 格式."""
    name = (user.get("name") or "").strip()
    username = (user.get("username") or "").strip()
    if name and username:
        return f"{name}@{username}"
    return username or name or ""


# ---------- MR Hook ----------
@dataclass
class MRHookPayload:
    """MR Hook 解构后的最小可用信息."""
    project_id: int
    mr_iid: int
    action: str                 # open / update / merge / close / reopen
    actor_username: str
    title: str
    source_branch: str
    target_branch: str
    state: str
    head_sha: str = ""          # diff_refs.head_sha — 用于判断是否有新 commit

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "MRHookPayload":
        obj = payload.get("object_attributes", {})
        proj = payload.get("project", {})
        author = payload.get("user", {})
        diff_refs = obj.get("diff_refs", {}) or {}
        last_commit = obj.get("last_commit", {}) or {}
        head_sha = (
            diff_refs.get("head_sha", "")
            or last_commit.get("id", "")
            or (payload.get("diff_refs") or {}).get("head_sha", "")
        )
        return cls(
            project_id=proj.get("id", 0),
            mr_iid=obj.get("iid", 0),
            action=obj.get("action", ""),
            actor_username=_display_name(author),
            title=obj.get("title", ""),
            source_branch=obj.get("source_branch", ""),
            target_branch=obj.get("target_branch", ""),
            state=obj.get("state", "opened"),
            head_sha=head_sha,
        )


# ---------- Note Hook ----------
@dataclass
class NoteHookPayload:
    """Note Hook 解构 — 仅关心 MR 评论."""
    project_id: int
    mr_iid: int
    note_id: int
    note_body: str
    actor_username: str
    note_type: str = ""           # "" / "DiscussionNote" / "DiffNote"
    discussion_id: str = ""        # 关联的 discussion (用于 /adopt /dismiss)
    noteable_type: str = ""        # "MergeRequest" 等
    is_system: bool = False        # GitLab 系统提示 (e.g. "changed this line")
    diff_file: str = ""            # DiffNote 指向的文件 (用于 system apply 匹配)
    diff_line: int = 0             # DiffNote 指向的行号

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "NoteHookPayload | None":
        obj = payload.get("object_attributes", {})
        noteable_type = obj.get("noteable_type", "")
        # 仅处理 MR 评论
        if noteable_type != "MergeRequest":
            return None
        # 必须有 merge_request 字段
        mr = payload.get("merge_request") or payload.get("issue", {})
        if not mr:
            return None
        proj = payload.get("project", {})
        author = payload.get("user", {})
        position = obj.get("position") or obj.get("original_position") or {}
        return cls(
            project_id=proj.get("id", 0),
            mr_iid=mr.get("iid", 0),
            note_id=obj.get("id", 0),
            note_body=obj.get("note", "") or "",
            actor_username=_display_name(author),
            note_type=obj.get("type", ""),
            discussion_id=str(obj.get("discussion_id", "") or ""),
            noteable_type=noteable_type,
            is_system=bool(obj.get("system", False)),
            diff_file=position.get("new_path", "") or position.get("old_path", "") or "",
            diff_line=int(position.get("new_line") or position.get("old_line") or 0),
        )


# ---------- 命令提取 ----------
COMMAND_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^/describe\b", re.MULTILINE), "describe"),
    (re.compile(r"^/improve\b", re.MULTILINE), "improve"),
]


def extract_command(note_body: str) -> str | None:
    """从 note body 提取命令（行首匹配，避免 /reviewed 误命中）."""
    if not note_body:
        return None
    for pattern, cmd in COMMAND_PATTERNS:
        if pattern.search(note_body):
            return cmd
    return None