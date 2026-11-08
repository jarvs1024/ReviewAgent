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


def find_open_suggestion(mr_iid: int, log: list[str], *, pid: int | None = None) -> dict | None:
    """找一个 open suggestion（优先指定 MR，其次同项目任意 MR）."""
    from reviewagent.telemetry.store import get_store

    target = pid if pid is not None else project_id()
    store = get_store()
    with store._conn() as conn:
        row = conn.execute(
            "SELECT project_id, mr_iid, note_id, file_path, target_line, head_sha "
            "FROM suggestions WHERE project_id=? AND mr_iid=? AND state='open' "
            "ORDER BY created_at DESC LIMIT 1",
            (target, mr_iid),
        ).fetchone()
        if not row:
            row = conn.execute(
                "SELECT project_id, mr_iid, note_id, file_path, target_line, head_sha "
                "FROM suggestions WHERE project_id=? AND state='open' "
                "ORDER BY created_at DESC LIMIT 1",
                (target,),
            ).fetchone()
    if not row:
        log.append(f"[{now_iso()}] WARN: no open suggestion anywhere in telemetry")
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


def reset_mr_review_runs(mr_iid: int, log: list[str], *, pid: int | None = None) -> None:
    """清该 MR 的 review_runs，让 max_review_calls 闸门重新放行."""
    from reviewagent.telemetry.store import get_store

    target = pid if pid is not None else project_id()
    store = get_store()
    with store._conn() as conn:
        n = conn.execute(
            "DELETE FROM review_runs WHERE project_id=? AND mr_iid=?",
            (target, mr_iid),
        ).rowcount
        conn.commit()
    log.append(f"[{now_iso()}] reset review_runs for mr {mr_iid} (rows={n})")


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
    "app/extra.py": (
        "import os\n\n\n"
        "def unused_helper(value=None, options={}):\n"
        "    return options\n"
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
    return GitlabFixture(
        project_id=pid,
        source_branch=source,
        target_branch="main",
        mr_iid=merge_request,
    )
