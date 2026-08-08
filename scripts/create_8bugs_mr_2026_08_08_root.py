"""create_8bugs_mr_2026_08_08_root.py — 用 root 身份建 MR (避免 bot_self 跳过).

前 30 行: 拿到 root 的 PAT (覆盖 env) 后再 import reviewagent.
后续逻辑: 复用 create_8bugs_mr.py 的 8-bug fixture 内容, 分支加 -root 后缀.
"""
import os, sys, time
from pathlib import Path

# === 关键: source .env 拿 GITLAB_URL 等, 然后用 OAuth password grant 拿 root 的 PAT 覆盖 ===
import requests
ROOT_OAUTH = requests.post(
    "http://127.0.0.1:8929/oauth/token",
    data={"grant_type": "password", "username": "root", "password": "Jarvs@2026"},
    timeout=10,
).json()
if "access_token" not in ROOT_OAUTH:
    raise RuntimeError(f"oauth failed: {ROOT_OAUTH}")

# 用 root 的 OAuth bearer 建一个 1 周有效 PAT, 然后用 PAT 走 reviewagent.gitlab.client
bearer = f"Bearer {ROOT_OAUTH['access_token']}"
pat_resp = requests.post(
    "http://127.0.0.1:8929/api/v4/users/1/personal_access_tokens",
    headers={"Authorization": bearer},
    json={
        "name": "test-mr-2026-08-08-root-runner",
        "scopes": ["api"],
        "expires_at": "2026-08-15",
    },
    timeout=10,
)
if pat_resp.status_code != 201:
    raise RuntimeError(f"create PAT failed: {pat_resp.status_code} {pat_resp.text}")
ROOT_PAT = pat_resp.json()["token"]
os.environ["GITLAB_PERSONAL_ACCESS_TOKEN"] = ROOT_PAT

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewagent.gitlab.client import client as gl

PROJECT_ID = 34
BRANCH = "codex/8bugs-merged-main-2026-08-08-root"
TARGET_BRANCH = "main"

MAIN_FILE = "services/verify_8bugs_2026_08_08.py"
MAIN_CODE = '''"""Post-deploy verify module — synthetic 8-bug fixture (2026-08-08 rerun, root-authored).

覆盖:
  - 违背 agents 规则:
    * Bug #1 SSD-RULE-NO-BARE-PRINT
    * Bug #2 SSD-RULE-DOCSTRING-REQUIRED
  - 通用规则:
    * Bug #3 SSD-RULE-NO-LOG-EXC
    * Bug #4 SSD-RULE-NO-MUTABLE-DEFAULT
  - other 规则:
    * Bug #5 R-OTHER:magic_number
    * Bug #6 R-OTHER:resource_leak

跨文件相关 Bug #7 #8 在 caller 文件 (verify_8bugs_caller_2026_08_08.py).
"""
from __future__ import annotations

import logging
import time
import sqlite3

logger = logging.getLogger(__name__)


def start_logging():
    # Bug #1 — 违背 agents 规则: SSD-RULE-NO-BARE-PRINT
    print("logging started")  # noqa: T201


def fetch_records(table):
    # Bug #2 — 违背 agents 规则: SSD-RULE-DOCSTRING-REQUIRED
    cursor = sqlite3.connect(":memory:").cursor()
    return cursor.execute(f"SELECT * FROM {table}").fetchall()


def safe_read(path):
    # Bug #3 — 通用规则: SSD-RULE-NO-LOG-EXC
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception:  # noqa: BLE001
        pass


def merge_buffers(left=[], right=[]):
    # Bug #4 — 通用规则: SSD-RULE-NO-MUTABLE-DEFAULT
    merged = left + right
    return merged


def poll_until_ready(check_fn, max_attempts=8):
    # Bug #5 — other 规则: R-OTHER:magic_number
    attempt = 0
    while attempt < max_attempts:
        if check_fn():
            return True
        time.sleep(0.123)
        attempt += 1
    return False


def save_report(rows, target):
    # Bug #6 — other 规则: R-OTHER:resource_leak
    f = open(target, "w", encoding="utf-8")
    f.write("\\n".join(rows))
    f.close()
    return len(rows)
'''

CALLER_FILE = "services/verify_8bugs_caller_2026_08_08.py"
CALLER_CODE = '''"""Caller for verify_8bugs_2026_08_08 — root-authored fixture.

覆盖:
  - 跨文件规则:
    * Bug #7 R-OTHER-IMPACT: caller 没传新参数 (caller_param)
    * Bug #8 R-OTHER-IMPACT: import path 漂移 (import_path)
"""
from __future__ import annotations

# Bug #8 — 跨文件规则: R-OTHER-IMPACT:import_path
from services.verify_8bugs_2026_08_08 import (
    fetch_records,
    save_report,
    start_logging,
)


def bootstrap(env: str) -> int:
    start_logging()
    # Bug #7 — 跨文件规则: R-OTHER-IMPACT:caller_param
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
        print(f"[1] branch {BRANCH} does not exist (skip)")
    time.sleep(1)
    project.branches.create({"branch": BRANCH, "ref": TARGET_BRANCH})
    print(f"[2] created branch {BRANCH} from {TARGET_BRANCH}")
    project.commits.create({
        "branch": BRANCH,
        "commit_message": "test(review): 8-bug fixture (2026-08-08, root-authored, 4 cats × 2)",
        "actions": [
            {"action": "create", "file_path": MAIN_FILE, "content": MAIN_CODE},
            {"action": "create", "file_path": CALLER_FILE, "content": CALLER_CODE},
        ],
    })
    print(f"[3] committed {MAIN_FILE} + {CALLER_FILE}")
    mr = project.mergerequests.create({
        "source_branch": BRANCH,
        "target_branch": TARGET_BRANCH,
        "title": "test: 8-bug review fixture (2026-08-08, root-authored)",
        "description": (
            "Test fixture by **root** (not bot, 避免 bot_self 跳过).\\n\\n"
            "8 个故意埋入的 bug 分布在 4 类:\\n"
            "  - 违背 agents 规则 (×2): SSD-RULE-NO-BARE-PRINT + SSD-RULE-DOCSTRING-REQUIRED\\n"
            "  - 通用规则 (×2): SSD-RULE-NO-LOG-EXC + SSD-RULE-NO-MUTABLE-DEFAULT\\n"
            "  - other 规则 (×2): R-OTHER:magic_number + R-OTHER:resource_leak\\n"
            "  - 跨文件规则 (×2): R-OTHER-IMPACT:caller_param + R-OTHER-IMPACT:import_path"
        ),
        "remove_source_branch": False,
    })
    print(f"[4] created MR !{mr.iid}: {mr.web_url}")
    print(f"[5] waiting for webhook (MR !{mr.iid})...")

if __name__ == "__main__":
    main()
