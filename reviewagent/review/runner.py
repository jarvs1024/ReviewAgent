"""单次 /improve run 的编排：plan → 复用 → 并行 LLM → 合并.

`run()` 消费 `review/planner.py:plan()` 的规划结果，只负责 store 读写、
指标、LLM 调用与合并；prompt / 源码装配仍经 `Callbacks` 交回门面
（`reviewagent/commands/improve.py` 收敛为兼容门面）.
"""
from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from reviewagent.logging_setup import logger
from reviewagent.review.dynamic.runner import refs_by_file, run_dynamic
from reviewagent.review.planner import (
    Plan,
    group_files_for_batching,
)
from reviewagent.review.planner import (
    plan as plan_files,
)
from reviewagent.review.validation import merge_chunks

# 动态规则整体线程超时（规则自身 params.timeout_seconds 优先）。
_DEFAULT_DYNAMIC_TIMEOUT_S = 35.0


@dataclass
class Callbacks:
    """门面注入的绑定方法（prompt / 源码 / store 邻接的读取都在门面侧）."""

    split_diff: Callable[[Path, list[str]], dict[str, str]]
    diff_line_map: Callable[[], dict[str, set[int]]]
    read_lines: Callable[[str], list[str]]
    read_line_count: Callable[[str, Any], int]
    stat_bytes: Callable[[str], int]
    blob_sha: Callable[[str, Any], str | None]
    head_sha: Callable[[], str | None]
    new_files: Callable[[Path], set[str]]
    collect_cross_refs: Callable[[list[str], dict[str, str], str], dict[str, list[dict]]]
    build_chunk_prompt: Callable[..., str]
    build_batch_prompt: Callable[..., str]
    call_chunk: Callable[[str, Any, str], Any]
    build_user_prompt: Callable[[], str]
    super_call_agent: Callable[[Any], dict]
    set_token_summary: Callable[[int, int, str, float], None]
    budget_fn: Callable[[int], int]


@dataclass
class RunContext:
    """一次 run 的输入；`review_mode` 由 runner 回写（门面同步到实例）."""

    project_id: Any
    mr_iid: Any
    ws: Any
    repo_context: str
    cfg: Any
    profile: Any
    rule_set: Any
    dynamic_rules: Any
    policy: Any
    callbacks: Callbacks
    overflow_files: list[str] | None = None
    review_mode: str = "business"


def _empty_summary(test_skipped: tuple[str, ...]) -> dict[str, Any]:
    if not test_skipped:
        return {"summary_md": "## 改进总览\n\n无代码文件变更，跳过检视。", "suggestions": []}
    shown = ", ".join(test_skipped[:10])
    suffix = "..." if len(test_skipped) > 10 else ""
    return {
        "summary_md": (
            f"## 改进总览\n\n> ⏭️ 以下 {len(test_skipped)} 个测试/配置文件跳过检视: "
            f"{shown}{suffix}"
        ),
        "suggestions": [],
    }


def _groups(
    files_llm: list[str],
    diff_by_file: dict[str, str],
    strategies: dict[str, str],
    cfg: Any,
) -> list[list[str]]:
    groups: list[list[str]] = [[fp] for fp in files_llm]
    if cfg.improve_batch_enabled and len(files_llm) > 1:
        groups = group_files_for_batching(
            files_llm, diff_by_file, strategies,
            max_diff_lines=cfg.improve_batch_max_diff_lines,
            max_files=cfg.improve_batch_max_files,
            max_total_lines=cfg.improve_batch_max_total_lines,
        )
    return groups


def _upsert_file_review_states(
    ctx: RunContext,
    files: tuple[str, ...],
    head_sha: str,
    per_file_counts: dict[str, int],
) -> None:
    try:
        from reviewagent.telemetry.store import get_store

        store = get_store()
        for fp in files:
            store.upsert_file_review_state(
                project_id=ctx.project_id, mr_iid=ctx.mr_iid,
                file_path=fp, blob_sha=ctx.callbacks.blob_sha(fp, ctx.ws),
                head_sha=head_sha, suggestion_count=per_file_counts.get(fp, 0),
            )
    except Exception as e:
        logger.warning("improve.upsert_file_state_failed (non-fatal): {}", e)


