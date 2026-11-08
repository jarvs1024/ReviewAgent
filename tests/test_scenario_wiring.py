"""配置项接线验证：review_config_dir / weekly_timezone / weekly_cron_schedule.

审计发现这三项"看起来生效、实际无效"（见
docs/superpowers/specs/2026-11-08-unified-e2e-design.md §2）。
"""
from __future__ import annotations

import dataclasses
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from reviewagent import config as config_mod
from reviewagent.config import current
from reviewagent.reporting.config import WeeklyReportConfig
from reviewagent.reporting.runner import _week_bounds, run_weekly_job
from reviewagent.scenario.profile import load_profile


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _write_profile(root: Path, name: str) -> Path:
    profile_dir = root / name
    _write_yaml(
        profile_dir / "profile.yaml",
        {
            "schema_version": 1,
            "name": name,
            "language": "python",
            "prompts": {"improve": "prompts/improve.md", "describe": "prompts/describe.md"},
        },
    )
    for rel in ("prompts/improve.md", "prompts/describe.md"):
        path = profile_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# prompt\n", encoding="utf-8")
    return profile_dir


def test_review_config_dir_comes_from_config_object(tmp_path, monkeypatch):
    """review_config_dir 由 Config 字段驱动，而不是只在 profile.py 里重读 env."""
    profile_root = tmp_path / "profiles"
    _write_profile(profile_root, "alt")
    config_dir = tmp_path / "custom-config"
    _write_yaml(
        config_dir / "profiles" / "alt" / "review.yaml",
        {"schema_version": 1, "limits": {"max_suggestions": 5}},
    )

    monkeypatch.delenv("REVIEW_CONFIG_DIR", raising=False)
    monkeypatch.setattr(
        config_mod, "_current",
        dataclasses.replace(current(), review_config_dir=str(config_dir)),
    )
    load_profile("alt", root=profile_root)

    assert current().improve_max_suggestions == 5


def test_weekly_timezone_invalid_fails_fast(tmp_path):
    cfg = WeeklyReportConfig(
        timezone="Not/A_Zone",
        collectors=(),
        target_project_id=0,
        target_branch="main",
    )

    with pytest.raises(ValueError, match="timezone"):
        run_weekly_job(cfg=cfg, output_dir=tmp_path)


def test_weekly_timezone_shifts_week_bounds():
    moment = datetime(2026, 1, 4, 16, 30, tzinfo=timezone.utc)  # 周日 16:30 UTC

    shanghai_start, _ = _week_bounds(now=moment, tz=ZoneInfo("Asia/Shanghai"))
    new_york_start, _ = _week_bounds(now=moment, tz=ZoneInfo("America/New_York"))

    assert shanghai_start.date().isoformat() == "2026-01-05"  # 已是周一
    assert new_york_start.date().isoformat() == "2025-12-29"  # 仍是周日


def test_weekly_cron_schedule_renders_oncalendar(tmp_path):
    """services_ops 的 render 子命令输出即 systemd drop-in 内容."""
    repo_root = Path(__file__).resolve().parents[1]
    env = {
        **os.environ,
        "PROJECT_DIR": str(repo_root),
        "REVIEWAGENT_ENV_FILE": "/dev/null",
        "REVIEWAGENT_WEEKLY_CRON_SCHEDULE": "Tue 09:15",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }

    proc = subprocess.run(
        ["bash", "scripts/services_ops.sh", "render-weekly-timer"],
        cwd=repo_root, env=env, capture_output=True, text=True, timeout=30,
    )

    assert proc.returncode == 0, proc.stderr
    assert "OnCalendar=Tue 09:15" in proc.stdout
