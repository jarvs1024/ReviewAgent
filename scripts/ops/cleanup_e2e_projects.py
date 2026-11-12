"""清理 GitLab 上堆积的 **E2E 临时项目**（O5）.

背景:
E2E 每轮会在 GitLab 上建一个临时项目 (`e2e-review-<ts>-r<N>` / `e2e-review-probe-*` /
`probe-create-test-*`)。跑完不删的话, 本地 GitLab 上会堆几十个僵尸项目 ——
实测已累积 34 个 `deletion_scheduled` + 若干正常态残留。

安全约束（**默认极保守**）:
1. 只删**名字精确匹配 E2E 命名模式**且 **owner 是当前 token 所属用户** 的项目。
   root 拥有的项目（如 id=1/3/28）一律跳过 —— 机器人没有权限也不该动。
2. 真实业务项目**永不删除**: `ssd-real-biz-full` (p31) 及其任何含
   `real-biz` 的路径都在保护名单里, 即便命名碰巧命中模式。
3. 默认 `--dry-run`: 只列出将删的项目, 人工确认后再加 `--apply` 真删。
4. 已是 `deletion_scheduled` 的项目 GitLab 自己会清, 本脚本**跳过**它们
   （重复 DELETE 只会拿到 404/403, 没有额外价值)。

用法:
  set -a; source config/instance.env; set +a
  .venv/bin/python scripts/ops/cleanup_e2e_projects.py            # 列出（dry-run）
  .venv/bin/python scripts/ops/cleanup_e2e_projects.py --apply    # 真删
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

#: E2E 临时项目的命名模式（必须整体匹配 path，不做模糊匹配）。
#: 实际形态: e2e-review-20261108t1132-r1 (时间戳 = 8 位日期 + t + 4 位时分)
#:         / e2e-review-probe-r99-deletion_scheduled-4
E2E_PROJECT_PATTERNS = (
    re.compile(r"^e2e-review-\d{8}t\d{4}-r\d+$"),
    re.compile(r"^e2e-review-probe-r\d+.*$"),
    re.compile(r"^probe-create-test$"),
)

#: 任何路径里含这些片段 → 永不删除。
PROTECTED_SUBSTRINGS = (
    "real-biz",     # 真实业务项目 (p31 ssd-real-biz-full / 副本)
    "ssd-review-demo",
    "ssd-dynamic-proof",
    "manual-review",
    "testmate-extensions",
)


def _ensure_env() -> None:
    for k in ("GITLAB_URL", "GITLAB_PERSONAL_ACCESS_TOKEN"):
        if not os.environ.get(k):
            raise RuntimeError(
                f"missing required env var: {k}; source config/instance.env first"
            )


class _GitLab:
    def __init__(self, base: str, token: str) -> None:
        self.base = base.rstrip("/")
        self.token = token

    def _request(self, method: str, path: str) -> tuple[int, object]:
        req = urllib.request.Request(
            self.base + path,
            method=method,
            headers={"PRIVATE-TOKEN": self.token},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8")
                return resp.status, (json.loads(body) if body else None)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="ignore")
            try:
                return e.code, json.loads(body)
            except json.JSONDecodeError:
                return e.code, body[:200]

    def get(self, path: str) -> tuple[int, object]:
        return self._request("GET", path)

    def delete(self, path: str) -> tuple[int, object]:
        return self._request("DELETE", path)

    def me(self) -> dict:
        status, body = self.get("/api/v4/user")
        if status != 200 or not isinstance(body, dict):
            raise RuntimeError(f"cannot resolve current user: status={status} body={body}")
        return body


def _is_e2e_project(path: str) -> bool:
    if any(sub in path for sub in PROTECTED_SUBSTRINGS):
        return False
    return any(p.match(path) for p in E2E_PROJECT_PATTERNS)


def main() -> int:
    _ensure_env()
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true",
                   help="真删除（默认只列出）")
    p.add_argument("--include-scheduled", action="store_true",
                   help="连同已 deletion_scheduled 的一并处理（默认跳过）")
    args = p.parse_args()

    gl = _GitLab(os.environ["GITLAB_URL"], os.environ["GITLAB_PERSONAL_ACCESS_TOKEN"])
    me = gl.me()
    my_username = me.get("username", "")
    my_id = me.get("id")
    print(f"[i] 当前用户: {my_username} (id={my_id})")

    status, projects = gl.get("/api/v4/projects?per_page=100&order_by=id&sort=asc")
    if status != 200 or not isinstance(projects, list):
        print(f"[!] 拉取项目失败: status={status} body={projects}", file=sys.stderr)
        return 1

    candidates: list[dict] = []
    skipped_scheduled = 0
    skipped_other_owner = 0
    skipped_protected = 0
    for proj in projects:
        path = str(proj.get("path") or "")
        full = str(proj.get("path_with_namespace") or "")
        if "deletion_scheduled" in full and not args.include_scheduled:
            skipped_scheduled += 1
            continue
        if not _is_e2e_project(path):
            if any(sub in full for sub in PROTECTED_SUBSTRINGS):
                skipped_protected += 1
            continue
        owner = str((proj.get("namespace") or {}).get("path") or "")
        if owner != my_username:
            skipped_other_owner += 1
            continue
        candidates.append(proj)

    print(f"[i] 扫描 {len(projects)} 个项目")
    print(f"[i] 跳过 deletion_scheduled: {skipped_scheduled}")
    print(f"[i] 跳过受保护(真实业务/手工): {skipped_protected}")
    print(f"[i] 跳过非本用户 owner: {skipped_other_owner}")
    print(f"[i] 待处理 E2E 项目: {len(candidates)}")
    for proj in candidates:
        print(f"    - id={proj['id']:>4} {proj.get('path_with_namespace')}")

    if not args.apply:
        print("[i] dry-run: 加 --apply 才真删")
        return 0

    if not candidates:
        print("[i] 无需删除")
        return 0

    ok = 0
    failed: list[tuple[int, int, object]] = []
    for proj in candidates:
        pid = int(proj["id"])
        code, body = gl.delete(f"/api/v4/projects/{pid}")
        if code in (202, 204):
            ok += 1
            print(f"    ✓ deleted id={pid} {proj.get('path_with_namespace')}")
        else:
            failed.append((pid, code, body))
            print(f"    ✗ failed  id={pid} status={code} body={body}")
    print(f"[ok] 删除 {ok}/{len(candidates)} 个项目")
    if failed:
        print(f"[!] 失败 {len(failed)} 个: {[f[0] for f in failed]}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
