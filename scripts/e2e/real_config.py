"""真实 diff 规划断言：用真实 MR/分支变更跑 `planner.plan()`，证明 review.yaml 生效.

四项只靠进程内 witness 不够“真实产物”的配置：

* ``scoring.review_mode`` —— 真实 diff 的规划结果落成 test/business；
* ``file_filter.priority_paths`` —— 关键路径在真实优先级/文件上限中胜出；
* ``limits.full_files`` —— 真实文件集合上 full 策略配额被遵守；
* ``scoring.test_sample_paths / test_sample_max`` —— 真实 51 文件分支抽样。

另含 ``repo_context`` 的真实仓库拉取断言（按配置规则目录递归扫描）。
所有断言都基于 GitLab API 返回的真实 diff/文件内容；纯计算部分可单测。
"""
from __future__ import annotations

import dataclasses
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from scripts.e2e import fixtures, stack
from scripts.e2e.report import FeatureResult

# 关键路径夹具与真实场景包同构：/testlib/lib_baw/ 命中 priority_paths，
# app/extra.py 作对照（不命中任何关键路径，diff 更大）。
API_FILE = "testlib/lib_baw/e2e_helper.py"
EXTRA_FILE = "app/extra.py"
PRIORITY_FILES = (API_FILE,)

SAMPLING_COUNT = 51
SAMPLING_BRANCH_FMT = "e2e/sampling-r{}"


@dataclass(frozen=True)
class ChangeSet:
    """一次真实变更：文件集合、逐文件 diff、head 上的文件原文."""

    files: tuple[str, ...]
    diffs: dict[str, str]
    contents: dict[str, str]
    new_files: frozenset[str] = frozenset()


def _cfg(cfg: Any, **overrides: Any) -> Any:
    return dataclasses.replace(cfg, **overrides)


@contextmanager
def _active_config(cfg: Any) -> Iterator[None]:
    from reviewagent import config as config_mod

    saved = config_mod._current
    config_mod._current = cfg
    try:
        yield
    finally:
        config_mod._current = saved


# ---------------------------------------------------------------------------
# 纯计算：真实变更 + 生效配置 → Plan
# ---------------------------------------------------------------------------


def plan_changes(
    changes: ChangeSet,
    cfg: Any,
    *,
    profile: Any = None,
    policy: Any = None,
    project_id: int | None = None,
    mr_iid: int | None = None,
) -> Any:
    """用真实变更跑 planner；预算固定放宽，避免 E2BIG 降级干扰四项判定."""
    from reviewagent.review.planner import plan

    if profile is None:
        profile = SimpleNamespace(name="real", language="python", scoring={}, rule_keys={})
    lines = {fp: changes.contents.get(fp, "").splitlines() for fp in changes.files}
    if policy is None:
        from reviewagent.review.policies.registry import get as get_policy

        policy = get_policy(
            profile.language,
            profile=profile,
            cfg=cfg,
            read_lines=lambda fp: lines.get(fp, []),
        )
    return plan(
        list(changes.files),
        dict(changes.diffs),
        profile=profile,
        cfg=cfg,
        policy=policy,
        read_line_count=lambda fp: len(lines.get(fp, [])),
        stat_bytes=lambda fp: len(changes.contents.get(fp, "").encode("utf-8")),
        rule_block_bytes=0,
        budget_fn=lambda _n: 10**9,
        new_files=set(changes.new_files),
        project_id=project_id,
        mr_iid=mr_iid,
    )


