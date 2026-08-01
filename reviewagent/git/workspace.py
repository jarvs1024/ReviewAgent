"""Git workspace 管理 — bare repo + worktree + 代码污染防护.

设计:
    1. 每个项目一个 bare repo（持久化在 ./data/repos/{project_id}.git/）
       - 多 MR 共用 git 对象，节省 clone 时间
       - 文件锁 (fcntl.flock) 防多 worker 同时初始化
    2. 每个 MR 一个 worktree（tmpfs /tmp/reviewagent-worktrees/...）
       - worktree 名含 tag（describe/review/improve）防止同 MR 的命令冲突
       - 任务结束 rm -rf，无持久化
    3. diff 文件写在 worktree 内的 .diff.patch（任务结束随 rm 清空）

工作流:
    ws = prepare_workspace(project_id, mr_iid, source_sha, diff_text, git_url, tag="describe")
        ... opencode.run(workdir=ws.worktree, files=[ws.diff_file]) ...
    cleanup_workspace(ws)
"""
from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from reviewagent.config import config
from reviewagent.logging_setup import logger


# ---------- 锁（防多 worker 竞态）----------
def _lock_bare(bare: Path) -> int:
    lock_path = Path(str(bare) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except Exception:
        os.close(fd)
        raise
    return fd


def _unlock_bare(fd: int) -> None:
    os.close(fd)


# ---------- 自定义异常 ----------
class WorkspaceError(RuntimeError):
    pass


# ---------- 数据结构 ----------
@dataclass
class Workspace:
    bare: Path
    worktree: Path
    diff_file: Path


# ---------- bare repo 操作 ----------
def _bare_path(project_id: int) -> Path:
    return config.repos_dir / f"{project_id}.git"


def _ensure_bare(project_id: int, git_url: str) -> Path:
    bare = _bare_path(project_id)
    if bare.exists():
        return bare

    lock_fd = _lock_bare(bare)
    try:
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
        try:
            bare.chmod(0o700)
            for sub in bare.rglob("*"):
                if sub.is_file():
                    sub.chmod(0o600)
        except OSError as e:
            logger.warning("git.chmod failed (non-fatal): {}", e)
        logger.info("git.clone_bare ok project={} path={}", project_id, bare)
        return bare
    finally:
        _unlock_bare(lock_fd)


def _fetch_incremental(bare: Path, git_url: str, target_sha: str | None = None) -> None:
    """增量 fetch bare repo — shallow 优先, 失败则 full fetch fallback.

    Refspec 用 refs/remotes/origin/* 而非 refs/heads/*:
    bare repo 上若有遗留 worktree 把某些 branch checked out 了,
    "+refs/heads/*:refs/heads/*" 会因为 "refusing to fetch into checked-out branch"
    整批失败, 导致新 branch 拉不到, 进而 worktree add 报 "invalid reference".
    用 remote-tracking refspec 只更新 refs/remotes/origin/*, 不碰本地 branch,
    永远不会冲突. _create_worktree 只需要 commit SHA, object db 里有就能 worktree add.

    Args:
        target_sha: 若指定, 确保该 SHA 在 object db (force-push 后新 commit 可能
                    还没被 refspec 拉到). 用 `git fetch origin <sha>` 单独拉.
                    修复 issue: MR update head_sha 变化太快, branch refspec 还没
                    拉到 force-push 后的新 commit 就 worktree add, 报 "invalid reference".
    """
    # 先 prune 掉残留的 worktree 引用, 防止 stale ref 卡住后续命令
    subprocess.run(
        ["git", "-C", str(bare), "worktree", "prune"],
        capture_output=True, text=True, timeout=30,
    )
    proc = subprocess.run(
        ["git", "-C", str(bare), "fetch", "--depth=1", git_url,
         "+refs/heads/*:refs/remotes/origin/*"],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        logger.warning("git.fetch shallow failed, trying full: {}", proc.stderr.strip()[:300])
        # Fallback: full fetch (unshallow)
        proc = subprocess.run(
            ["git", "-C", str(bare), "fetch", git_url, "+refs/heads/*:refs/remotes/origin/*"],
            capture_output=True, text=True, timeout=180,
        )
        if proc.returncode != 0:
            logger.warning(
                "git.fetch full also failed (worktree may fail): {}", proc.stderr.strip()[:300]
            )

    # 单独 fetch target_sha (force-push 后 branch refspec 还没拉到的情况)
    if target_sha:
        _fetch_sha(bare, git_url, target_sha)


def _fetch_sha(bare: Path, git_url: str, sha: str) -> None:
    """单独 fetch 一个 commit SHA, 确保它在 object db (无 ancestor).

    Why: 之前 +refs/heads/*:refs/remotes/origin/* 只能拉 branch HEAD 处的 commit.
    force-push 后, GitLab 报给 webhook 的 head_sha 可能是新 commit, 但本地 branch
    ref 还没更新. 单独 fetch 该 SHA (depth=1) 只需该 commit object (worktree add
    不需要祖先).
    """
    # 先看是否已有
    check = subprocess.run(
        ["git", "-C", str(bare), "cat-file", "-t", sha],
        capture_output=True, text=True, timeout=10,
    )
    if check.returncode == 0:
        return  # already in object db
    # 单独 fetch
    proc = subprocess.run(
        ["git", "-C", str(bare), "fetch", "--depth=1", git_url, sha],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode == 0:
        logger.info("git.fetch_sha ok bare={} sha={}", bare.name, sha[:7])
    else:
        logger.warning(
            "git.fetch_sha failed bare={} sha={}: {}",
            bare.name, sha[:7], proc.stderr.strip()[:200],
        )


# ---------- worktree 操作 ----------
def _worktree_path(project_id: int, mr_iid: int, sha: str, tag: str = "wt") -> Path:
    short = sha[:7] if len(sha) >= 7 else sha
    return config.worktrees_dir / f"review-{project_id}-{mr_iid}-{tag}-{short}"


def _create_worktree(bare: Path, project_id: int, mr_iid: int, sha: str, tag: str = "wt") -> Path:
    wt = _worktree_path(project_id, mr_iid, sha, tag)
    if wt.exists() or wt.is_symlink():
        shutil.rmtree(wt, ignore_errors=True)
        subprocess.run(["git", "-C", str(bare), "worktree", "prune"], capture_output=True, timeout=30)
    config.worktrees_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["git", "-C", str(bare), "worktree", "add", "--detach", str(wt), sha],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise WorkspaceError(f"git worktree add failed (sha={sha[:7]}): {proc.stderr.strip()[:500]}")
    logger.info("git.worktree_add ok path={}", wt)
    return wt


# ---------- diff 文件 ----------
def _write_diff(worktree: Path, diff_text: str) -> Path:
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
    tag: str = "wt",
) -> Workspace:
    bare = _ensure_bare(project_id, git_url)
    _fetch_incremental(bare, git_url, target_sha=source_sha)  # 确保 source_sha 在 object db
    worktree = _create_worktree(bare, project_id, mr_iid, source_sha, tag)
    diff_file = _write_diff(worktree, diff_text)
    return Workspace(bare=bare, worktree=worktree, diff_file=diff_file)


def cleanup_workspace(ws: Workspace) -> None:
    if ws.worktree.exists():
        shutil.rmtree(ws.worktree, ignore_errors=True)
        logger.info("git.worktree_rm ok path={}", ws.worktree)
    try:
        subprocess.run(["git", "-C", str(ws.bare), "worktree", "prune"], capture_output=True, timeout=30)
    except subprocess.TimeoutExpired:
        logger.warning("git.worktree_prune timeout")


# ---------- 工具 ----------
def _scrub_token(url: str) -> str:
    if "@" not in url:
        return url
    scheme_token, rest = url.split("@", 1)
    if "://" in scheme_token:
        scheme = scheme_token.split("://", 1)[0]
        return f"{scheme}://***@{rest}"
    return f"***@{rest}"