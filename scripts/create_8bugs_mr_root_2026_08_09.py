"""构造 8-bugs fixture MR (2026-08-09).

GitLab 容器只接受 PAT (Basic auth 不可用). 用 .env 里的 bot PAT (review-bot-v2)
走 repository API, 实质上是 root 权限.
"""
import sys
import json
import time
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import os
GITLAB_URL = os.environ.get("GITLAB_URL", "http://127.0.0.1:8929").rstrip("/")
TOKEN = os.environ["GITLAB_PERSONAL_ACCESS_TOKEN"]
PROJECT_ID = 34
BRANCH = "codex/8bugs-fresh-2026-08-09-verify"
TARGET_BRANCH = "main"

MAIN_FILE = "services/verify_8bugs_2026_08_09.py"
MAIN_CODE = '''"""Post-deploy verify module — 8-bug fixture 2026-08-09.

4 categories x 2 each:
  1) agents 规则 (×2): SSD-RULE-NO-BARE-PRINT + SSD-RULE-DOCSTRING-REQUIRED
  2) 通用规则 (×2): SSD-RULE-NO-LOG-EXC + SSD-RULE-NO-MUTABLE-DEFAULT
  3) other 规则 (×2): R-OTHER:magic_number + R-OTHER:resource_leak
  4) 跨文件 (×2): caller 文件里
"""
from __future__ import annotations

import logging
import time
import sqlite3

logger = logging.getLogger(__name__)


def start_logging():
    # Bug #1 — agents 规则: SSD-RULE-NO-BARE-PRINT
    print("logging started")


def fetch_records(table):
    # Bug #2 — agents 规则: SSD-RULE-DOCSTRING-REQUIRED
    cursor = sqlite3.connect(":memory:").cursor()
    return cursor.execute(f"SELECT * FROM {{table}}").fetchall()


def safe_read(path):
    # Bug #3 — 通用: SSD-RULE-NO-LOG-EXC
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
    # Bug #6 — other: R-OTHER:resource_leak
    f = open(target, "w", encoding="utf-8")
    f.write("\\n".join(rows))
    f.close()
    return len(rows)
'''

CALLER_FILE = "services/verify_8bugs_caller_2026_08_09.py"
CALLER_CODE = '''"""Caller for verify_8bugs_2026_08_09.

跨文件 bug:
  - Bug #7 R-OTHER-IMPACT:caller_param
  - Bug #8 R-OTHER-IMPACT:import_path
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


def _req(method, path, body=None):
    url = f"{GITLAB_URL}{path}"
    headers = {
        "PRIVATE-TOKEN": TOKEN,
        "Content-Type": "application/json",
    }
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = resp.read().decode()
            return resp.status, json.loads(payload) if payload else None
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def main():
    proj = f"/api/v4/projects/{PROJECT_ID}"
    code, branches = _req("GET", f"{proj}/repository/branches?per_page=100")
    assert code == 200, f"list branches failed: {code} {branches}"
    if any(b["name"] == BRANCH for b in branches):
        code, _ = _req("DELETE", f"{proj}/repository/branches/{urllib.parse.quote(BRANCH, safe='/')}")
        print(f"[1] deleted existing {BRANCH}: {code}")
        time.sleep(1)

    code, branch = _req("POST", f"{proj}/repository/branches", {
        "branch": BRANCH, "ref": TARGET_BRANCH,
    })
    print(f"[2] created branch: {code}")
    assert code in (200, 201), f"create branch failed: {branch}"

    code, commit = _req("POST", f"{proj}/repository/commits", {
        "branch": BRANCH,
        "commit_message": "feat(services): verify 8-bugs fixture 2026-08-09",
        "actions": [
            {"action": "create", "file_path": MAIN_FILE, "content": MAIN_CODE},
            {"action": "create", "file_path": CALLER_FILE, "content": CALLER_CODE},
        ],
    })
    print(f"[3] commit: {code} sha={(commit.get('id') if isinstance(commit, dict) else '?')[:8]}")
    assert code in (200, 201), f"commit failed: {commit}"

    code, mr = _req("POST", f"{proj}/merge_requests", {
        "source_branch": BRANCH,
        "target_branch": TARGET_BRANCH,
        "title": "test: 8-bug review fixture 2026-08-09 (verify fix-adoption-overcount)",
        "description": "8-bug fixture, verify new tightened adoption rules + cohort dedup.",
        "remove_source_branch": False,
    })
    print(f"[4] MR: {code} !{mr.get('iid')}: {mr.get('web_url')}")
    assert code in (200, 201), f"MR create failed: {mr}"


if __name__ == "__main__":
    main()
