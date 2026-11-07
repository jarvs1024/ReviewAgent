"""L0 配置层：单一 schema（默认值唯一来源）+ env binder + 运行时 config 代理.

import 顺序约定：`schema` 是纯数据，不 import 上层模块；`binder.from_env()`
在函数内部才 import `reviewagent.config`，因此本包可以安全地被 config 反向引用。

公共名约定：`UNSET` / `validate` / `coerce_scenario` 等供 `reviewagent.config`
等上层模块引用，避免跨模块 import 私有名（`_UNSET` / `_validate`）。
"""
from __future__ import annotations

from reviewagent.settings.binder import (
    UNSET,
    coerce,
    coerce_scenario,
    explicit_env,
    fingerprint,
    from_env,
    validate,
)
from reviewagent.settings.schema import SCHEMA, SCHEMA_BY_NAME, FieldSpec, profiles_root

__all__ = [
    "SCHEMA",
    "SCHEMA_BY_NAME",
    "FieldSpec",
    "UNSET",
    "coerce",
    "coerce_scenario",
    "explicit_env",
    "fingerprint",
    "from_env",
    "profiles_root",
    "validate",
]
