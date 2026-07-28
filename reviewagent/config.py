"""业务配置 — 一个文件，dataclass + 环境变量.

启动时 Config.from_env() 一次性加载，启动后 frozen 不可变.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(key: str, default: str | None = None, required: bool = False) -> str:
    """读取环境变量；缺失且 required 则抛错."""
    val = os.environ.get(key, default)
    if required and not val:
        raise RuntimeError(f"missing required env var: {key}")
    return val or ""


@dataclass(frozen=True)
class Config:
    # ---- GitLab ----
    gitlab_url: str
    gitlab_pat: str
    gitlab_webhook_secret: str
    gitlab_bot_username: str = "review-agent"

    # ---- opencode ----
    opencode_url: str = "http://localhost:4096"
    opencode_username: str = "opencode"
    opencode_password: str = ""
    opencode_model: str = "minimax/MiniMax-M2.7"

    # ---- Redis / RQ ----
    redis_url: str = "redis://localhost:6379/0"
    rq_queue_name: str = "review"
    rq_worker_timeout: int = 600  # 单任务超时（秒）

    # ---- 存储 ----
    data_dir: Path = field(default_factory=lambda: Path("./data"))
    log_level: str = "INFO"

    # ---- 限制 ----
    mr_cooldown_seconds: int = 30
    max_review_calls_per_mr: int = 0  # 0 = 不限；>0 强制上限

    # ---------- 派生路径 ----------
    @property
    def sqlite_path(self) -> Path:
        return self.data_dir / "telemetry.db"

    @property
    def repos_dir(self) -> Path:
        """bare repos 持久化目录（多 MR 共用 git 对象）."""
        return self.data_dir / "repos"

    @property
    def worktrees_dir(self) -> Path:
        """git worktree 临时目录（每次任务临时，应放 tmpfs）."""
        return Path("/tmp/reviewagent-worktrees")

    @property
    def weekly_reports_dir(self) -> Path:
        return self.data_dir / "weekly_reports"

    # ---------- 工厂 ----------
    @classmethod
    def from_env(cls) -> "Config":
        cfg = cls(
            gitlab_url=_env("GITLAB_URL", required=True),
            gitlab_pat=_env("GITLAB_PERSONAL_ACCESS_TOKEN", required=True),
            gitlab_webhook_secret=_env("GITLAB_WEBHOOK_SECRET", required=True),
            gitlab_bot_username=_env("GITLAB_BOT_USERNAME", "review-agent"),
            opencode_url=_env("OPENCODE_URL", "http://localhost:4096"),
            opencode_username=_env("OPENCODE_USERNAME", "opencode"),
            opencode_password=_env("OPENCODE_PASSWORD", ""),
            opencode_model=_env("OPENCODE_MODEL", "minimax/MiniMax-M2.7"),
            redis_url=_env("REDIS_URL", "redis://localhost:6379/0"),
            rq_queue_name=_env("RQ_QUEUE_NAME", "review"),
            rq_worker_timeout=int(_env("RQ_WORKER_TIMEOUT", "600")),
            data_dir=Path(_env("REVIEWAGENT_DATA_DIR", "./data")),
            log_level=_env("REVIEWAGENT_LOG_LEVEL", "INFO"),
            mr_cooldown_seconds=int(_env("MR_COOLDOWN_SECONDS", "30")),
            max_review_calls_per_mr=int(_env("MAX_REVIEW_CALLS_PER_MR", "0")),
        )
        # 确保目录存在
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        cfg.repos_dir.mkdir(parents=True, exist_ok=True)
        cfg.worktrees_dir.mkdir(parents=True, exist_ok=True)
        cfg.weekly_reports_dir.mkdir(parents=True, exist_ok=True)
        return cfg


# 全局单例；启动时初始化
config = Config.from_env()
