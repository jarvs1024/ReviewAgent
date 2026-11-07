"""负向 fixture：settings/** 之外直接读环境变量（配置边界守卫必须拦截）."""
from __future__ import annotations

import os

OVERLAY_PATH = os.environ.get("REVIEWAGENT_OVERLAY_PATH", "")
EXTRA_SUFFIX = os.getenv("REVIEWAGENT_EXTRA_SUFFIX")

__all__ = ["EXTRA_SUFFIX", "OVERLAY_PATH"]
