"""构造 GitLab 测试 MR — 8-bugs review-test (merged main 部署版).

8 个 bug 分 4 类 (每类 2 个):
  1) 违背 agents 规则 (2 个 SSD-RULE-* 违规)
  2) 跨文件规则       (2 个 R-OTHER-IMPACT 违规)
  3) 通用规则         (2 个 R-XXX 违规)
  4) other 规则       (2 个 R-OTHER:* 违规)

文件结构:
  services/verify_8bugs_2026_08_05.py         (主文件, 6 个内联 bug)
  services/verify_8bugs_caller_2026_08_05.py  (caller 文件, 2 个跨文件 bug)
"""
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewagent.gitlab.client import client as gl

PROJECT_ID = 34
BRANCH = "codex/8bugs-merged-main-2026-08-08"
TARGET_BRANCH = "main"

MAIN_FILE = "services/verify_8bugs_2026_08_08.py"
MAIN_CODE = '''"""Post-deploy verify module — synthetic 8-bug fixture.

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

跨文件相关 Bug #7 #8 在 caller 文件 (verify_8bugs_caller_2026_08_05.py).
"""
from __future__ import annotations

import logging
import time
import sqlite3

logger = logging.getLogger(__name__)


def start_logging():
    # Bug #1 — 违背 agents 规则: SSD-RULE-NO-BARE-PRINT
    # production path 不能用 print, 必须 logging.
    print("logging started")  # noqa: T201


def fetch_records(table):
    # Bug #2 — 违背 agents 规则: SSD-RULE-DOCSTRING-REQUIRED
    # 每个 def 都要有 docstring.
    cursor = sqlite3.connect(":memory:").cursor()
    return cursor.execute(f"SELECT * FROM {table}").fetchall()


def safe_read(path):
    # Bug #3 — 通用规则: SSD-RULE-NO-LOG-EXC
    # except 必须 log, 而不是悄悄 pass.
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception:  # noqa: BLE001
        pass


def merge_buffers(left=[], right=[]):
    # Bug #4 — 通用规则: SSD-RULE-NO-MUTABLE-DEFAULT
    # mutable default argument 是常见踩坑, 跨次调用共享同一对象.
    merged = left + right
    return merged


def poll_until_ready(check_fn, max_attempts=8):
    # Bug #5 — other 规则: R-OTHER:magic_number
    # 硬编码 0.123 — 应提到模块常量.
    attempt = 0
    while attempt < max_attempts:
        if check_fn():
            return True
        time.sleep(0.123)
        attempt += 1
    return False


def save_report(rows, target):
    # Bug #6 — other 规则: R-OTHER:resource_leak
    # f = open(...) 没 with 包裹, 异常路径会泄漏文件句柄.
    f = open(target, "w", encoding="utf-8")
    f.write("\\n".join(rows))
    f.close()
    return len(rows)
'''

CALLER_FILE = "services/verify_8bugs_caller_2026_08_08.py"
CALLER_CODE = '''"""Caller for verify_8bugs_2026_08_05.

覆盖:
  - 跨文件规则:
    * Bug #7 R-OTHER-IMPACT: caller 没传新参数 (caller_param)
    * Bug #8 R-OTHER-IMPACT: import path 漂移 (import_path)
"""
from __future__ import annotations

# Bug #8 — 跨文件规则: R-OTHER-IMPACT:import_path
# verify_8bugs_2026_08_08 已经搬到 services/audit/ 子目录,
# 但 caller 还在用旧路径 services.verify_8bugs_2026_08_05 — module 找不到.
from services.verify_8bugs_2026_08_08 import (
    fetch_records,
    save_report,
    start_logging,
)


def bootstrap(env: str) -> int:
    start_logging()  # 调用方 — 同步使用 main 模块
    # Bug #7 — 跨文件规则: R-OTHER-IMPACT:caller_param
    # 新版 fetch_records(table, conn) 要求两个参数, caller 还在用旧的
    # 只传 table 的写法, 缺 conn 参数 — 实际跑会 TypeError.
    rows = fetch_records(f"events_{env}")
    if not rows:
        return 0
    return save_report([str(r) for r in rows], f"/tmp/{env}.log")
'''


def main():
    project = gl._get_project(PROJECT_ID)

    # 1. 删除旧分支 (如果存在)
    try:
        project.branches.get(BRANCH)
        project.branches.delete(BRANCH)
        print(f"[1] deleted existing branch {BRANCH}")
        time.sleep(1)
    except Exception as e:
        print(f"[1] branch {BRANCH} does not exist (skip delete): {e}")

    # 2. 创建新分支
    project.branches.create({"branch": BRANCH, "ref": TARGET_BRANCH})
    print(f"[2] created branch {BRANCH} from {TARGET_BRANCH}")

    # 3. 提交两个文件到新分支 (同一个 commit)
    actions = [
        {"action": "create", "file_path": MAIN_FILE, "content": MAIN_CODE},
        {"action": "create", "file_path": CALLER_FILE, "content": CALLER_CODE},
    ]
    project.commits.create({
        "branch": BRANCH,
        "commit_message":
            "test(review): 8-bug fixture for merged-main deploy (4 categories × 2 each)",
        "actions": actions,
    })
    print(f"[3] committed {MAIN_FILE} + {CALLER_FILE} to {BRANCH}")

    # 4. 创建 MR
    mr = project.mergerequests.create({
        "source_branch": BRANCH,
        "target_branch": TARGET_BRANCH,
        "title": "test: 8-bug review fixture (post-merge-main deploy, 2026-08-08 rerun)",
        "description": (
            "Test fixture for merged-main deploy verification.\n\n"
            "8 个故意埋入的 bug 分布在 4 类:\n"
            "  - 违背 agents 规则 (×2): SSD-RULE-NO-BARE-PRINT + SSD-RULE-DOCSTRING-REQUIRED\n"
            "  - 通用规则 (×2): SSD-RULE-NO-LOG-EXC + SSD-RULE-NO-MUTABLE-DEFAULT\n"
            "  - other 规则 (×2): R-OTHER:magic_number + R-OTHER:resource_leak\n"
            "  - 跨文件规则 (×2): R-OTHER-IMPACT:caller_param + R-OTHER-IMPACT:import_path\n\n"
            "期望 review 输出命中全部 8 处 + SSD-AGENTS-MARKER 标记."
        ),
        "remove_source_branch": False,
    })
    print(f"[4] created MR !{mr.iid}: {mr.web_url}")
    print(f"[5] waiting for webhook to trigger review on MR !{mr.iid}...")


if __name__ == "__main__":
    main()
