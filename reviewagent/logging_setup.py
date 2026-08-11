"""日志 — loguru 统一封装."""
from __future__ import annotations

import sys

from loguru import logger

from reviewagent.config import config

_LOG_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
    "<level>{message}</level>"
)


def setup_logging() -> None:
    """全局日志配置；幂等.

    输出目标:
    - stderr (console, 供 journalctl / 开发调试)
    - {data_dir}/log/reviewagent.log (文件, 自动轮转 + 压缩归档)
    """
    logger.remove()

    # 1) stderr — 保持原有格式 (带颜色, 方便终端查看)
    logger.add(
        sys.stderr,
        level=config.log_level,
        format=_LOG_FORMAT,
        backtrace=False,
        diagnose=False,
    )

    # 2) 文件日志 — 轮转 50MB, 保留 30 天, 旧文件 gzip 压缩
    log_dir = config.data_dir / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.add(
        str(log_dir / "reviewagent.log"),
        level=config.log_level,
        format=_LOG_FORMAT,
        rotation="50 MB",
        retention="30 days",
        compression="gz",
        encoding="utf-8",
        enqueue=True,        # 多进程安全 (worker fork 后不丢日志)
        backtrace=True,      # 文件日志保留 traceback, 方便排查
        diagnose=True,
    )


__all__ = ["logger", "setup_logging"]