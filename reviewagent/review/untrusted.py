"""不可信内容规范化 / 围栏自适应（brief 声明的导入位置）.

实现放在中立模块 :mod:`reviewagent.security`（只依赖标准库）：`scenario` 不得
import `review`，而 `scenario/rules/repo.py` 同样需要这两个函数。本模块只做
重导出，调用方在 `review` 命名空间下仍可 ``from reviewagent.review.untrusted
import normalize_untrusted, fence_for``。
"""
from __future__ import annotations

from reviewagent.security import fence_for, normalize_untrusted

__all__ = ["fence_for", "normalize_untrusted"]
