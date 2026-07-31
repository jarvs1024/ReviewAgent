"""telemetry 数据契约 — dataclass.

PoC 阶段先定义两个核心 dataclass：
    MRRecord: MR 元信息快照
    ReviewRun: 一次检视任务的执行记录
Phase 2 再扩展 Suggestion / ActionEvent.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------- MR 元信息 ----------
@dataclass
class MRRecord:
    project_id: int
    mr_iid: int
    title: str
    author_username: str
    source_branch: str
    target_branch: str
    state: str
    created_at: datetime | None = None
    updated_at: datetime | None = None
    merged_at: datetime | None = None
    description_generated: bool = False
    last_review_at: datetime | None = None

    @classmethod
    def from_gitlab(cls, mr: dict) -> "MRRecord":
        """从 GitLab API 返回的 MR dict 构造."""
        author = mr.get("author") or {}
        name = (author.get("name") or "").strip()
        username = author.get("username", "unknown")
        author_display = f"{name}@{username}" if name else username
        return cls(
            project_id=mr["project_id"],
            mr_iid=mr["iid"],
            title=mr.get("title", ""),
            author_username=author_display,
            source_branch=mr.get("source_branch", ""),
            target_branch=mr.get("target_branch", ""),
            state=mr.get("state", "opened"),
            created_at=_parse_dt(mr.get("created_at")),
            updated_at=_parse_dt(mr.get("updated_at")),
            merged_at=_parse_dt(mr.get("merged_at")),
        )


# ---------- 检视任务执行 ----------
@dataclass
class ReviewRun:
    project_id: int
    mr_iid: int
    command: str                   # describe / review / improve
    triggered_by: str              # webhook / note / scheduled
    actor_username: str = ""
    started_at: datetime = field(default_factory=_now)
    finished_at: datetime | None = None
    status: str = "running"        # running / success / failed / timeout
    error: str | None = None
    model: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    duration_ms: int = 0


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # GitLab ISO 8601: "2024-01-15T10:30:00.000Z"
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None