"""E2E 夹具：GitLab 临时项目/分支/MR 与 telemetry 状态准备.

夹具只负责「把环境摆到可验证状态」，断言在 `flows` / `config_audit`。
"""
from __future__ import annotations

import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

from scripts.e2e.stack import (
    gitlab_request,
    now_iso,
    project_id,
    telemetry_get,
)

# ---------------------------------------------------------------------------
# telemetry 状态
# ---------------------------------------------------------------------------


def find_open_suggestion(
    mr_iid: int, log: list[str], *, pid: int | None = None, timeout_s: int = 120
) -> dict | None:
    """找一个 open suggestion（优先指定 MR，其次同项目任意 MR）.

    suggestion 是 improve 收尾阶段异步落库的，紧随其后跑 adopt/dismiss 可能还看不到；
    这里短轮询等它出现，超时才判负。
    """
    from reviewagent.telemetry.store import get_store

    target = pid if pid is not None else project_id()
    store = get_store()
    deadline = time.time() + timeout_s
    row = None
    while time.time() < deadline:
        with store._conn() as conn:
            # 已有 action 的 note 不能再选：adopt 落库到 state 翻转之间有几秒窗口，
            # 不排除的话 dismiss 会挑到同一条 suggestion。
            row = conn.execute(
                "SELECT s.project_id, s.mr_iid, s.note_id, s.file_path, s.target_line, s.head_sha "
                "FROM suggestions s WHERE s.project_id=? AND s.mr_iid=? AND s.state='open' "
                "AND NOT EXISTS (SELECT 1 FROM suggestion_actions a "
                "  WHERE a.project_id=s.project_id AND a.mr_iid=s.mr_iid "
                "    AND a.suggestion_note_id=s.note_id) "
                "ORDER BY s.created_at DESC LIMIT 1",
                (target, mr_iid),
            ).fetchone()
            if not row:
                row = conn.execute(
                    "SELECT s.project_id, s.mr_iid, s.note_id, s.file_path, s.target_line, s.head_sha "
                    "FROM suggestions s WHERE s.project_id=? AND s.state='open' "
                    "AND NOT EXISTS (SELECT 1 FROM suggestion_actions a "
                    "  WHERE a.project_id=s.project_id AND a.mr_iid=s.mr_iid "
                    "    AND a.suggestion_note_id=s.note_id) "
                    "ORDER BY s.created_at DESC LIMIT 1",
                    (target,),
                ).fetchone()
        if row:
            break
        time.sleep(3)
    if not row:
        log.append(
            f"[{now_iso()}] WARN: no open suggestion in telemetry after {timeout_s}s"
        )
        return None
    suggestion = dict(row)
    log.append(
        f"[{now_iso()}] picked suggestion mr={suggestion['mr_iid']} "
        f"file={suggestion['file_path']} line={suggestion['target_line']}"
    )
    return suggestion


def reset_mr_describe_state(mr_iid: int, log: list[str], *, pid: int | None = None) -> None:
    """清 description_generated / last_review_at，让 /describe 的 already 守卫失效."""
    from reviewagent.telemetry.store import get_store

    target = pid if pid is not None else project_id()
    store = get_store()
    with store._conn() as conn:
        n = conn.execute(
            "UPDATE mr_activity SET description_generated=0, last_review_at=NULL "
            "WHERE project_id=? AND mr_iid=?",
            (target, mr_iid),
        ).rowcount
        conn.commit()
    log.append(f"[{now_iso()}] reset description_generated=0 for mr {mr_iid} (rows={n})")


def get_action_count(mr_iid: int, action: str, *, pid: int | None = None) -> int:
    from reviewagent.telemetry.store import get_store

    target = pid if pid is not None else project_id()
    store = get_store()
    with store._conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM suggestion_actions "
            "WHERE project_id=? AND mr_iid=? AND action=?",
            (target, mr_iid, action),
        ).fetchone()
    return int(row[0]) if row else 0


def get_dismissal_count(mr_iid: int, *, pid: int | None = None) -> int:
    target = pid if pid is not None else project_id()
    status, body = telemetry_get(f"/mr/{target}/{mr_iid}/dismissals?limit=500")
    if status == 200 and isinstance(body, dict):
        return len(body.get("dismissals", body.get("items", [])) or [])
    return 0


