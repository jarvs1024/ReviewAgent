"""日志 — loguru 统一封装."""
from __future__ import annotations

import sys

from loguru import logger

from reviewagent.config import config


def setup_logging() -> None:
    """全局日志配置；幂等."""
    logger.remove()
    logger.add(
        sys.stderr,
        level=config.log_level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
               "<level>{level: <8}</level> | "
               "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
               "<level>{message}</level>",
        backtrace=False,
        diagnose=False,
    )


__all__ = ["logger", "setup_logging"]