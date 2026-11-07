"""L0 配置层：单一 schema（默认值唯一来源）+ env binder + 运行时 config 代理.

import 顺序约定：`schema` 是纯数据，不 import 上层模块；`binder.from_env()`
在函数内部才 import `reviewagent.config`，因此本包可以安全地被 config 反向引用。
"""
from __future__ import annotations

from reviewagent.settings.binder import (
    coerce,
    explicit_env,
    fingerprint,
    from_env,
)
from reviewagent.settings.schema import SCHEMA, SCHEMA_BY_NAME, FieldSpec

__all__ = [
    "SCHEMA",
    "SCHEMA_BY_NAME",
    "FieldSpec",
    "coerce",
    "explicit_env",
    "fingerprint",
    "from_env",
]
