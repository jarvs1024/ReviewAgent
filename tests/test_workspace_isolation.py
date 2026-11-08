"""worktree 隔离回归：同 MR/同 SHA 的并发 workspace 不得互相删除.

背景：worktree 路径只由 project/mr/tag/sha 决定，链锁一旦丢失（E2E 清锁、
TTL 到期），两个并发链会共用同一路径，后建者 rmtree 掉前者，前者清理时又
删掉后者，导致 agent 读到 "worktree 不存在".
"""
from __future__ import annotations

import os
import subprocess
import time
from types import SimpleNamespace

import pytest

from reviewagent.git import workspace as ws_mod
from reviewagent.git.workspace import cleanup_workspace, prepare_workspace


@pytest.fixture
def git_env(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(["git", "init", "-q", str(src)], check=True)
    (src / "core.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(src), "add", "."], check=True)
    subprocess.run(
        [
            "git", "-C", str(src),
            "-c", "user.name=t", "-c", "user.email=t@example.com",
            "commit", "-qm", "init",
        ],
        check=True,
    )
    repos = tmp_path / "repos"
    repos.mkdir()
    bare = repos / "3.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
    sha = subprocess.check_output(
        ["git", "-C", str(bare), "rev-parse", "HEAD"], text=True
    ).strip()

    worktrees = tmp_path / "worktrees"
    monkeypatch.setattr(
        ws_mod, "config",
        SimpleNamespace(repos_dir=repos, worktrees_dir=worktrees),
    )
    monkeypatch.setattr(ws_mod, "_fetch_incremental", lambda *a, **k: None)
    return bare, sha, worktrees


def _prepare(bare, sha, tag: str = "describe"):
    return prepare_workspace(
        project_id=3,
        mr_iid=3,
        source_sha=sha,
        diff_text="diff --git a/core.py b/core.py\n",
        git_url=str(bare),
        tag=tag,
    )


def test_same_mr_sha_workspaces_are_isolated(git_env):
    bare, sha, _ = git_env
    ws1 = _prepare(bare, sha)
    sentinel = ws1.worktree / "sentinel.txt"
    sentinel.write_text("alive", encoding="utf-8")

    ws2 = _prepare(bare, sha)

    assert ws1.worktree != ws2.worktree, "同一 MR/SHA 的并发 workspace 必须使用不同路径"
    assert sentinel.exists(), "创建第二个 workspace 不得删除第一个的工作区"
    assert (ws2.worktree / "core.py").exists()

    cleanup_workspace(ws1)

    assert not ws1.worktree.exists()
    assert (ws2.worktree / "core.py").exists(), "清理一个 workspace 不得影响另一个"


def test_prepare_workspace_sweeps_stale_orphan_dirs(git_env):
    bare, sha, worktrees = git_env
    worktrees.mkdir(parents=True, exist_ok=True)
    stale = worktrees / "review-9-9-describe-abcdef0"
    fresh = worktrees / "review-9-9-improve-1234567"
    stale.mkdir()
    fresh.mkdir()
    old = time.time() - 7 * 3600
    os.utime(stale, (old, old))

    _prepare(bare, sha)

    assert not stale.exists(), "超过 6h 的孤儿 worktree 应在下次创建时清理"
    assert fresh.exists(), "新鲜目录不得被清理"
