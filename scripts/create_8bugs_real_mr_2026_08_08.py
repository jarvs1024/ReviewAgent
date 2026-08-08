"""create_8bugs_real_mr_2026_08_08.py — 让 #7 #8 成真 bug 的 fixture."""
from pathlib import Path
import os, sys, time
import requests

# OAuth password grant 拿 root 身份
oauth = requests.post(
    "http://127.0.0.1:8929/oauth/token",
    data={"grant_type": "password", "username": "root", "password": "Jarvs@2026"},
    timeout=10,
).json()
bearer = f"Bearer {oauth['access_token']}"
pat = requests.post(
    "http://127.0.0.1:8929/api/v4/users/1/personal_access_tokens",
    headers={"Authorization": bearer},
    json={"name": "test-mr-realbugs-2026-08-08", "scopes": ["api"], "expires_at": "2026-08-15"},
    timeout=10,
).json()
os.environ["GITLAB_PERSONAL_ACCESS_TOKEN"] = pat["token"]

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from reviewagent.gitlab.client import client as gl

PROJECT_ID = 34
BRANCH = "codex/8bugs-real-cross-file-2026-08-08"
TARGET_BRANCH = "main"

# main 移到 services/audit/ 子目录
MAIN_FILE = "services/audit/verify_8bugs_real_2026_08_08.py"
MAIN_CODE = '''"""verify_8bugs_real — moved to services/audit/ subdir on 2026-08-08.

跨文件 fixture (root-authored):
  Bug #1 SSD-RULE-NO-BARE-PRINT    (start_logging 裸 print)
  Bug #2 SSD-RULE-DOCSTRING-REQUIRED (fetch_records 缺 docstring)
  Bug #3 SSD-RULE-NO-LOG-EXC       (safe_read 吞异常)
  Bug #4 SSD-RULE-NO-MUTABLE-DEFAULT (merge_buffers 默认 list)
  Bug #5 R-OTHER:magic_number      (poll_until_ready 0.123)
  Bug #6 SSD-RULE-RESOURCE-CONTEXT-MANAGER (save_report 裸 open)
  Bug #7 R-OTHER-IMPACT:caller_param  — fetch_records 现需要 (table, conn), caller 没传
  Bug #8 R-OTHER-IMPACT:import_path   — main 已搬 services/audit/, caller 仍 import 旧路径
"""
from __future__ import annotations

import logging
import time
import sqlite3

logger = logging.getLogger(__name__)


def start_logging():
    # Bug #1
    print("logging started")  # noqa: T201


def fetch_records(table, conn):
    # Bug #2 + Bug #7
    cursor = conn.cursor()
    return cursor.execute(f"SELECT * FROM {table}").fetchall()


def safe_read(path):
    # Bug #3
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception:  # noqa: BLE001
        pass


def merge_buffers(left=[], right=[]):
    # Bug #4
    merged = left + right
    return merged


def poll_until_ready(check_fn, max_attempts=8):
    # Bug #5
    attempt = 0
    while attempt < max_attempts:
        if check_fn():
            return True
        time.sleep(0.123)
        attempt += 1
    return False


def save_report(rows, target):
    # Bug #6
    f = open(target, "w", encoding="utf-8")
    f.write("\\n".join(rows))
    f.close()
    return len(rows)
'''

CALLER_FILE = "services/verify_8bugs_real_caller_2026_08_08.py"
CALLER_CODE = '''"""Caller for verify_8bugs_real — root-authored cross-file fixture.

注: 故意 import 旧路径 services.verify_8bugs_real_2026_08_08,
实际 main 已搬到 services/audit/ — import 会失败 (Bug #8 真 bug).
fetch_records 也只传 table 缺 conn (Bug #7 真 bug).
"""
from __future__ import annotations

# Bug #8 — R-OTHER-IMPACT:import_path
from services.verify_8bugs_real_2026_08_08 import (
    fetch_records,
    save_report,
    start_logging,
)


def bootstrap(env: str) -> int:
    start_logging()
    # Bug #7 — R-OTHER-IMPACT:caller_param
    rows = fetch_records(f"events_{env}")
    if not rows:
        return 0
    return save_report([str(r) for r in rows], f"/tmp/{env}.log")
'''

def main():
    project = gl._get_project(PROJECT_ID)
    try:
        project.branches.get(BRANCH)
        project.branches.delete(BRANCH)
        print(f"[1] deleted existing branch {BRANCH}")
    except Exception:
        print(f"[1] branch {BRANCH} not exist")
    time.sleep(1)
    project.branches.create({"branch": BRANCH, "ref": TARGET_BRANCH})
    print(f"[2] created branch {BRANCH}")
    project.commits.create({
        "branch": BRANCH,
        "commit_message": "test(review): 8-bug fixture (real #7 #8 cross-file, root-authored)",
        "actions": [
            {"action": "create", "file_path": MAIN_FILE, "content": MAIN_CODE},
            {"action": "create", "file_path": CALLER_FILE, "content": CALLER_CODE},
        ],
    })
    print(f"[3] committed {MAIN_FILE} + {CALLER_FILE}")
    mr = project.mergerequests.create({
        "source_branch": BRANCH,
        "target_branch": TARGET_BRANCH,
        "title": "test: 8-bug fixture with REAL cross-file bugs (2026-08-08)",
        "description": (
            "**Real bugs** (#7 #8 是真 bug, 不同 #244):\n\n"
            "  - Bug #8 真: main 在 `services/audit/`, caller 仍 import 旧路径 — 加载失败\n"
            "  - Bug #7 真: `fetch_records(table, conn)` 要求 conn, caller 只传 table — TypeError\n\n"
            "期望: 全部 8 处 + SSD-AGENTS-MARKER"
        ),
        "remove_source_branch": False,
    })
    print(f"[4] created MR !{mr.iid}: {mr.web_url}")

if __name__ == "__main__":
    main()
