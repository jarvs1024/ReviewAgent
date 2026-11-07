"""worktree 路径约束：把仓库内相对路径解析为 worktree 根目录内的真实路径.

安全不变量（spec §7.6 第 2 条）：所有源码读取必须限定在本次 worktree 根目录内。
拒绝的三类输入：

1. 绝对路径（``/etc/passwd``）；
2. 任一 ``..`` 分量（即使最终仍落在根目录内，见上条 spec「`..` 越界一律拒绝」）；
3. 解析真实路径后落在根目录之外的符号链接（``link.py -> /etc/passwd``）。

空字符串按「空路径」单独拒绝（``ValueError``）；``"."`` 不拒绝。

其余情况返回 ``Path.resolve(strict=True)`` 的结果：文件缺失自然抛
``FileNotFoundError``（调用侧静默降级），循环符号链接抛 ``OSError``。
"""
from __future__ import annotations

from pathlib import Path

__all__ = ["resolve_within"]


def resolve_within(root: Path, rel: str) -> Path:
    """解析 ``root/rel`` 并校验结果仍在 ``root`` 内.

    Raises:
        ValueError: 空路径、绝对路径、含 ``..`` 分量或解析后越出 ``root``。
        FileNotFoundError: 目标不存在（``resolve(strict=True)`` 的自然行为）。
        OSError: 其它解析失败（如循环符号链接 ELOOP）。
    """
    if not str(rel).strip():
        # 空串会退化为 root 本身（"."），按「空路径」显式拒绝；"." 本身不拒绝.
        raise ValueError("empty path rejected")
    candidate = Path(rel)
    if candidate.is_absolute():
        raise ValueError(f"absolute path rejected: {rel}")
    if any(part == ".." for part in candidate.parts):
        raise ValueError(f"parent traversal rejected: {rel}")
    root_resolved = Path(root).resolve()
    target = (root_resolved / candidate).resolve(strict=True)
    if target != root_resolved and root_resolved not in target.parents:
        raise ValueError(f"path escapes worktree: {rel}")
    return target
