"""Git workspace 管理 — bare repo + worktree + 代码污染防护.

设计:
    1. 每个项目一个 bare repo（持久化在 ./data/repos/{project_id}.git/）
       - 多 MR 共用 git 对象，节省 clone 时间
       - 只含 git 对象，不含 working tree，安全可控
    2. 每个 MR 一个 worktree（tmpfs /tmp/reviewagent-worktrees/...）
       - 任务结束 rm -rf，无持久化
       - agent 在 worktree 内读项目代码 + diff
    3. diff 文件写在 worktree 内的 .diff.patch（任务结束随 rm 清空）

工作流:
    ws = prepare_workspace(project_id, mr_iid, source_sha, diff_text, gitlab_url, pat)
        ... opencode.run(workdir=ws.worktree, files=[ws.diff_file]) ...
    cleanup_workspace(ws)
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from reviewagent.config import config
from reviewagent.logging_setup import logger


# ---------- 自定义异常 ----------
class WorkspaceError(RuntimeError):
    pass


# ---------- 数据结构 ----------
@dataclass
class Workspace:
    """MR 检视工作区.

    Attributes:
        bare: bare repo 路径（持久化，多 MR 共用）
        worktree: 当前 MR 的 working tree 路径（tmpfs，临时）
        diff_file: 当前 MR 的 diff 文件路径（在 worktree 内，临时）
    """
    bare: Path
    worktree: Path
    diff_file: Path


# ---------- bare repo 操作 ----------
def _bare_path(project_id: int) -> Path:
    return config.repos_dir / f"{project_id}.git"


def _ensure_bare(project_id: int, git_url: str) -> Path:
    """确保项目的 bare repo 存在；不存在则 git clone --bare.

    Args:
        git_url: 已带 token 的完整 git URL（由 GitLabClient.get_project_git_url 提供）
    """
    bare = _bare_path(project_id)
    if bare.exists():
        return bare

    bare.parent.mkdir(parents=True, exist_ok=True)
    safe_url = _scrub_token(git_url)
    logger.info("git.clone_bare start project={} url={}", project_id, safe_url)

    proc = subprocess.run(
        ["git", "clone", "--bare", "--depth=1", git_url, str(bare)],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        raise WorkspaceError(f"git clone --bare failed: {proc.stderr.strip()[:500]}")

    # 安全收紧权限（仅当前用户可读写）
    try:
        bare.chmod(0o700)
        for sub in bare.rglob("*"):
            if sub.is_file():
                sub.chmod(0o600)
    except OSError as e:
        logger.warning("git.chmod failed (non-fatal): {}", e)

    logger.info("git.clone_bare ok project={} path={}", project_id, bare)
    return bare


def _fetch_incremental(bare: Path, git_url: str) -> None:
    """增量 fetch — 让 bare repo 包含 source_sha 所需对象."""
    proc = subprocess.run(
        ["git", "-C", str(bare), "fetch", "--depth=1", git_url, "+refs/heads/*:refs/heads/*"],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        logger.warning("git.fetch failed (will try full clone): {}", proc.stderr.strip()[:300])


# ---------- worktree 操作 ----------
def _worktree_path(project_id: int, mr_iid: int, sha: str) -> Path:
    short = sha[:7] if len(sha) >= 7 else sha
    return config.worktrees_dir / f"review-{project_id}-{mr_iid}-{short}"


def _create_worktree(bare: Path, project_id: int, mr_iid: int, sha: str) -> Path:
    wt = _worktree_path(project_id, mr_iid, sha)

    # 若已存在（同名残留），先清理
    if wt.exists() or wt.is_symlink():
        shutil.rmtree(wt, ignore_errors=True)
        subprocess.run(
            ["git", "-C", str(bare), "worktree", "prune"],
            capture_output=True, timeout=30,
        )

    config.worktrees_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["git", "-C", str(bare), "worktree", "add", "--detach", str(wt), sha],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise WorkspaceError(
            f"git worktree add failed (sha={sha[:7]}): {proc.stderr.strip()[:500]}"
        )

    logger.info("git.worktree_add ok path={}", wt)
    return wt


# ---------- diff 文件 ----------
def _write_diff(worktree: Path, diff_text: str) -> Path:
    """在 worktree 内写 diff 文件（随 worktree 一同清理）."""
    diff_path = worktree / ".diff.patch"
    diff_path.write_text(diff_text, encoding="utf-8")
    try:
        diff_path.chmod(0o600)
    except OSError:
        pass
    return diff_path


# ---------- 主入口 ----------
def prepare_workspace(
    *,
    project_id: int,
    mr_iid: int,
    source_sha: str,
    diff_text: str,
    git_url: str,
) -> Workspace:
    """为一次 MR 检视准备：bare repo + worktree + diff 文件.

    Args:
        project_id: GitLab 项目 ID
        mr_iid: MR IID
        source_sha: MR source branch HEAD commit SHA
        diff_text: 完整 diff 文本（来自 GitLab API）
        git_url: 已带 token 的完整 git URL（由 GitLabClient.get_project_git_url 提供）

    Returns:
        Workspace 对象，包含 bare / worktree / diff_file 三个路径

    Raises:
        WorkspaceError: bare clone 或 worktree 创建失败
    """
    bare = _ensure_bare(project_id, git_url)
    _fetch_incremental(bare, git_url)
    worktree = _create_worktree(bare, project_id, mr_iid, source_sha)
    diff_file = _write_diff(worktree, diff_text)
    return Workspace(bare=bare, worktree=worktree, diff_file=diff_file)


# ---------- 工具 ----------
def _scrub_token(url: str) -> str:
    """从 git URL 中移除 token（用于日志）. """
    if "@" not in url:
        return url
    scheme_token, rest = url.split("@", 1)
    # scheme_token 形如 "https://oauth2:glpat-xxx"
    if "://" in scheme_token:
        scheme = scheme_token.split("://", 1)[0]
        return f"{scheme}://***@{rest}"
    return f"***@{rest}"


def cleanup_workspace(ws: Workspace) -> None:
    """清理：删除 worktree + prune；bare repo 保留（多 MR 共用）."""
    if ws.worktree.exists():
        shutil.rmtree(ws.worktree, ignore_errors=True)
        logger.info("git.worktree_rm ok path={}", ws.worktree)

    try:
        subprocess.run(
            ["git", "-C", str(ws.bare), "worktree", "prune"],
            capture_output=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        logger.warning("git.worktree_prune timeout")