def audit_review_config(
    changes: ChangeSet,
    cfg: Any,
    *,
    profile: Any = None,
    policy: Any = None,
) -> list[tuple[str, bool, str]]:
    """review_mode / priority_paths / full_files 的真实规划断言."""
    active = plan_changes(changes, cfg, profile=profile, policy=policy)
    results: list[tuple[str, bool, str]] = []

    flipped_mode = "business" if active.review_mode != "business" else "test"
    flipped = plan_changes(
        changes, _cfg(cfg, improve_review_mode=flipped_mode), profile=profile, policy=policy
    )
    configured = cfg.improve_review_mode
    mode_ok = (
        active.review_mode == configured
        if configured in ("test", "business")
        else active.review_mode in ("test", "business")
    )
    results.append(
        (
            "review_mode",
            mode_ok and flipped.review_mode == flipped_mode,
            f"config={configured} -> plan={active.review_mode}; "
            f"flip {flipped_mode} -> {flipped.review_mode}",
        )
    )

    missing = [fp for fp in (API_FILE, EXTRA_FILE) if fp not in changes.files]
    if missing:
        results.append(("priority_paths", False, f"真实 MR 缺少夹具文件: {missing}"))
    else:
        no_keyword = plan_changes(
            changes, _cfg(cfg, improve_keyword_paths=()), profile=profile, policy=policy
        )
        cap = len([fp for fp in PRIORITY_FILES if fp in changes.files]) + 1
        capped = plan_changes(
            changes, _cfg(cfg, improve_max_files_to_review=cap), profile=profile, policy=policy
        )
        on_scores = active.file_priorities
        off_scores = no_keyword.file_priorities
        kept = capped.files_to_review
        ok = (
            on_scores[API_FILE] > on_scores[EXTRA_FILE]
            and off_scores[EXTRA_FILE] >= off_scores[API_FILE]
            and API_FILE in kept
            and EXTRA_FILE in capped.cap_truncated
        )
        results.append(
            (
                "priority_paths",
                ok,
                f"score api={on_scores[API_FILE]:.1f} > extra={on_scores[EXTRA_FILE]:.1f}; "
                f"无关键路径时 extra={off_scores[EXTRA_FILE]:.1f} >= api={off_scores[API_FILE]:.1f}; "
                f"max_files={cap} 保留 api, 截断 extra={EXTRA_FILE in capped.cap_truncated}",
            )
        )

    full_active = [fp for fp, s in active.strategies.items() if s == "full"]
    full_one = plan_changes(
        changes, _cfg(cfg, improve_full_files=1), profile=profile, policy=policy
    )
    full_one_count = sum(1 for s in full_one.strategies.values() if s == "full")
    full_off = plan_changes(
        changes, _cfg(cfg, improve_full_files=0), profile=profile, policy=policy
    )
    full_off_count = sum(1 for s in full_off.strategies.values() if s == "full")
    quota = cfg.improve_full_files
    results.append(
        (
            "full_files",
            0 < len(full_active) <= quota and full_one_count == 1 and full_off_count == 0,
            f"quota={quota} 实际 full={len(full_active)}/{len(active.strategies)}; "
            f"override 1 -> {full_one_count}, override 0 -> {full_off_count}",
        )
    )
    return results


def audit_sampling(
    changes: ChangeSet,
    cfg: Any,
    *,
    profile: Any = None,
    policy: Any = None,
) -> list[tuple[str, bool, str]]:
    """test_sample_paths / test_sample_max 的真实分支抽样断言."""
    max_n = cfg.improve_test_sample_max
    total = len(changes.files)
    active = plan_changes(changes, cfg, profile=profile, policy=policy)
    kept = [fp for fp in active.files if any(p in fp for p in cfg.improve_test_sample_paths)]
    skipped = list(active.test_skipped)

    off = plan_changes(
        changes, _cfg(cfg, improve_test_sample_max=0), profile=profile, policy=policy
    )
    off_kept = [fp for fp in off.files if any(p in fp for p in cfg.improve_test_sample_paths)]
    again = plan_changes(changes, cfg, profile=profile, policy=policy)

    expected = min(max_n, total) if max_n > 0 else total
    ok = (
        total > max_n > 0
        and len(kept) == expected
        and len(skipped) == total - expected
        and len(off_kept) == total
        and not off.test_skipped
        and list(again.files) == list(active.files)
    )
    return [
        (
            "test_sampling",
            ok,
            f"branch={total} files, max={max_n} -> kept={len(kept)} skipped={len(skipped)}; "
            f"max=0 -> kept={len(off_kept)} skipped={len(off.test_skipped)}; "
            f"deterministic={list(again.files) == list(active.files)}",
        )
    ]


