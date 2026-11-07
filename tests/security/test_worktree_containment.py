"""worktree 路径约束：``resolve_within`` 单测 + ImproveCommand 四个读取器集成.

安全不变量（spec §7.6 第 2 条）：所有 worktree 源码读取必须限定在 worktree 根目录
内；绝对路径、任一 ``..`` 分量与符号链接逃逸一律拒绝（WARNING 日志），文件缺失 /
解析失败静默降级为既有的「读不到」返回值（``[]`` / ``-1`` / ``None``）。
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from reviewagent.review.paths import resolve_within


# ---------- resolve_within ----------
def test_rejects_absolute_and_parent(tmp_path):
    with pytest.raises(ValueError):
        resolve_within(tmp_path, "/etc/passwd")
    with pytest.raises(ValueError):
        resolve_within(tmp_path, "../outside.py")


def test_rejects_nested_parent_component(tmp_path):
    """即使最终仍落在 root 内，含 ``..`` 分量也一律拒绝."""
    (tmp_path / "a").mkdir()
    (tmp_path / "x.py").write_text("x = 1\n")
    with pytest.raises(ValueError):
        resolve_within(tmp_path, "a/../../x")


def test_rejects_symlink_escape(tmp_path):
    outside = tmp_path.parent / "outside.py"
    outside.write_text("secret")
    (tmp_path / "link.py").symlink_to(outside)
    with pytest.raises(ValueError):
        resolve_within(tmp_path, "link.py")


def test_accepts_nested_file_and_returns_resolved_path(tmp_path):
    nested = tmp_path / "pkg" / "mod.py"
    nested.parent.mkdir()
    nested.write_text("x = 1\n")
    got = resolve_within(tmp_path, "pkg/mod.py")
    assert got == nested.resolve()
    assert got.is_file()


def test_accepts_symlink_inside_root(tmp_path):
    target = tmp_path / "real.py"
    target.write_text("x = 1\n")
    (tmp_path / "alias.py").symlink_to(target)
    assert resolve_within(tmp_path, "alias.py") == target.resolve()


def test_missing_file_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        resolve_within(tmp_path, "nope.py")


# ---------- ImproveCommand 门面集成 ----------
def _cmd(worktree: Path):
    from reviewagent.commands.improve import ImproveCommand

    cmd = object.__new__(ImproveCommand)
    cmd.ws = SimpleNamespace(worktree=worktree)
    return cmd


def _assert_all_rejected(cmd, rel: str) -> None:
    assert cmd._read_file_lines(rel) == []
    assert cmd._read_file_line_count(rel, cmd.ws) == -1
    assert cmd._stat_file_bytes(rel) == -1
    assert cmd._file_blob_sha(rel, cmd.ws) is None


def test_facade_rejects_absolute_path(tmp_path):
    secret = tmp_path.parent / "secret.py"
    secret.write_text("s1\ns2\n", encoding="utf-8")
    _assert_all_rejected(_cmd(tmp_path), str(secret))


def test_facade_rejects_parent_traversal(tmp_path):
    outside = tmp_path.parent / "traversed.py"
    outside.write_text("s1\n", encoding="utf-8")
    _assert_all_rejected(_cmd(tmp_path), "../traversed.py")


def test_facade_rejects_symlink_escape(tmp_path):
    outside = tmp_path.parent / "escaped.py"
    outside.write_text("secret\n", encoding="utf-8")
    (tmp_path / "link.py").symlink_to(outside)
    _assert_all_rejected(_cmd(tmp_path), "link.py")


def test_facade_keeps_legacy_behaviour_for_regular_files(tmp_path):
    nested = tmp_path / "pkg" / "mod.py"
    nested.parent.mkdir()
    payload = "import os\nx = 1\n"
    nested.write_text(payload, encoding="utf-8")
    cmd = _cmd(tmp_path)

    assert cmd._read_file_lines("pkg/mod.py") == ["import os", "x = 1"]
    assert cmd._read_file_line_count("pkg/mod.py", cmd.ws) == 2
    assert cmd._stat_file_bytes("pkg/mod.py") == len(payload.encode())
    assert cmd._file_blob_sha("pkg/mod.py", cmd.ws) == hashlib.sha1(
        nested.read_bytes()
    ).hexdigest()


def test_facade_degrades_silently_for_missing_file(tmp_path):
    _assert_all_rejected(_cmd(tmp_path), "missing.py")