def run(ctx: RunContext) -> dict[str, Any]:
    callbacks = ctx.callbacks
    line_map = callbacks.diff_line_map()
    files = sorted(line_map)
    new_files = set()
    if getattr(ctx.ws, "diff_file", None):
        new_files = callbacks.new_files(ctx.ws.diff_file)
    diff_by_file = callbacks.split_diff(ctx.ws.diff_file, files) if files else {}

    try:
        rule_block_bytes = len((ctx.rule_set.verbatim_body or "").encode())
    except Exception:
        rule_block_bytes = 0

    publish_head_sha = ""
    file_states: dict[str, dict] = {}
    if line_map:
        publish_head_sha = callbacks.head_sha() or ""
        if publish_head_sha:
            from reviewagent.telemetry.store import get_store

            try:
                file_states = get_store().get_file_review_states(ctx.project_id, ctx.mr_iid)
            except Exception as e:
                logger.warning("improve.incremental_lookup_failed (non-fatal, full review): {}", e)
                file_states = {}

    planned: Plan = plan_files(
        files,
        diff_by_file,
        profile=ctx.profile,
        cfg=ctx.cfg,
        policy=ctx.policy,
        read_line_count=lambda fp: callbacks.read_line_count(fp, ctx.ws),
        stat_bytes=callbacks.stat_bytes,
        prior_states=file_states,
        head_sha=publish_head_sha,
        blob_sha=lambda fp: callbacks.blob_sha(fp, ctx.ws),
        rule_block_bytes=rule_block_bytes,
        profile_fingerprint=None,
        ruleset_fingerprint=None,
        new_files=new_files,
        overflow_set=ctx.overflow_files,
        line_map=line_map,
        budget_fn=callbacks.budget_fn,
        repo_context=ctx.repo_context,
        project_id=ctx.project_id,
        mr_iid=ctx.mr_iid,
    )
    ctx.review_mode = planned.review_mode

    if not planned.files:
        return _empty_summary(planned.test_skipped)

    from reviewagent.metrics import inc as _metric_inc

    strat_counts = {"full": 0, "partial": 0, "patch": 0}
    for s in planned.strategies.values():
        strat_counts[s] = strat_counts.get(s, 0) + 1
    _metric_inc(
        "reviewagent_improve_strategy_total",
        project_id=str(ctx.project_id),
        mr_iid=str(ctx.mr_iid),
        strategy="full", amount=float(strat_counts["full"]),
    )
    _metric_inc(
        "reviewagent_improve_strategy_total",
        project_id=str(ctx.project_id),
        mr_iid=str(ctx.mr_iid),
        strategy="partial", amount=float(strat_counts["partial"]),
    )
    _metric_inc(
        "reviewagent_improve_strategy_total",
        project_id=str(ctx.project_id),
        mr_iid=str(ctx.mr_iid),
        strategy="patch", amount=float(strat_counts["patch"]),
    )

    if planned.single_file:
        if not planned.single_file_over_budget:
            return callbacks.super_call_agent(ctx.ws)
        # E2BIG 防线: 单文件源码块超预算时不走基类捷径 (基类
        # _build_user_prompt 无条件内嵌全部源码, MR 1136 即此路径触发
        # [Errno 7]), 落到 strategy 路径按预算降级 partial/patch
        fp = planned.files[0]
        diff_lines, file_lines = len(line_map.get(fp, ())), callbacks.read_line_count(fp, ctx.ws)
        file_bytes = callbacks.stat_bytes(fp)
        logger.info(
            "improve.full_source_budget_downgrade project={} mr={} file={} "
            "file_bytes={} est_source_bytes={} budget={} -> strategy path (single-file)",
            ctx.project_id, ctx.mr_iid, fp, file_bytes,
            file_bytes + file_lines * 7, callbacks.budget_fn(diff_lines),
        )

    if planned.reused:
        _metric_inc(
            "reviewagent_improve_incremental_reuse_total",
            project_id=str(ctx.project_id), mr_iid=str(ctx.mr_iid),
            amount=float(len(planned.reused)),
        )

    files_to_review = list(planned.files_to_review)
    files_reuse = list(planned.reused)

    # 全局一次 rg 找所有 diff 文件的跨文件 caller 引用 (替代 per-file 重复 rg)
    # V8: 只对需重检的文件做 rg (reuse 文件不调 LLM, 不需要)
    # 有动态规则集时走 run_dynamic（四态证据）；否则保持 legacy 直调回调。
    cross_file_enabled = True
    if ctx.dynamic_rules is None:
        cross_file_refs_by_file = callbacks.collect_cross_refs(
            files_to_review, diff_by_file, str(ctx.ws.worktree),
        )
    else:
        enabled_rules = [r for r in ctx.dynamic_rules.rules if r.enabled]
        if enabled_rules:
            evidence = run_dynamic(
                ctx.dynamic_rules,
                files=files_to_review,
                diff_by_file=diff_by_file,
                worktree=str(ctx.ws.worktree),
                timeout_s=_DEFAULT_DYNAMIC_TIMEOUT_S,
                fetch_callback=callbacks.collect_cross_refs,
            )
            cross_file_refs_by_file = refs_by_file(evidence, files_to_review)
            for key, ev in evidence.items():
                if ev.degraded:
                    logger.warning(
                        "dynamic.degraded rule={} provider={} reason={}",
                        key, ev.provider, ev.reason,
                    )
        else:
            # 规则全部禁用 → 整段不存在（不是空模板）。
            cross_file_refs_by_file = {}
            cross_file_enabled = False

    # V9: 从 store 取复用文件上轮的 open suggestions (blob 相同 → 仍有效)
    # 只取 state=open (applied/dismissed 不重显); head_sha 用该文件上次检视的 head
    # (blob 相同 → 内容字节相同 → 行号必然仍有效)
    # L4: 同时取待重检文件 (blob 变了) 的 prior open issues 作 prompt 锚点
    # → LLM 确认而非从零重推, 省 output token + 跨轮连续; 行号已偏移, 让 LLM 重定位
    reuse_suggestions: list[dict[str, Any]] = []
    prior_issues_by_file: dict[str, list[dict[str, Any]]] = {}
    need_load = bool(files_reuse) or (bool(files_to_review) and bool(file_states))
    if need_load:
        from reviewagent.telemetry.store import get_store

        try:
            store = get_store()
            rows = store.list_suggestions(
                project_id=ctx.project_id, mr_iid=ctx.mr_iid, limit=500,
            )
            reuse_set = set(files_reuse)
            review_set = set(files_to_review)
            for r in rows:
                fp = r.get("file_path", "")
                if not fp:
                    continue
                last_head = (file_states.get(fp) or {}).get("head_sha") or ""
                if (r.get("head_sha") != last_head
                        or (r.get("state") or "open") != "open"):
                    continue
                item = {
                    "file": fp,
                    "start_line": r.get("target_line", 0),
                    "severity": r.get("severity", "medium"),
                    "label": r.get("label", ""),
                    "header": r.get("header", ""),
                    "rationale": r.get("one_sentence_summary", "") or r.get("header", ""),
                }
                if fp in reuse_set:
                    reuse_suggestions.append(item)
                elif fp in review_set:
                    prior_issues_by_file.setdefault(fp, []).append(item)
            if files_reuse:
                logger.info(
                    "improve.incremental_reuse_loaded project={} mr={} reuse_files={} suggestions={}",
                    ctx.project_id, ctx.mr_iid, len(files_reuse), len(reuse_suggestions),
                )
            if prior_issues_by_file:
                logger.info(
                    "improve.prior_anchors project={} mr={} files={} anchors={}",
                    ctx.project_id, ctx.mr_iid,
                    len(prior_issues_by_file),
                    sum(len(v) for v in prior_issues_by_file.values()),
                )
        except Exception as e:
            logger.warning("improve.reuse_load_failed (non-fatal): {}", e)
            # 复用失败 → 降级为全检; 锚点失败 → 跳过锚点 (LLM 自行重推, 无害)
            if files_reuse:
                files_to_review = list(planned.files)
                files_reuse = []

    if not files_to_review and files_reuse:
        logger.info("improve.all_reused project={} mr={} — skip LLM entirely", ctx.project_id, ctx.mr_iid)
        # V9: 记录本轮各文件状态 (全复用, blob 不变, count=复用建议数)
        reused_counts: dict[str, int] = {}
        for s in reuse_suggestions:
            fp = s.get("file", "")
            if fp:
                reused_counts[fp] = reused_counts.get(fp, 0) + 1
        _upsert_file_review_states(ctx, planned.files, publish_head_sha, reused_counts)
        return merge_chunks(
            [{"summary_md": "", "suggestions": reuse_suggestions}],
            cfg=ctx.cfg,
            score_fn=ctx.policy.score_suggestion,
            skipped_files=list(planned.test_skipped),
            file_priorities=dict(planned.file_priorities),
            new_files=new_files,
            cap_truncated=list(planned.cap_truncated),
        )

    reuse_load_degraded = bool(planned.reused) and not files_reuse
    files_llm = files_to_review if files_to_review else list(planned.files)
    if files_to_review and not reuse_load_degraded:
        groups = [list(g) for g in planned.batches]
    else:
        groups = _groups(files_llm, diff_by_file, planned.strategies, ctx.cfg)
        _batched = sum(1 for g in groups if len(g) > 1)
        _merged_files = sum(len(g) for g in groups if len(g) > 1)
        if _batched:
            logger.info(
                "improve.batch project={} mr={} files={} llm_calls={} batched_groups={} merged_files={}",
                ctx.project_id, ctx.mr_iid, len(files_llm), len(groups), _batched, _merged_files,
            )

    workers = min(len(groups), ctx.cfg.improve_parallel_workers)

    logger.info(
        "improve.parallel project={} mr={} files_to_review={} reuse={} groups={} workers={}",
        ctx.project_id, ctx.mr_iid, len(files_llm), len(files_reuse), len(groups), workers,
    )

    chunk_results: list[dict[str, Any]] = []
    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_cost_credits = 0.0
    last_model = ""
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for group in groups:
            if len(group) == 1:
                fp = group[0]
                prompt = callbacks.build_chunk_prompt(
                    fp, diff_by_file.get(fp, ""), line_map.get(fp, set()), ctx.ws,
                    cross_file_refs=cross_file_refs_by_file.get(fp, []),
                    strategy=planned.strategies.get(fp, "full"),
                    prior_issues=prior_issues_by_file.get(fp),
                    cross_file_enabled=cross_file_enabled,
                )
                fut = pool.submit(callbacks.call_chunk, prompt, ctx.ws, fp)
            else:
                prompt = callbacks.build_batch_prompt(
                    group, diff_by_file, line_map, ctx.ws,
                    cross_file_refs_by_file, planned.strategies, prior_issues_by_file,
                    cross_file_enabled=cross_file_enabled,
                )
                fut = pool.submit(callbacks.call_chunk, prompt, ctx.ws, f"batch({len(group)}f)")
            futures[fut] = group
        for fut in as_completed(futures):
            group = futures[fut]
            label = group[0] if len(group) == 1 else f"batch({len(group)}f)"
            try:
                oc_result = fut.result()
                data = oc_result.data
                # V10: 批次防御 — 丢弃 file 不在组内的 suggestions (防归属混淆)
                if len(group) > 1 and isinstance(data, dict):
                    gs = set(group)
                    suggs = data.get("suggestions") or []
                    valid = [s for s in suggs if isinstance(s, dict) and s.get("file") in gs]
                    dropped = len(suggs) - len(valid)
                    if dropped:
                        logger.warning(
                            "improve.batch_dropped project={} mr={} group={} dropped={} (file 字段不在组内)",
                            ctx.project_id, ctx.mr_iid, label, dropped,
                        )
                    data["suggestions"] = valid
                chunk_results.append(data)
                total_prompt_tokens += oc_result.prompt_tokens
                total_completion_tokens += oc_result.completion_tokens
                total_cost_credits += oc_result.cost_credits
                if oc_result.model:
                    last_model = oc_result.model
            except Exception as e:
                logger.error(
                    "improve.chunk_failed project={} mr={} file={} err={}",
                    ctx.project_id, ctx.mr_iid, label, e,
                )
                # 单个 chunk 失败不影响其他 (batch 失败 = 全组空结果, 不拆分重试)
                chunk_results.append({"summary_md": "", "suggestions": []})

    # 汇总 token 统计交回门面写入 _last_oc_result (主线程安全写入)
    callbacks.set_token_summary(
        total_prompt_tokens, total_completion_tokens, last_model, total_cost_credits,
    )

    # V8: 把复用的 suggestions 也合并进结果 (作为额外的 "chunk result")
    if reuse_suggestions:
        chunk_results.append({"summary_md": "", "suggestions": reuse_suggestions})

    # V9: 记录本轮各文件状态 (LLM 产出 + 复用), 供下一轮按文件粒度复用
    counts: dict[str, int] = {}
    for cr in chunk_results:
        for s in (cr.get("suggestions") or []):
            fp = s.get("file", "")
            if fp:
                counts[fp] = counts.get(fp, 0) + 1
    _upsert_file_review_states(ctx, planned.files, publish_head_sha, counts)

    # skipped_files 已含 test_skipped (上文 planned.test_skipped)
    # V6 软阈值化后不再有"限额 overflow 丢弃"——overflow 文件降级为 patch 仍进 LLM
    return merge_chunks(
        chunk_results,
        cfg=ctx.cfg,
        score_fn=ctx.policy.score_suggestion,
        skipped_files=list(planned.test_skipped),
        file_priorities=dict(planned.file_priorities),
        new_files=new_files,
        cap_truncated=list(planned.cap_truncated),
    )


__all__ = [
    "Callbacks",
    "RunContext",
    "merge_chunks",
    "run",
]
