"""构造 GitLab 测试 MR — 8-bugs fresh fixture 2026-08-09.

基于当前 main (含 fix 23af720) 创建新分支, 8-bug fixture 提交一个 commit,
无 LLM 提示. root 用户提交.
"""
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewagent.gitlab.client import client as gl

PROJECT_ID = 34
BRANCH = "codex/8bugs-fresh-2026-08-09-verify"
TARGET_BRANCH = "main"

MAIN_FILE = "services/verify_8bugs_2026_08_09.py"
MAIN_CODE = '''"""Post-deploy verify module — 8-bug fixture 2026-08-09.

4 categories x 2 each:
  1) 违背 agents 规则:
     - SSD-RULE-NO-BARE-PRINT
     - SSD-RULE-DOCSTRING-REQUIRED
  2) 通用规则:
     - SSD-RULE-NO-LOG-EXC
     - SSD-RULE-NO-MUTABLE-DEFAULT
  3) other 规则:
     - R-OTHER:magic_number
     - R-OTHER:resource_leak
  4) 跨文件规则 (在 caller 文件里):
     - R-OTHER-IMPACT:caller_param
     - R-OTHER-IMPACT:import_path
"""
from __future__ import annotations

import logging
import time
import sqlite3

logger = logging.getLogger(__name__)


def start_logging():
    # Bug #1 — agents 规则: SSD-RULE-NO-BARE-PRINT (production path 不能 print)
    print("logging started")


def fetch_records(table):
    # Bug #2 — agents 规则: SSD-RULE-DOCSTRING-REQUIRED (def 之后必须 docstring)
    cursor = sqlite3.connect(":memory:").cursor()
    return cursor.execute(f"SELECT * FROM {{table}}").fetchall()


def safe_read(path):
    # Bug #3 — 通用: SSD-RULE-NO-LOG-EXC (except 必须 log)
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception:
        pass


def merge_buffers(left=[], right=[]):
    # Bug #4 — 通用: SSD-RULE-NO-MUTABLE-DEFAULT
    merged = left + right
    return merged


def poll_until_ready(check_fn, max_attempts=8):
    # Bug #5 — other: R-OTHER:magic_number
    attempt = 0
    while attempt < max_attempts:
        if check_fn():
            return True
        time.sleep(0.123)
        attempt += 1
    return False


def save_report(rows, target):
    # Bug #6 — other: R-OTHER:resource_leak (open 没用 with)
    f = open(target, "w", encoding="utf-8")
    f.write("\\n".join(rows))
    f.close()
    return len(rows)
'''

CALLER_FILE = "services/verify_8bugs_caller_2026_08_09.py"
CALLER_CODE = '''"""Caller for verify_8bugs_2026_08_09.

跨文件 bug:
  - Bug #7 R-OTHER-IMPACT:caller_param (新接口要求 2 个参数)
  - Bug #8 R-OTHER-IMPACT:import_path (caller 用错 import path)
"""
from __future__ import annotations

from services.verify_8bugs_2026_08_09 import (
    fetch_records,
    save_report,
    start_logging,
)


def bootstrap(env: str) -> int:
    start_logging()
    rows = fetch_records(f"events_{{env}}")
    if not rows:
        return 0
    return save_report([str(r) for r in rows], f"/tmp/{{env}}.log")
'''


def main():
    project = gl._get_project(PROJECT_ID)

    try:
        project.branches.get(BRANCH)
        project.branches.delete(BRANCH)
        print(f"[1] deleted existing branch {BRANCH}")
        time.sleep(1)
    except Exception as e:
        print(f"[1] branch not exist (skip): {e}")

    project.branches.create({"branch": BRANCH, "ref": TARGET_BRANCH})
    print(f"[2] created branch {BRANCH} from {TARGET_BRANCH}")

    actions = [
        {"action": "create", "file_path": MAIN_FILE, "content": MAIN_CODE},
        {"action": "create", "file_path": CALLER_FILE, "content": CALLER_CODE},
    ]
    project.commits.create({
        "branch": BRANCH,
        "commit_message": "feat(services): verify 8-bugs fixture 2026-08-09",
        "actions": actions,
    })
    print(f"[3] committed 2 files to {BRANCH}")

    mr = project.mergerequests.create({
        "source_branch": BRANCH,
        "target_branch": TARGET_BRANCH,
        "title": "test: 8-bug review fixture 2026-08-09",
        "description": "8 个故意埋入的 bug 分布在 4 类. Verify MR.",
        "remove_source_branch": False,
    })
    print(f"[4] created MR !{mr.iid}: {mr.web_url}")


if __name__ == "__main__":
    main()
