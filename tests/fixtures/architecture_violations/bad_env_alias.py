"""负向 fixture：os 别名读取环境变量（配置边界守卫必须拦截）.

`import os as _o` / `from os import environ as env` / `getattr(_o, "environ")`
都等价于直接读环境变量，不得成为绕过 allowlist 的出口。
"""
from __future__ import annotations

import os as _o
from os import environ as _env

OVERLAY_PATH = _o.environ.get("REVIEWAGENT_OVERLAY_PATH", "")
LEGACY_SUFFIX = getattr(_o, "environ", {}).get("REVIEWAGENT_LEGACY_SUFFIX")
IMPORTED_ENV = _env.get("REVIEWAGENT_IMPORTED_ENV")

__all__ = ["IMPORTED_ENV", "LEGACY_SUFFIX", "OVERLAY_PATH"]
