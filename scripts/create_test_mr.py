"""构造 GitLab 测试 MR 触发检视流程.

创建测试分支 → 提交有问题的 Python 代码 → 创建 MR → 触发 webhook 检视.
"""
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewagent.gitlab.client import client as gl
from reviewagent.logging_setup import logger

PROJECT_ID = 34
BRANCH = "test-config-validation"
TARGET_BRANCH = "main"

# 有问题的测试代码 — 覆盖多条规则
BAD_CODE = '''"""Test file for config validation end-to-end check."""
import os
import json  # unused import
import sqlite3


def process_data(items=[]):
    # mutable default arg, no type hints, no docstring
    result = []
    for item in items:
        f = open("/tmp/data.txt")
        data = f.read()
        f.close()
        result.append(data)
    return result


def calculate_score(value):
    # no type hints, no docstring, bare except
    try:
        return value * 100 / len(value)
    except:
        pass


def fetch_user(conn, email):
    # SQL injection via f-string
    cursor = conn.cursor()
    query = f"SELECT * FROM users WHERE email = '{email}'"
    return cursor.execute(query).fetchone()


def run_task():
    # bare print, no return type
    print("task started")
    try:
        result = do_work()
    except:
        pass
    print("task done")
'''


def main():
    project = gl._get_project(PROJECT_ID)

    # 1. 删除旧分支（如果存在）
    try:
        project.branches.get(BRANCH)
        project.branches.delete(BRANCH)
        print(f"[1] deleted existing branch {BRANCH}")
        time.sleep(1)
    except Exception:
        print(f"[1] branch {BRANCH} does not exist, skip delete")

    # 2. 创建新分支
    project.branches.create({"branch": BRANCH, "ref": TARGET_BRANCH})
    print(f"[2] created branch {BRANCH} from {TARGET_BRANCH}")

    # 3. 提交有问题的代码
    file_path = "services/test_config_validation.py"
    project.files.create({
        "file_path": file_path,
        "branch": BRANCH,
        "content": BAD_CODE,
        "commit_message": "test: add config validation test file with known issues",
    })
    print(f"[3] committed {file_path} to {BRANCH}")

    # 4. 创建 MR
    mr = project.mergerequests.create({
        "source_branch": BRANCH,
        "target_branch": TARGET_BRANCH,
        "title": "test: config validation — trigger review with known issues",
        "description": "测试配置验证用 MR，包含可变默认参数、裸 except、SQL 注入、手动 open/close、缺类型注解等已知问题。",
        "remove_source_branch": False,
    })
    mr_iid = mr.iid
    mr_url = mr.web_url
    print(f"[4] created MR !{mr_iid}: {mr_url}")

    # 5. 等待 webhook 触发检视
    print(f"[5] waiting for webhook to trigger review on MR !{mr_iid}...")
    print(f"    webhook -> :3000 -> rq review-v2 -> opencode :4096")
    print(f"    check logs/worker-w1.log for processing")


if __name__ == "__main__":
    main()