# ---------------------------------------------------------------------------
# 真实仓库规则上下文
# ---------------------------------------------------------------------------


class _StackGitlabClient:
    """`fetch_rule_files` 需要的最小 GitLab 客户端（全部走 E2E stack 的 HTTP）."""

    def _get_project(self, project_id: int) -> SimpleNamespace:
        project = stack.gitlab_get(f"/projects/{project_id}")
        return SimpleNamespace(default_branch=project.get("default_branch") or "main")

    def get_file_at_sha(self, project_id: int, path: str, ref: str) -> str | None:
        try:
            return stack.gitlab_raw(project_id, path, ref)
        except RuntimeError:
            return None

    def list_repository_tree(
        self, project_id: int, path: str, ref: str, *, recursive: bool = True
    ) -> list[dict]:
        query = urllib.parse.urlencode(
            {"path": path, "ref": ref, "per_page": 100, "recursive": str(recursive).lower()}
        )
        data = stack.gitlab_get(f"/projects/{project_id}/repository/tree?{query}")
        return list(data) if isinstance(data, list) else []


def audit_repo_context(project_id: int, cfg: Any, log: list[str]) -> list[tuple[str, bool, str]]:
    """按配置的规则目录递归拉取 `.md` 并渲染（默认 `.agents/rules`）."""
    from reviewagent.scenario.rules import repo

    client = _StackGitlabClient()
    repo._files_cache.clear()
    repo._files_cache_ts.clear()
    with _active_config(cfg):
        files = repo.fetch_rule_files(client, project_id)
        block = repo.build_repo_context(client, project_id)

    rules_dir = repo._normalize_rules_dir(cfg.repo_context_rules_dir)
    dir_files = [p for p in files if rules_dir and p.startswith(f"{rules_dir}/")]
    nested = [p for p in dir_files if "/" in p[len(rules_dir) + 1:]]
    ok = (
        bool(rules_dir)
        and bool(files)
        and len(dir_files) == len(files)
        and bool(nested)
        and "<instruction_files>" in block
        and "E2E repo rule marker" in block
        and "E2E repo context marker" in block
        and "SSD-RULE-E2E-IMPORT" in block
    )
    log.append(
        f"[{stack.now_iso()}] repo_context pid={project_id} dir={rules_dir} "
        f"files={sorted(files)}"
    )
    return [
        (
            "repo_context",
            ok,
            f"dir={rules_dir} nested={nested} -> files={sorted(files)} "
            f"rendered={len(block)}B",
        )
    ]


# ---------------------------------------------------------------------------
# 真实数据获取
# ---------------------------------------------------------------------------


def _with_header(path: str, diff: str) -> str:
    """GitLab `/changes` 的 diff 正文从 `@@` 开始；补回文件头让行号解析生效."""
    if diff.lstrip().startswith("diff --git "):
        return diff
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n{diff}"


def fetch_mr_changes(project_id: int, mr_iid: int, log: list[str]) -> ChangeSet:
    """MR /changes + head 原文（与检视链路口径一致）."""
    mr = stack.gitlab_get(f"/projects/{project_id}/merge_requests/{mr_iid}")
    payload = stack.gitlab_get(f"/projects/{project_id}/merge_requests/{mr_iid}/changes")
    entries = payload.get("changes") or []
    files: list[str] = []
    diffs: dict[str, str] = {}
    contents: dict[str, str] = {}
    new_files: set[str] = set()
    for entry in entries:
        if entry.get("deleted_file"):
            continue
        path = entry.get("new_path") or entry.get("old_path") or ""
        if not path:
            continue
        files.append(path)
        diffs[path] = _with_header(path, entry.get("diff") or "")
        contents[path] = stack.gitlab_raw(project_id, path, mr["sha"])
        if entry.get("new_file"):
            new_files.add(path)
    log.append(
        f"[{stack.now_iso()}] real_config mr={mr_iid} sha={mr['sha'][:10]} "
        f"files={sorted(files)}"
    )
    return ChangeSet(tuple(sorted(files)), diffs, contents, frozenset(new_files))


