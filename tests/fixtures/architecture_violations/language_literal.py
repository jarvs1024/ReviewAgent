"""负向 fixture：模块 docstring 里的 .py / import ast 必须被忽略。

docstring 中出现 `import ast` 与 `endswith(".py")` 只是文档说明，
守卫只扫描可执行代码（import 节点与字符串常量）。
"""
from __future__ import annotations

# 注释里出现 -t py 与 if language == "python": 也必须被忽略。
DEFAULT_FILE_TYPE = "-t py"
SOURCE_SUFFIX = ".py"


def language_branch(language: str) -> bool:
    """函数 docstring 同样忽略：`.py` / `import ast` 只是描述."""
    return language == "python"