def wait_for_action(
    mr_iid: int,
    action: str,
    before: int,
    *,
    timeout_s: int = 300,
    pid: int | None = None,
) -> tuple[bool, int]:
    """等 suggestion_actions 里某动作计数增长（adopt/dismiss 是异步 job）."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        current = get_action_count(mr_iid, action, pid=pid)
        if current > before:
            return True, current
        time.sleep(3)
    return False, get_action_count(mr_iid, action, pid=pid)


# ---------------------------------------------------------------------------
# GitLab 临时夹具（全量档）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GitlabFixture:
    project_id: int
    source_branch: str
    target_branch: str
    mr_iid: int


def create_project(name: str, log: list[str]) -> int:
    data = gitlab_request(
        "POST",
        "/projects",
        payload={
            "name": name,
            "path": name.lower(),
            "visibility": "private",
            "initialize_with_readme": False,
        },
    )
    pid = int(data["id"])
    log.append(f"[{now_iso()}] created project {name} id={pid}")
    return pid


def commit_files(
    pid: int,
    branch: str,
    files: dict[str, str],
    message: str,
    log: list[str],
) -> None:
    """逐文件提交；文件已存在则走 PUT（GitLab files API 一次一个文件）."""
    for path, content in files.items():
        encoded = urllib.parse.quote(path, safe="")
        base = f"/projects/{pid}/repository/files/{encoded}"
        if _file_exists(pid, path, branch):
            gitlab_request("PUT", base, payload={"branch": branch, "content": content, "commit_message": message})
        else:
            gitlab_request("POST", base, payload={"branch": branch, "content": content, "commit_message": message})
    log.append(f"[{now_iso()}] committed {len(files)} files to {branch}")


def _file_exists(pid: int, path: str, ref: str) -> bool:
    encoded = urllib.parse.quote(path, safe="")
    try:
        gitlab_request("GET", f"/projects/{pid}/repository/files/{encoded}?ref={ref}")
    except RuntimeError:
        return False
    return True


def create_branch(pid: int, branch: str, ref: str, log: list[str]) -> None:
    gitlab_request(
        "POST",
        f"/projects/{pid}/repository/branches",
        payload={"branch": branch, "ref": ref},
    )
    log.append(f"[{now_iso()}] created branch {branch} from {ref}")


def open_merge_request(pid: int, source: str, target: str, title: str, log: list[str]) -> int:
    data = gitlab_request(
        "POST",
        f"/projects/{pid}/merge_requests",
        payload={"source_branch": source, "target_branch": target, "title": title},
    )
    mr_iid = int(data["iid"])
    log.append(f"[{now_iso()}] opened MR !{mr_iid} {source} -> {target}")
    return mr_iid


def wait_for_mr_diff(pid: int, mr_iid: int, log: list[str], *, timeout_s: int = 120) -> bool:
    """等 GitLab 算出 MR diff.

    刚创建的 MR，`/changes` 可能短暂返回空列表；此时发命令会得到
    「MR has no diff (empty or binary-only)」，因此夹具先就绪再继续。
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        data = gitlab_request("GET", f"/projects/{pid}/merge_requests/{mr_iid}/changes")
        changes = data.get("changes") if isinstance(data, dict) else None
        if changes:
            log.append(f"[{now_iso()}] MR !{mr_iid} diff ready ({len(changes)} files)")
            return True
        time.sleep(2)
    log.append(f"[{now_iso()}] WARN: MR !{mr_iid} diff still empty after {timeout_s}s")
    return False


def delete_project(pid: int, log: list[str]) -> None:
    gitlab_request("DELETE", f"/projects/{pid}")
    log.append(f"[{now_iso()}] deleted project {pid}")


def register_webhook(pid: int, url: str, secret: str, log: list[str]) -> int:
    """可选：把真实 webhook 挂到夹具项目（readiness 展示用；合成 webhook 不依赖它）."""
    data = gitlab_request(
        "POST",
        f"/projects/{pid}/hooks",
        payload={
            "url": url,
            "token": secret,
            "merge_requests_events": True,
            "note_events": True,
            "push_events": True,
        },
    )
    hook_id = int(data["id"])
    log.append(f"[{now_iso()}] registered webhook {hook_id} -> {url}")
    return hook_id


def project_webhooks(pid: int) -> list[dict[str, Any]]:
    data = gitlab_request("GET", f"/projects/{pid}/hooks")
    return list(data) if isinstance(data, list) else []