def _diff_contents(diffs: list[dict]) -> tuple[list[str], dict[str, str], dict[str, str]]:
    files: list[str] = []
    by_file: dict[str, str] = {}
    contents: dict[str, str] = {}
    for entry in diffs:
        path = entry.get("new_path") or entry.get("old_path") or ""
        if not path or entry.get("deleted_file"):
            continue
        diff = entry.get("diff") or ""
        files.append(path)
        by_file[path] = _with_header(path, diff)
        contents[path] = "\n".join(
            line[1:] for line in diff.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )
    return files, by_file, contents


def fetch_compare_changes(
    project_id: int, from_ref: str, to_ref: str, log: list[str]
) -> ChangeSet:
    """`/repository/compare` 的真实 diff（抽样分支用）."""
    query = urllib.parse.urlencode({"from": from_ref, "to": to_ref})
    payload = stack.gitlab_get(f"/projects/{project_id}/repository/compare?{query}")
    files, diffs, contents = _diff_contents(payload.get("diffs") or [])
    log.append(
        f"[{stack.now_iso()}] real_config compare {from_ref}..{to_ref} files={len(files)}"
    )
    return ChangeSet(tuple(sorted(files)), diffs, contents)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def run(
    mr_iid: int,
    round_idx: int,
    project_id: int,
    cfg: Any,
    log: list[str],
) -> list[FeatureResult]:
    """全量档入口：真实 MR / 抽样分支 / 仓库规则上下文三组断言."""
    from reviewagent.scenario.profile import load_profile

    profile = load_profile()
    results: list[FeatureResult] = []

    def _add(prefix: str, checks: list[tuple[str, bool, str]]) -> None:
        for name, ok, detail in checks:
            result = FeatureResult(f"config_real:{prefix}{name}", round_idx, mr_iid, ok, detail, log)
            results.append(result)

    try:
        changes = fetch_mr_changes(project_id, mr_iid, log)
        _add("", audit_review_config(changes, cfg, profile=profile))
    except Exception as exc:  # noqa: BLE001 - 单组失败要进报告
        log.append(f"[{stack.now_iso()}] real_config mr failed: {type(exc).__name__}: {exc}")
        results.append(
            FeatureResult(
                "config_real:mr_changes", round_idx, mr_iid, False,
                f"{type(exc).__name__}: {exc}", log,
            )
        )

    branch = SAMPLING_BRANCH_FMT.format(round_idx)
    try:
        fixtures.create_sampling_branch(project_id, branch, SAMPLING_COUNT, log)
        sampling = fetch_compare_changes(project_id, "main", branch, log)
        _add("", audit_sampling(sampling, cfg, profile=profile))
    except Exception as exc:  # noqa: BLE001
        log.append(f"[{stack.now_iso()}] real_config sampling failed: {type(exc).__name__}: {exc}")
        results.append(
            FeatureResult(
                "config_real:test_sampling", round_idx, mr_iid, False,
                f"{type(exc).__name__}: {exc}", log,
            )
        )
    finally:
        try:
            fixtures.delete_branch(project_id, branch, log)
        except Exception as exc:  # noqa: BLE001 - 清理失败只告警
            log.append(f"[{stack.now_iso()}] WARN delete branch {branch}: {exc}")

    try:
        _add("", audit_repo_context(project_id, cfg, log))
    except Exception as exc:  # noqa: BLE001
        log.append(f"[{stack.now_iso()}] real_config repo_context failed: {type(exc).__name__}: {exc}")
        results.append(
            FeatureResult(
                "config_real:repo_context", round_idx, mr_iid, False,
                f"{type(exc).__name__}: {exc}", log,
            )
        )
    return results
