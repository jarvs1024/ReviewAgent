"""Weekly artifact — structured 周报持久化层 (JSON 落盘 + dataclass).

参考 pr-agent: pr_agent/reporting/report.py
    {
        "schema_version": 1,
        "project_id": int,
        "week_label": "2026-W30",
        "week_start": iso,
        "week_end": iso,
        "generated_at": iso,
        "timezone": "Asia/Shanghai",
        "report_title": "...",
        "report_emoji": "...",
        "sections": {section_name: SectionResult}
    }
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from reviewagent.logging_setup import logger

from .collectors.base import SectionResult


SCHEMA_VERSION = 2


def iso_week_label(dt: datetime) -> str:
    """Return ISO-week label like ``2026-W30``."""
    iso = dt.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


@dataclass
class WeeklyArtifact:
    """Structured weekly report — JSON 落盘 + 可渲染 markdown."""
    project_id: int
    week_label: str
    week_start: datetime
    week_end: datetime
    timezone: str
    sections: dict[str, SectionResult] = field(default_factory=dict)
    generated_at: datetime | None = None
    report_title: str = "ReviewAgent 项目代码检视周报"
    report_emoji: str = "📊"
    dashboard_url: str = ""  # 检视看板地址, 周报「本周检视概况」末尾链接

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "project_id": self.project_id,
            "week_label": self.week_label,
            "week_start": self.week_start.isoformat(),
            "week_end": self.week_end.isoformat(),
            "generated_at": (self.generated_at or datetime.now().astimezone()).isoformat(),
            "timezone": self.timezone,
            "report_title": self.report_title,
            "report_emoji": self.report_emoji,
            "dashboard_url": self.dashboard_url,
            "sections": {n: sr.to_dict() for n, sr in self.sections.items()},
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "WeeklyArtifact":
        return cls(
            project_id=data["project_id"],
            week_label=data["week_label"],
            week_start=datetime.fromisoformat(data["week_start"]),
            week_end=datetime.fromisoformat(data["week_end"]),
            timezone=data.get("timezone", "UTC"),
            generated_at=datetime.fromisoformat(data["generated_at"]) if data.get("generated_at") else None,
            report_title=data.get("report_title", "ReviewAgent 项目代码检视周报"),
            report_emoji=data.get("report_emoji", "📊"),
            dashboard_url=data.get("dashboard_url", ""),
            sections={
                n: SectionResult(
                    status=sr.get("status", "ok"),
                    data=sr.get("data"),
                    markdown=sr.get("markdown"),
                    error=sr.get("error"),
                    meta=sr.get("meta", {}),
                )
                for n, sr in data.get("sections", {}).items()
            },
        )


def build_artifact(
    *,
    project_id: int,
    week_start: datetime,
    week_end: datetime,
    timezone: str,
    sections: Mapping[str, SectionResult],
    report_title: str = "ReviewAgent 项目代码检视周报",
    report_emoji: str = "📊",
    dashboard_url: str = "",
) -> WeeklyArtifact:
    return WeeklyArtifact(
        project_id=project_id,
        week_label=iso_week_label(week_start),
        week_start=week_start,
        week_end=week_end,
        timezone=timezone,
        sections=dict(sections),
        generated_at=datetime.now().astimezone(),
        report_title=report_title,
        report_emoji=report_emoji,
        dashboard_url=dashboard_url,
    )


def write_artifact(artifact: WeeklyArtifact, out_path: Path) -> Path:
    """JSON 落盘, 父目录自动建."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(artifact.to_json(), encoding="utf-8")
    logger.info("reporting.artifact written path={} bytes={}", out_path, out_path.stat().st_size)
    return out_path


__all__ = [
    "SCHEMA_VERSION", "WeeklyArtifact",
    "iso_week_label", "build_artifact", "write_artifact",
]
