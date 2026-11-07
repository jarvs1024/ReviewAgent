"""负向 fixture：一次越界 import（守卫必须拦截）.

该文件故意同时违反四条方向边界，供 tests/architecture 的负向测试使用；
它不会被生产代码导入，也不参与打包。
"""
from __future__ import annotations

from reviewagent.commands.improve import ImproveCommand
from reviewagent.review.policies.python import extract_identifiers
from reviewagent.scenario.profile import load_profile

__all__ = ["ImproveCommand", "extract_identifiers", "load_profile"]