def mr_discussion_bodies(pid: int, mr_iid: int) -> list[str]:
    """取 MR 行内评论正文（DiffNote），用于验证规则键确实发布到了 GitLab."""
    discussions = gitlab_request(
        "GET", f"/projects/{pid}/merge_requests/{mr_iid}/discussions?per_page=100"
    )
    bodies: list[str] = []
    for discussion in discussions if isinstance(discussions, list) else []:
        for note in discussion.get("notes", []):
            if note.get("system"):
                continue
            if note.get("type") == "DiffNote" or note.get("position"):
                bodies.append(str(note.get("body") or ""))
    return bodies


# ---------------------------------------------------------------------------
# 真实数据场景（全量档）
# ---------------------------------------------------------------------------

_BASE_FILES: dict[str, str] = {
    "app/__init__.py": '"""E2E fixture package."""\n',
    "app/utils.py": 'def normalise(value):\n    return value.strip()\n',
    "app/service.py": (
        '"""Service layer."""\n\n'
        "from app import utils\n\n\n"
        "def load(items):\n"
        "    return [utils.normalise(item) for item in items]\n"
    ),
    "app/api/__init__.py": '"""API package."""\n',
    "app/api/handler.py": (
        '"""HTTP handler."""\n\n\n'
        "def handle(payload):\n"
        "    return payload\n"
    ),
    "app/core/__init__.py": '"""Core package."""\n',
    "app/core/engine.py": (
        '"""Engine."""\n\n\n'
        "def run(job):\n"
        "    return job\n"
    ),
    "app/models/__init__.py": '"""Models package."""\n',
    "app/models/item.py": (
        '"""Item model."""\n\n\n'
        "class Item:\n"
        "    def __init__(self, name):\n"
        "        self.name = name\n"
    ),
    "app/extra.py": (
        '"""Extra helpers."""\n\n\n'
        "def helper(value=None):\n"
        "    return value\n"
    ),
    # 与真实 SSD 仓库同构的库层路径：命中 review.yaml 的 priority_paths
    "testlib/lib_baw/e2e_helper.py": (
        '"""E2E priority helper."""\n\n\n'
        "def ping(disk):\n"
        "    return disk\n\n\n"
        "def collect(disk):\n"
        "    return [disk]\n\n\n"
        "def reset(disk):\n"
        "    return None\n"
    ),
    "conftest.py": (
        "import pytest\n\n\n"
        "@pytest.fixture\n"
        "def sample():\n"
        "    return [1, 2, 3]\n"
    ),
    "testcase/test_service.py": (
        "from app.service import load\n\n\n"
        "def test_load():\n"
        '    assert load([" a "]) == ["a"]\n'
    ),
    # 仓库规则目录：运行时按配置递归扫描（默认 .agents/rules），无需入口文件
    ".agents/rules/team.md": (
        "# Team rules\n\n"
        "Prefer explicit imports. E2E repo rule marker.\n\n"
        "| 规则键 | 严重度 | 摘要 |\n|---|---|---|\n"
        "| `SSD-RULE-E2E-IMPORT` | high | 禁止通配 import |\n"
    ),
    ".agents/rules/team/nested.md": (
        "# Nested rules\n\n"
        "E2E repo context marker: recursive scan reaches nested files.\n"
    ),
}

