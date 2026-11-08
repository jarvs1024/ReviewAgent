"""E2E 基础设施：HTTP / Redis / GitLab / telemetry 访问，不含业务断言.

所有地址与凭证都在调用时从 env 读取（`config/instance.env` 由 shell 注入），
模块导入本身没有副作用 —— 测试可以安全 import。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import redis as _redis_mod

REPO_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_WEBHOOK_URL = "http://127.0.0.1:3000/webhook"
DEFAULT_TELEMETRY_BASE = "http://127.0.0.1:3000/api/v1/telemetry"
DEFAULT_PROJECT_ID = 34


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def webhook_url() -> str:
    return os.environ.get("REVIEWAGENT_WEBHOOK_URL", DEFAULT_WEBHOOK_URL)


def api_base() -> str:
    """平台 API 根（webhook URL 去掉 `/webhook`）."""
    return webhook_url().removesuffix("/webhook")


def telemetry_base() -> str:
    return os.environ.get("REVIEWAGENT_TELEMETRY_BASE", DEFAULT_TELEMETRY_BASE)


def gitlab_api_base() -> str:
    explicit = os.environ.get("REVIEWAGENT_GITLAB_API_BASE")
    if explicit:
        return explicit.rstrip("/")
    url = os.environ.get("GITLAB_URL", "http://127.0.0.1:8929").rstrip("/")
    return f"{url}/api/v4"


def webhook_secret() -> str:
    try:
        return os.environ["GITLAB_WEBHOOK_SECRET"]
    except KeyError as exc:  # pragma: no cover - 部署配置缺失
        raise RuntimeError("GITLAB_WEBHOOK_SECRET 未设置（先 source config/instance.env）") from exc


def gitlab_token() -> str:
    try:
        return os.environ["GITLAB_PERSONAL_ACCESS_TOKEN"]
    except KeyError as exc:  # pragma: no cover
        raise RuntimeError(
            "GITLAB_PERSONAL_ACCESS_TOKEN 未设置（先 source config/instance.env）"
        ) from exc


def project_id() -> int:
    return int(os.environ.get("REVIEWAGENT_WEEKLY_TARGET_PROJECT_ID", str(DEFAULT_PROJECT_ID)))


def _http(
    url: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 20,
) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def post_webhook(payload: dict, log: list[str]) -> tuple[int, dict]:
    body = json.dumps(payload).encode("utf-8")
    status, raw = _http(
        webhook_url(),
        method="POST",
        body=body,
        headers={
            "Content-Type": "application/json",
            "X-Gitlab-Event": payload.get("object_kind", "merge_request"),
            "X-Gitlab-Token": webhook_secret(),
        },
        timeout=30,
    )
    text = raw.decode("utf-8", errors="ignore")
    try:
        parsed: Any = json.loads(text) if text else {}
    except json.JSONDecodeError:
        parsed = {"raw_body": text}
    log.append(f"[{now_iso()}] HTTP {status} webhook: {parsed}")
    return status, parsed


def gitlab_get(path: str) -> Any:
    status, raw = _http(
        f"{gitlab_api_base()}{path}",
        headers={"PRIVATE-TOKEN": gitlab_token()},
    )
    if status != 200:
        raise RuntimeError(f"GitLab GET {path} -> {status}: {raw[:200]!r}")
    return json.loads(raw)


def gitlab_raw(project_id: int, path: str, ref: str) -> str:
    """读取仓库文件原文（`gitlab_request` 会 JSON 解码，raw 文件必须另走一条）."""
    import urllib.parse

    encoded = urllib.parse.quote(path, safe="")
    status, raw = _http(
        f"{gitlab_api_base()}/projects/{project_id}/repository/files/{encoded}/raw"
        f"?ref={urllib.parse.quote(ref, safe='')}",
        headers={"PRIVATE-TOKEN": gitlab_token()},
    )
    if status != 200:
        raise RuntimeError(f"GitLab raw {path}@{ref} -> {status}: {raw[:200]!r}")
    return raw.decode("utf-8", errors="replace")


def gitlab_request(method: str, path: str, *, payload: dict | None = None) -> Any:
    """GitLab 写操作（建项目/建分支/建 MR/删除）；失败抛 RuntimeError."""
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    status, raw = _http(
        f"{gitlab_api_base()}{path}",
        method=method,
        body=body,
        headers={"PRIVATE-TOKEN": gitlab_token(), "Content-Type": "application/json"},
        timeout=30,
    )
    text = raw.decode("utf-8", errors="ignore")
    if status >= 300:
        raise RuntimeError(f"GitLab {method} {path} -> {status}: {text[:300]}")
    return json.loads(text) if text else {}


def telemetry_get(path: str) -> tuple[int, Any]:
    status, raw = _http(f"{telemetry_base()}{path}", timeout=15)
    text = raw.decode("utf-8", errors="ignore")
    try:
        return status, json.loads(text) if text else {}
    except json.JSONDecodeError:
        return status, {"error": text}


def redis_client():
    return _redis_mod.from_url(
        os.environ.get("REDIS_URL", "redis://127.0.0.1:63790/2"),
        decode_responses=True,
    )


def clear_ephemeral_locks(log: list[str], *, pid: int | None = None) -> int:
    """只清冷却/调用计数/diff_head 这类跨轮残留.

    **绝不删除 `reviewagent:lock:*`**：那是链锁，清理会放行重复链（历史上已因此
    产生过并发重复检视）。链锁只能靠 TTL 自然过期。
    """
    target = pid if pid is not None else project_id()
    patterns = (
        "reviewagent:cooldown:*",
        "reviewagent:max_review_calls:*",
        f"reviewagent:diff_head:{target}:*",
    )
    r = redis_client()
    total = 0
    for pattern in patterns:
        keys = r.keys(pattern)
        if keys:
            total += r.delete(*keys)
    log.append(f"[{now_iso()}] cleared {total} ephemeral locks (cooldown/max_calls/diff_head)")
    return total


def delete_diff_head(mr_iid: int, *, pid: int | None = None) -> None:
    target = pid if pid is not None else project_id()
    redis_client().delete(f"reviewagent:diff_head:{target}:{mr_iid}")


def get_mr_info(mr_iid: int, log: list[str], *, pid: int | None = None) -> dict:
    target = pid if pid is not None else project_id()
    mr = gitlab_get(f"/projects/{target}/merge_requests/{mr_iid}")
    log.append(f"[{now_iso()}] mr {mr_iid} sha={mr['sha']} state={mr['state']}")
    return mr


def list_mr_runs(mr_iid: int, limit: int = 100, *, pid: int | None = None) -> list[dict]:
    target = pid if pid is not None else project_id()
    status, body = telemetry_get(f"/mr/{target}/{mr_iid}/runs?limit={limit}")
    if status != 200 or not isinstance(body, dict):
        return []
    return body.get("runs", body.get("items", [])) or []


def wait_for_new_run(
    mr_iid: int,
    before_count: int,
    *,
    expect_status: str = "any",  # success | failed | skipped | terminal | any
    expected_min_increase: int = 1,
    timeout_s: int = 300,
    log: list[str] | None = None,
    pid: int | None = None,
) -> dict:
    """阻塞等到出现 ≥ expected_min_increase 个新 run_record，且状态满足期望."""
    deadline = time.time() + timeout_s
    count = before_count
    while time.time() < deadline:
        runs = list_mr_runs(mr_iid, limit=max(50, before_count + 20), pid=pid)
        count = len(runs)
        delta = count - before_count
        if delta >= expected_min_increase:
            tops = runs[: max(1, delta)]
            statuses = [r.get("status") for r in tops]
            terminal = [s for s in statuses if s in ("success", "failed", "skipped")]
            if expect_status == "any":
                ok = True
            elif expect_status == "terminal":
                ok = len(terminal) >= expected_min_increase
            elif expect_status in ("success", "failed", "skipped"):
                ok = statuses.count(expect_status) >= expected_min_increase
            else:
                ok = True
            if ok:
                if log is not None:
                    log.append(
                        f"[{now_iso()}] wait_for_new_run ok mr={mr_iid} delta={delta} "
                        f"status={expect_status} tops="
                        f"{[(r.get('command'), r.get('status')) for r in tops]}"
                    )
                return {"before": before_count, "after": count, "delta": delta, "tops": tops, "ok": True}
        time.sleep(3)
    if log is not None:
        log.append(
            f"[{now_iso()}] TIMEOUT wait_for_new_run mr={mr_iid} before={before_count} "
            f"last={count} expected>={expected_min_increase} status={expect_status}"
        )
    return {
        "before": before_count,
        "after": count,
        "delta": count - before_count,
        "tops": [],
        "ok": False,
        "timeout": True,
    }


def make_merge_request_payload(
    mr_iid: int, action: str, actor: str = "review-tester@root", *, pid: int | None = None
) -> dict:
    mr = get_mr_info(mr_iid, [], pid=pid)
    name, _, username = actor.partition("@")
    return {
        "object_kind": "merge_request",
        "event_type": "merge_request",
        "user": {"name": name, "username": username or actor},
        "project": {"id": mr["project_id"], "name": str(mr["project_id"])},
        "object_attributes": {
            "iid": mr_iid,
            "title": mr["title"],
            "source_branch": mr["source_branch"],
            "target_branch": mr["target_branch"],
            "state": "opened",
            "action": action,
            # 真实 webhook 带这三个时间戳; 缺失会让 mr_activity.created_at 落 NULL.
            "created_at": mr.get("created_at"),
            "updated_at": mr.get("updated_at"),
            "merged_at": mr.get("merged_at"),
            "last_commit": {"id": mr["sha"]},
            "diff_refs": {
                "head_sha": mr["sha"],
                "start_sha": mr["sha"],
                "base_sha": mr["sha"],
            },
        },
    }


def make_note_payload(
    mr_iid: int,
    body: str,
    *,
    actor: str = "review-tester@root",
    note_type: str = "",
    note_id: int | None = None,
    discussion_id: str | None = None,
    is_system: bool = False,
    diff_file: str = "",
    diff_line: int = 0,
    pid: int | None = None,
) -> dict:
    mr = get_mr_info(mr_iid, [], pid=pid)
    name, _, username = actor.partition("@")
    return {
        "object_kind": "note",
        "event_type": "note",
        "user": {"name": name, "username": username or actor},
        "project": {"id": mr["project_id"], "name": str(mr["project_id"])},
        "merge_request": {"iid": mr_iid, "title": mr["title"]},
        "object_attributes": {
            "id": note_id if note_id is not None else int(time.time() * 1000),
            "note": body,
            "noteable_type": "MergeRequest",
            "type": note_type,
            "system": is_system,
            "discussion_id": discussion_id or "",
            "position": (
                {
                    "new_path": diff_file,
                    "old_path": diff_file,
                    "new_line": diff_line,
                    "old_line": diff_line,
                }
                if diff_file
                else None
            ),
        },
    }


def current_profile() -> tuple[str, str]:
    """当前磁盘上的生效场景包 `(name, fingerprint)`（env 由调用方先注入）."""
    from reviewagent.scenario.profile import load_profile

    profile = load_profile(None)
    return profile.name, profile.fingerprint


def _check_profile_freshness() -> tuple[bool, str]:
    """运行中的 API 必须加载当前磁盘场景包.

    改完 prompts/profile 忘记重启本地栈时，队列里的 job 会全部被
    `job.profile_mismatch` 拒绝，表现为全量档莫名超时；这里提前暴露。
    """
    status, raw = _http(f"{api_base()}/readyz", timeout=5)
    body = json.loads(raw) if raw else {}
    name, fingerprint = current_profile()
    served_name = body.get("profile")
    served_fp = body.get("profile_fingerprint")
    ok = status == 200 and served_name == name and served_fp == fingerprint
    detail = (
        f"served={str(served_name)}/{str(served_fp)[:12]} current={name}/{fingerprint[:12]}"
    )
    if not ok:
        detail += "（场景包有改动但服务未重启 → bash scripts/restart_local.sh）"
    return ok, detail


def probe() -> list[tuple[str, bool, str]]:
    """全量档前置检查：API / telemetry / Redis 都必须在位，且服务加载的是当前场景包."""
    checks: list[tuple[str, bool, str]] = []
    for name, url in (("api.readyz", f"{api_base()}/readyz"), ("telemetry.health", f"{telemetry_base()}/health")):
        try:
            status, raw = _http(url, timeout=5)
            checks.append((name, status == 200, f"{status} {raw[:80].decode('utf-8', errors='ignore')}"))
        except Exception as exc:  # noqa: BLE001 - 探针必须汇总而不是中断
            checks.append((name, False, f"{type(exc).__name__}: {exc}"))
    try:
        ok, detail = _check_profile_freshness()
        checks.append(("profile.freshness", ok, detail))
    except Exception as exc:  # noqa: BLE001
        checks.append(("profile.freshness", False, f"{type(exc).__name__}: {exc}"))
    try:
        checks.append(("redis.ping", bool(redis_client().ping()), "ok"))
    except Exception as exc:  # noqa: BLE001
        checks.append(("redis.ping", False, f"{type(exc).__name__}: {exc}"))
    return checks
