"""业务配置 — 一个文件，dataclass + 环境变量.

启动时 Config.from_env() 一次性加载，启动后 frozen 不可变.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(key: str, default: str | None = None, required: bool = False) -> str:
    """读取环境变量；缺失且 required 则抛错. 自动 strip 首尾空白."""
    val = os.environ.get(key, default)
    if required and not val:
        raise RuntimeError(f"missing required env var: {key}")
    return (val or "").strip()


def _env_tuple(key: str, default: str) -> tuple[str, ...]:
    """读逗号分隔的 tuple（如 'describe,improve'）."""
    raw = os.environ.get(key, default)
    return tuple(s.strip() for s in raw.split(",") if s.strip())


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
    rq_worker_count: int = 3  # 并发 worker 数

    # ---- 命令链（每个 MR 按顺序串行执行）----
    pr_commands: tuple[str, ...] = ("describe", "improve")
    push_commands: tuple[str, ...] = ("describe", "improve")

    # ---- 存储 ----
    data_dir: Path = field(default_factory=lambda: Path("./data"))
    log_level: str = "INFO"

    # ---- 限制 ----
    mr_cooldown_seconds: int = 30
    max_review_calls_per_mr: int = 0  # 0 = 不限；>0 强制上限

    # ---- Diff 限制 ----
    max_diff_chars: int = 50000              # 超过则跳过检视并评论告知
    opencode_max_diff_chars: int = 20000     # opencode prompt 内联 diff 截断阈值

    # ---- AGENTS.md 仓库规则 ----
    repo_context_files: tuple[str, ...] = ("AGENTS.md",)  # 从仓库默认分支读取的规则文件
    repo_context_rules_dir: str = ".agents/rules"  # 规则目录 (自动读取其下所有 .md)
    rule_key_prefix: str = "SSD"             # 规则键前缀 (如 SSD-RULE-NO-LOG-EXC)
    repo_context_max_lines: int = 2000       # 规则文件最大行数 (超出截断)

    # ---- improve 并行 + 限流 ----
    improve_parallel_workers: int = 3        # 按文件分块并行调 opencode 的路数
    improve_max_files: int = 10              # 单次检视最大文件数 (0=不限, 超出跳过)
    improve_max_suggestions: int = 15        # 单次最大 inline 建议数 (0=不限, 超出只写总览)
    improve_min_score: int = 0               # 改进建议最低分数 (0=不过滤, 建议值 20~40)

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
            rq_worker_count=int(_env("RQ_WORKER_COUNT", "3")),
            pr_commands=_env_tuple("PR_COMMANDS", "describe,improve"),
            push_commands=_env_tuple("PUSH_COMMANDS", "describe,improve"),
            data_dir=Path(_env("REVIEWAGENT_DATA_DIR", "./data")),
            log_level=_env("REVIEWAGENT_LOG_LEVEL", "INFO"),
            mr_cooldown_seconds=int(_env("MR_COOLDOWN_SECONDS", "30")),
            max_review_calls_per_mr=int(_env("MAX_REVIEW_CALLS_PER_MR", "0")),
            max_diff_chars=int(_env("MAX_DIFF_CHARS", "50000")),
            opencode_max_diff_chars=int(_env("OPENCODE_MAX_DIFF_CHARS", "20000")),
            repo_context_files=_env_tuple("REPO_CONTEXT_FILES", "AGENTS.md"),
            repo_context_rules_dir=_env("REPO_CONTEXT_RULES_DIR", ".agents/rules"),
            rule_key_prefix=_env("RULE_KEY_PREFIX", "SSD"),
            repo_context_max_lines=int(_env("REPO_CONTEXT_MAX_LINES", "2000")),
            improve_parallel_workers=int(_env("IMPROVE_PARALLEL_WORKERS", "3")),
            improve_max_files=int(_env("IMPROVE_MAX_FILES", "10")),
            improve_max_suggestions=int(_env("IMPROVE_MAX_SUGGESTIONS", "15")),
            improve_min_score=int(_env("IMPROVE_MIN_SCORE", "0")),
        )
        # 确保目录存在
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        cfg.repos_dir.mkdir(parents=True, exist_ok=True)
        cfg.worktrees_dir.mkdir(parents=True, exist_ok=True)
        cfg.weekly_reports_dir.mkdir(parents=True, exist_ok=True)
        return cfg


# 全局单例；启动时初始化
config = Config.from_env()