_CHANGE_FILES: dict[str, str] = {
    # 通配 import + 跨文件调用 + 裸 except + 可变默认参数 + 低价值噪声
    "app/service.py": (
        '"""Service layer."""\n\n'
        "from app import utils\n"
        "from app.utils import *\n\n\n"
        "def load(items):\n"
        "    return [utils.normalise(item) for item in items]\n\n\n"
        "def total(items):\n"
        "    result = 0\n"
        "    for item in items:\n"
        "        result += item\n"
        "    return result\n\n\n"
        "def save(items):\n"
        "    try:\n"
        '        with open("out.txt", "w") as handle:\n'
        "            handle.write(str(load(items)))\n"
        "    except:\n"
        "        pass\n\n\n"
        "def cache_value(key, cache={}):\n"
        "    return cache.get(key)\n"
    ),
    # 关键路径对照：命中 priority_paths 的 /testlib/lib_baw/（改动小于 extra.py）
    "testlib/lib_baw/e2e_helper.py": (
        '"""E2E priority helper."""\n\n'
        "from framework import logger\n\n\n"
        "def ping(disk):\n"
        '    logger.info("e2e helper ping")\n'
        "    return disk\n\n\n"
        "def collect(disk):\n"
        '    logger.info("e2e helper collect")\n'
        "    return [disk]\n\n\n"
        "def reset(disk):\n"
        '    logger.info("e2e helper reset")\n'
        "    return None\n"
    ),
    # 关键路径：/api/ 与 /core/ 与 /models/ 不命中当前场景包的 priority_paths
    "app/api/handler.py": (
        '"""HTTP handler."""\n\n\n'
        "def handle(payload):\n"
        "    if payload is None:\n"
        "        return {}\n"
        '    return {"value": payload, "echo": payload}\n'
    ),
    "app/core/engine.py": (
        '"""Engine."""\n\n\n'
        "def run(job, options={}):\n"
        "    if job is None:\n"
        "        return options\n"
        "    return job\n"
    ),
    "app/models/item.py": (
        '"""Item model."""\n\n\n'
        "class Item:\n"
        "    def __init__(self, name, tags=[]):\n"
        "        self.name = name\n"
        "        self.tags = tags\n"
    ),
    # 非关键路径的对照文件：diff 略大于 api/handler.py
    "app/extra.py": (
        '"""Extra helpers."""\n\n\n'
        "def unused_helper(value=None, options={}):\n"
        "    return options\n\n\n"
        "def echo(value):\n"
        "    return value\n"
    ),
    # 测试/配置路径：应被过滤或抽样，不应产生行内评论
    "testcase/test_service.py": (
        "from app.service import load, total\n\n\n"
        "def test_load():\n"
        '    assert load([" a "]) == ["a"]\n\n\n'
        "def test_total():\n"
        "    assert total([1, 2]) == 3\n"
    ),
    "conftest.py": (
        "import pytest\n\n\n"
        "@pytest.fixture\n"
        "def sample():\n"
        "    return [1, 2, 3]\n\n\n"
        "@pytest.fixture\n"
        "def other():\n"
        "    return {\"a\": 1}\n"
    ),
}


def create_review_fixture(round_idx: int, log: list[str], *, suffix: str = "") -> GitlabFixture:
    """建一个真实的临时项目 + 变更分支 + MR（每轮一个，结束即删）."""
    name = f"e2e-review-{suffix or 'run'}-r{round_idx}".lower()
    pid = create_project(name, log)
    commit_files(pid, "main", _BASE_FILES, "e2e: base", log)
    source = f"e2e/change-r{round_idx}"
    create_branch(pid, source, "main", log)
    commit_files(pid, source, _CHANGE_FILES, "e2e: change", log)
    merge_request = open_merge_request(pid, source, "main", f"E2E 真实变更 r{round_idx}", log)
    wait_for_mr_diff(pid, merge_request, log)
    return GitlabFixture(
        project_id=pid,
        source_branch=source,
        target_branch="main",
        mr_iid=merge_request,
    )


# ---------------------------------------------------------------------------
# 抽样分支（真实对比 API 驱动 test_sampling 断言）
# ---------------------------------------------------------------------------


def create_sampling_branch(
    pid: int,
    branch: str,
    count: int,
    log: list[str],
    *,
    base: str = "main",
    prefix: str = "testcase/sample",
) -> str:
    """单次 commits API 提交 count 个 testcase 文件，形成可 compare 的真实分支."""
    actions = [
        {
            "action": "create",
            "file_path": f"{prefix}_{idx:02d}.py",
            "content": "def test_sample():\n    assert True\n",
        }
        for idx in range(1, count + 1)
    ]
    gitlab_request(
        "POST",
        f"/projects/{pid}/repository/commits",
        payload={
            "branch": branch,
            "start_branch": base,
            "commit_message": f"e2e: {count} sample cases",
            "actions": actions,
        },
    )
    log.append(f"[{now_iso()}] created sampling branch {branch} with {count} files")
    return branch


def delete_branch(pid: int, branch: str, log: list[str]) -> None:
    gitlab_request(
        "DELETE",
        f"/projects/{pid}/repository/branches/{urllib.parse.quote(branch, safe='')}",
    )
    log.append(f"[{now_iso()}] deleted branch {branch}")
