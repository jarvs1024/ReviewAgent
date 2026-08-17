"""/improve 命令端到端.

工作流:
    1. 调 opencode agent `improve`
    2. 解析 agent 返回 {summary_md, suggestions[]}
    3. 顶层 summary 评论
    4. 每条 suggestion 作为可 Apply 的 inline comment 发出
       GitLab UI 会渲染代码块 + "Apply suggestion" 按钮，让 reviewer 一键 commit.

格式说明:
    GitLab "committable suggestion" 的 body 必须是 markdown 代码块，\
    其中第一行为 ```suggestion 语言:<lang> 头（与 pr-agent 一致）.
    例:
        ```suggestion:-0
        def f():
            return 1
        ```
"""
from __future__ import annotations

import re
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from reviewagent.commands._common import BaseCommand, BaseCommandError
from reviewagent.commands._common import publish_overview
import subprocess
import hashlib as _hashlib
from reviewagent.config import config


def _suggestion_fingerprint(file_path: str, line: int, header: str | None) -> str:
    """Stable fingerprint for suggestion dedup.

    基于 (file:line:header_normalized) 三元组 — 同位置同类问题永远同 fp,
    与 existing_code 行数变化无关 (之前用 sha256(existing_code) 会被 LLM
    给的 1~N 行范围差绕过). lower-case header normalize 让大小写不敏感.

    Args:
        file_path: suggestion 文件路径.
        line: new_line (建议应用的行).
        header: suggestion header (raw, 含大小写/空格都会被 normalize).

    Returns:
        24 字符 hex fingerprint. 与 _suggestion_dedup_key 保持一致.
    """
    hdr_norm = (header or "").strip().lower()
    return _hashlib.sha256(
        f"{file_path}:{line}:{hdr_norm}".encode("utf-8")
    ).hexdigest()[:24]


def _suggestion_content_fingerprint(existing_code: str) -> str:
    """基于 existing_code normalize 后的内容指纹 (16 字符 hex).

    Why 不依赖 line: Apply 一个 suggestion 会让后续行号 +N, 同一行代码
    物理位置变化但内容不变 → 行号 fingerprint 失效, 内容 fingerprint 命中.

    normalize: strip + 折叠多空白为单空格. 缩进差异视为同代码 (避免 LLM
    给的 leading-trim 噪声破坏 fingerprint).

    与 _suggestion_fingerprint 互补:
      - _suggestion_fingerprint: 同 file:line 同类问题 (行号敏感)
      - _suggestion_content_fingerprint: 同 existing_code 内容 (行号不敏感)
    """
    import re as _cf_re
    code_norm = _cf_re.sub(r"\s+", " ", (existing_code or "").strip())
    return _hashlib.sha256(code_norm.encode("utf-8")).hexdigest()[:16]
from reviewagent.git.diff_lines import (
    find_nearest_valid_line,
    format_line_map_for_prompt,
    parse_diff_line_map,
)
from reviewagent.gitlab.client import GitLabError
from reviewagent.logging_setup import logger
from reviewagent.llm import OpencodeOutputError, get_client


# Backward-compat re-exports
ImproveError = BaseCommandError

# 规则引用正则: 匹配 SSD-RULE-XXX 形式 (rule_key_prefix 可配)
# 用于从 rationale/header 中提取所有规则键 (含 R-XXX, R-OTHER:* 等)
_RULE_REF_REGEX = re.compile(
    r"\b(?:SSD-RULE-\w+"
    r"|R-OTHER-IMPACT:[a-z0-9_]+"
    r"|R-OTHER:[a-z0-9_]+"
    r"|R-[A-Z]+(?:-[A-Z0-9_]+)*)\b"
)
# 豁免正则: 仅匹配 {prefix}-RULE-* 形式的规则 (如 SSD-RULE-XXX)
# 用于评分/严重度过滤的豁免判断 — 只有仓库自定义规则才豁免, R-OTHER 等通用规则不豁免
_RULE_EXEMPT_REGEX = re.compile(
    rf"\b{re.escape(config.rule_key_prefix)}-RULE-\w+\b"
)




def _handle_cohort_reoccurrence(
    *,
    store,
    project_id: int,
    mr_iid: int,
    cohort_key: str,
    new_note_id: str,
    file_path: str,
    target_line: int,
    target_line_end: int,
    existing: str,
    head_sha: str,
    gitlab,
) -> None:
    """Batch3: 同一 cohort 上一代被 applied/dismissed 但问题又复现, supersede 旧记录.

    判定: 同 cohort_key 最新一条 (排除 new_note_id 自身) 处于 applied/dismissed,
    且当前 head_sha 下的文件里 existing_code 仍然出现 (说明问题没真修),
    则 supersede 旧记录, generation+1.
    否则不动 — 让正常的 dedup 走.

    Why: MR 249 误分类根因之一是 V1 applied + V2 open 同时存在, 汇总双计.
    supersede 后 V1 不再被 list_latest_by_cohort 取到, V2 才是唯一代表.
    """
    if not cohort_key:
        return
    prior = store.get_latest_in_cohort_excluding(
        project_id=project_id, mr_iid=mr_iid,
        cohort_key=cohort_key, exclude_note_id=new_note_id,
    )
    if not prior:
        return
    if prior.get("state") not in ("applied", "dismissed"):
        return
    # 校验问题是否真的还在: 当前 head_sha 的文件里 existing 仍存在
    if not existing:
        return
    try:
        current_content = gitlab.get_file_at_sha(
            project_id, file_path, head_sha,
        )
    except Exception:  # noqa: BLE001
        return
    if current_content is None:
        return
    if existing.strip() not in current_content:
        # 问题已不存在, 不 supersede — 走正常 dedup 路径会跳过
        return
    # supersede 旧记录, bump generation
    new_gen = int(prior.get("cohort_generation") or 1) + 1
    store.supersede_suggestion(
        old_note_id=prior["note_id"],
        new_note_id=new_note_id,
        generation=new_gen,
    )
    logger.info(
        "improve.cohort_reoccurrence project={} mr={} cohort={} "
        "old_note={} state={} -> superseded by new_note={} generation={}",
        project_id, mr_iid, cohort_key,
        prior["note_id"][:8], prior.get("state"),
        new_note_id[:8], new_gen,
    )


class ImproveCommand(BaseCommand):
    COMMAND_NAME = "improve"
    DEFAULT_AGENT = "improve"

    # 每条 suggestion 末尾追加 /adopt /dismiss 帮助文本 (与 pr-agent 一致)
    # 末段提醒用户用 MR 评论 + 命令标记处理, 避免直接点「解决主题」或 ✓ 图标关闭 thread — 这两条都会被
    # GitLab 归类为「已关闭(未分类)」, /adopt /dismiss 检测不到, telemetry 缺失处理数据.
    HELP_TEXT_FOOTER = (
        "\n\n✅ 接受建议\n"
        "   • 直接用：点上方「应用建议」按钮\n"
        "   • 自己改：请先提交修改，再回复 `/adopt [理由]`\n"
        "\n❌ 关闭建议\n"
        "   • 回复 `/dismiss [理由]`\n"
        "\n💡 为保证数据采集的正确性，请优先使用以上方式来处理MR建议; "
        "非必要不要直接点「解决主题」或 ✓ 图标来关闭主题."
    )
    # ---------- 并行分块调用 ----------
    def _call_agent(self, ws, *, overflow_files: list[str] | None = None) -> dict[str, Any]:
        """覆盖基类: 按文件分块 + 并行调 opencode + 合并结果.

        V6 新增:
        - overflow_files: 来自 MAX_DIFF_CHARS 截断的文件列表 (强制 patch-only 模式)
        - 测试路径过滤 (improve_skip_test_paths)
        - 文件优先级排序 (improve_keyword_paths 加分)
        - 4 档 strategy 分配 (full / partial / patch / skip)
        - _build_chunk_prompt 根据 strategy 切换 source_block (full/partial/patch)
        """
        overflow_set: set[str] = set(overflow_files or [])
        line_map = self._diff_line_map()
        all_files = sorted(line_map.keys())
        new_files = self._new_files_from_diff(ws.diff_file) if getattr(ws, "diff_file", None) else set()
        self._review_mode = "business"  # 默认值, 后续 auto 判断可能覆盖

        # 文件扩展名过滤: 跳过 .md/.doc/.png 等非代码文件
        excluded_ext = set(config.review_exclude_extensions)
        if excluded_ext:
            code_files = [f for f in all_files if not any(f.lower().endswith(ext) for ext in excluded_ext)]
            if len(code_files) < len(all_files):
                skipped_ext = set(all_files) - set(code_files)
                logger.info(
                    "improve.file_filter project={} mr={} skipped={}",
                    self.project_id, self.mr_iid, sorted(skipped_ext),
                )
            all_files = code_files

        if not all_files:
            return {"summary_md": "## 改进总览\n\n无代码文件变更，跳过检视。", "suggestions": []}

        # V7 检视模式判断: auto 按 diff 测试文件占比自动切 business/test
        review_mode = config.improve_review_mode
        if review_mode == "auto":
            from pathlib import Path as _P
            _test_pats = config.improve_skip_test_paths
            _test_count = sum(
                1 for fp in all_files
                if any(p in fp or _P(fp).name.startswith(p.rstrip("/")) for p in _test_pats)
            )
            review_mode = "test" if _test_count > len(all_files) * 0.5 else "business"
            logger.info(
                "improve.review_mode_auto project={} mr={} test_files={} total={} -> {}",
                self.project_id, self.mr_iid, _test_count, len(all_files), review_mode,
            )
        self._review_mode = review_mode

        # V6 测试路径过滤 (路径级别, 不与扩展名过滤冲突)
        # V7: test 模式下 IMPROVE_SKIP_TEST_PATHS 仍生效 (D 配置: 测试代码即业务代码, 但 conftest.py 等共享 fixture 文件仍跳过)
        test_skipped: list[str] = []
        skip_test_patterns = config.improve_skip_test_paths
        if skip_test_patterns:
            from pathlib import Path as _P
            filtered: list[str] = []
            for fp in all_files:
                name = _P(fp).name
                if any(pat in fp or name.startswith(pat.rstrip("/")) for pat in skip_test_patterns):
                    test_skipped.append(fp)
                else:
                    filtered.append(fp)
            if test_skipped:
                logger.info(
                    "improve.test_path_filter project={} mr={} skipped={}",
                    self.project_id, self.mr_iid, sorted(test_skipped),
                )
            all_files = filtered

        # C: 自动 case / 测试文件抽样检视 (IMPROVE_TEST_SAMPLE_MAX>0 启用)
        _sample_paths = config.improve_test_sample_paths
        _sample_max = config.improve_test_sample_max
        if _sample_paths and _sample_max > 0:
            all_files, _unsampled = self._sample_case_files(all_files, _sample_paths, _sample_max)
            if _unsampled:
                test_skipped.extend(_unsampled)
                logger.info(
                    "improve.test_file_sampling project={} mr={} total_case={} sampled={} skipped={}",
                    self.project_id, self.mr_iid, len(_unsampled) + _sample_max, _sample_max, len(_unsampled),
                )

        if not all_files:
            return {
                "summary_md": "## 改进总览\n\n无代码文件变更，跳过检视。" if not test_skipped
                              else f"## 改进总览\n\n> ⏭️ 以下 {len(test_skipped)} 个测试/配置文件跳过检视: {', '.join(test_skipped[:10])}{'...' if len(test_skipped) > 10 else ''}",
                "suggestions": [],
            }

        # V7 文件优先级打分 (按 score 倒序排序)
        # 信号: 改动大小(log1p不封顶) + 关键路径 + 改动密度 + 测试特征密度
        file_meta: dict[str, tuple[int, int]] = {}
        for fp in all_files:
            diff_size = len(line_map.get(fp, set()))
            file_size = self._read_file_line_count(fp, ws)
            file_meta[fp] = (diff_size, file_size)

        # 预算测试特征密度 (test 模式下才计入权重, 但所有模式都算 — log 用)
        test_feature_cache: dict[str, float] = {}
        if config.improve_priority_weight_test_feature > 0:
            for fp in all_files:
                test_feature_cache[fp] = self._test_feature_density(fp, file_meta[fp][1])

        def _priority(fp: str) -> float:
            diff_size, file_size = file_meta[fp]
            # 改动大小: log1p 单调递增不封顶, 大改动永远比小改动高分
            score = math.log1p(diff_size) * config.improve_priority_weight_diff
            # 关键路径加分 (降到 25, 不再碾压 diff_size)
            fp_stripped = fp.lstrip("/")
            if any(kw.lstrip("/") in fp or kw in fp_stripped for kw in config.improve_keyword_paths):
                score += config.improve_priority_weight_keyword
            # 改动密度: 改动占比 > 30% → 高风险 (整文件重写级别)
            if file_size > 0 and (diff_size / file_size) > 0.3:
                score += config.improve_priority_weight_density
            # 测试特征密度: test 模式生效, business 模式权重=0
            if config.improve_priority_weight_test_feature > 0:
                score += test_feature_cache.get(fp, 0.0) * config.improve_priority_weight_test_feature
            # A: 新增文件优先级加分 (自然吃满 IMPROVE_FULL_FILES 配额 → full 源码)
            if config.improve_new_file_full and fp in new_files:
                score += config.improve_priority_weight_keyword
            return score

        sorted_files = sorted(all_files, key=_priority, reverse=True)

        # V7 strategy 分配 (full / partial / patch) — 抽成独立方法
        strategy = self._assign_strategy(sorted_files, file_meta, overflow_set)

        files = list(sorted_files)  # 所有文件都进 LLM (策略降级但不丢)
        skipped_files = list(test_skipped)  # 测试/配置文件不进 LLM
        cap_truncated: list[str] = []  # D: 文件数硬上限截断的低优先级文件 (与 test_skipped 分开记)

        # 记录 strategy 分布 (metrics + log)
        from reviewagent.metrics import inc as _metric_inc
        strat_counts = {"full": 0, "partial": 0, "patch": 0}
        for s in strategy.values():
            strat_counts[s] = strat_counts.get(s, 0) + 1
        _metric_inc(
            "reviewagent_improve_strategy_total",
            project_id=str(self.project_id),
            mr_iid=str(self.mr_iid),
            strategy="full", amount=float(strat_counts["full"]),
        )
        _metric_inc(
            "reviewagent_improve_strategy_total",
            project_id=str(self.project_id),
            mr_iid=str(self.mr_iid),
            strategy="partial", amount=float(strat_counts["partial"]),
        )
        _metric_inc(
            "reviewagent_improve_strategy_total",
            project_id=str(self.project_id),
            mr_iid=str(self.mr_iid),
            strategy="patch", amount=float(strat_counts["patch"]),
        )
        logger.info(
            "improve.strategy project={} mr={} total={} full={} partial={} patch={} test_skip={}",
            self.project_id, self.mr_iid, len(files),
            strat_counts["full"], strat_counts["partial"], strat_counts["patch"],
            len(test_skipped),
        )

        if len(files) <= 1 and not skipped_files:
            return super()._call_agent(ws)  # 单文件走原路径

        # 按文件拆分 diff
        diff_by_file = self._split_diff_by_file(ws.diff_file, files)

        # V9 增量复用: 按文件内容指纹 (blob sha) 复用, 取代整 MR head_sha.
        # 原因: apply 到文件 A → push 新 commit → 整 MR head 变 → head_sha 复用对 ALL 文件
        # 失效 (每轮全检, 在 apply 循环里收益=0). blob sha 只在文件内容真变时才变
        # → 仅重检 A, 复用 B/C/D. shallow fetch 不靠 git 历史, 直接读 worktree 算 sha1.
        _publish_head_sha = self._get_mr_head_sha() or ""
        files_to_review: list[str] = []
        files_reuse: list[str] = []
        file_states: dict[str, dict] = {}
        # 当前每文件 blob (从 worktree 读 raw bytes + sha1; 始终算, 供末尾 upsert)
        cur_blobs: dict[str, str | None] = {fp: self._file_blob_sha(fp, ws) for fp in files}
        if _publish_head_sha:
            from reviewagent.telemetry.store import get_store
            try:
                file_states = get_store().get_file_review_states(self.project_id, self.mr_iid)
            except Exception as e:
                logger.warning("improve.incremental_lookup_failed (non-fatal, full review): {}", e)
                file_states = {}

            if file_states:
                # 内容变了的文件的 diff (跨文件影响检测: unchanged 文件标识符出现在
                # changed diff → 强制重检, 防 "B 没变但被 A 牵动" 漏检)
                changed_diffs = {
                    fp: diff_by_file.get(fp, "") for fp in files
                    if cur_blobs.get(fp) != (file_states.get(fp) or {}).get("blob_sha")
                }
                changed_text = "\n".join(changed_diffs.values())

                for fp in files:
                    prev = file_states.get(fp)
                    prev_blob = (prev or {}).get("blob_sha") if prev else None
                    if prev_blob and cur_blobs.get(fp) == prev_blob:
                        # 内容未变 → 跨文件影响检测
                        idents = set(self._extract_identifiers(diff_by_file.get(fp, "")))
                        cross_impact = any(
                            ident in changed_text for ident in idents if len(ident) >= 3
                        ) if changed_text else False
                        if cross_impact:
                            files_to_review.append(fp)  # 强制重检
                        else:
                            files_reuse.append(fp)  # 复用上轮
                    else:
                        files_to_review.append(fp)  # 内容变了 / 首次 / blob 未知 → 重检

                if files_reuse:
                    logger.info(
                        "improve.incremental project={} mr={} total={} review={} reuse={}",
                        self.project_id, self.mr_iid,
                        len(files), len(files_to_review), len(files_reuse),
                    )
                    from reviewagent.metrics import inc as _metric_inc
                    _metric_inc(
                        "reviewagent_improve_incremental_reuse_total",
                        project_id=str(self.project_id), mr_iid=str(self.mr_iid),
                        amount=float(len(files_reuse)),
                    )
            else:
                files_to_review = list(files)
        else:
            files_to_review = list(files)  # 拿不到 head_sha, 全检

        # D: 文件数硬上限 (IMPROVE_MAX_FILES_TO_REVIEW) — 只截断送 LLM 的集合, 复用文件不计数
        _max_files = config.improve_max_files_to_review
        if _max_files > 0 and len(files_to_review) > _max_files:
            files_to_review, _truncated = self._apply_file_count_cap(
                files_to_review, new_files, config.improve_keyword_paths, _max_files,
            )
            if _truncated:
                cap_truncated.extend(_truncated)
                logger.info(
                    "improve.file_count_cap project={} mr={} max={} kept={} truncated={}",
                    self.project_id, self.mr_iid, _max_files, len(files_to_review), len(_truncated),
                )

        # 全局一次 rg 找所有 diff 文件的跨文件 caller 引用 (替代 per-file 重复 rg)
        # V8: 只对需重检的文件做 rg (reuse 文件不调 LLM, 不需要)
        wt = str(ws.worktree)
        cross_file_refs_by_file = self._collect_cross_file_refs_for_mr(files_to_review, diff_by_file, wt)

        # V9: 从 store 取复用文件上轮的 open suggestions (blob 相同 → 仍有效)
        # 只取 state=open (applied/dismissed 不重显); head_sha 用该文件上次检视的 head
        # (blob 相同 → 内容字节相同 → 行号必然仍有效)
        # L4: 同时取待重检文件 (blob 变了) 的 prior open issues 作 prompt 锚点
        # → LLM 确认而非从零重推, 省 output token + 跨轮连续; 行号已偏移, 让 LLM 重定位
        reuse_suggestions: list[dict[str, Any]] = []
        prior_issues_by_file: dict[str, list[dict[str, Any]]] = {}
        _need_load = bool(files_reuse) or (bool(files_to_review) and bool(file_states))
        if _need_load:
            from reviewagent.telemetry.store import get_store
            try:
                _store = get_store()
                rows = _store.list_suggestions(
                    project_id=self.project_id, mr_iid=self.mr_iid, limit=500,
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
                        self.project_id, self.mr_iid, len(files_reuse), len(reuse_suggestions),
                    )
                if prior_issues_by_file:
                    logger.info(
                        "improve.prior_anchors project={} mr={} files={} anchors={}",
                        self.project_id, self.mr_iid,
                        len(prior_issues_by_file),
                        sum(len(v) for v in prior_issues_by_file.values()),
                    )
            except Exception as e:
                logger.warning("improve.reuse_load_failed (non-fatal): {}", e)
                # 复用失败 → 降级为全检; 锚点失败 → 跳过锚点 (LLM 自行重推, 无害)
                if files_reuse:
                    files_to_review = list(files)
                    files_reuse = []

        # V9: 计算各文件 priority, 透传给 _merge_chunks 做建议预算加权 (高优先级文件更多槽位)
        _file_priorities = {fp: _priority(fp) for fp in files}

        # 并行调用 (只调需重检的文件)
        # V9: files_to_review 为空 + files_reuse 非空 → 全复用, 不兜底
        if not files_to_review and files_reuse:
            logger.info("improve.all_reused project={} mr={} — skip LLM entirely", self.project_id, self.mr_iid)
            # V9: 记录本轮各文件状态 (全复用, blob 不变, count=复用建议数)
            _counts = {}
            for s in reuse_suggestions:
                fp = s.get("file", "")
                if fp:
                    _counts[fp] = _counts.get(fp, 0) + 1
            self._upsert_file_review_states(files, ws, _publish_head_sha, _counts)
            return self._merge_chunks(
                [{"summary_md": "", "suggestions": reuse_suggestions}],
                skipped_files=skipped_files,
                file_priorities=_file_priorities,
                new_files=new_files,
                cap_truncated=cap_truncated,
            )
        files_llm = files_to_review if files_to_review else list(files)  # 兜底: 拿不到sha或首次
        workers = min(len(files_llm), config.improve_parallel_workers)

        logger.info(
            "improve.parallel project={} mr={} files_to_review={} reuse={} workers={}",
            self.project_id, self.mr_iid, len(files_llm), len(files_reuse), workers,
        )

        chunk_results: list[dict[str, Any]] = []
        total_prompt_tokens = 0
        total_completion_tokens = 0
        total_cost_credits = 0.0
        last_model = ""
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {}
            for fp in files_llm:
                file_diff = diff_by_file.get(fp, "")
                valid_lines = line_map.get(fp, set())
                prompt = self._build_chunk_prompt(
                    fp, file_diff, valid_lines, ws,
                    cross_file_refs=cross_file_refs_by_file.get(fp, []),
                    strategy=strategy.get(fp, "full"),  # V6: 4 档 strategy 透传
                    prior_issues=prior_issues_by_file.get(fp),  # L4 锚点
                )
                fut = pool.submit(self._call_chunk, prompt, ws, fp)
                futures[fut] = fp
            for fut in as_completed(futures):
                fp = futures[fut]
                try:
                    oc_result = fut.result()
                    chunk_results.append(oc_result.data)
                    total_prompt_tokens += oc_result.prompt_tokens
                    total_completion_tokens += oc_result.completion_tokens
                    total_cost_credits += oc_result.cost_credits
                    if oc_result.model:
                        last_model = oc_result.model
                except Exception as e:
                    logger.error(
                        "improve.chunk_failed project={} mr={} file={} err={}",
                        self.project_id, self.mr_iid, fp, e,
                    )
                    # 单个 chunk 失败不影响其他
                    chunk_results.append({"summary_md": "", "suggestions": []})

        # 汇总 token 统计到 _last_oc_result (主线程安全写入)
        self._last_oc_result = type(self)._make_token_summary(
            total_prompt_tokens, total_completion_tokens, last_model, total_cost_credits
        )

        # V8: 把复用的 suggestions 也合并进结果 (作为额外的 "chunk result")
        if reuse_suggestions:
            chunk_results.append({"summary_md": "", "suggestions": reuse_suggestions})

        # V9: 记录本轮各文件状态 (LLM 产出 + 复用), 供下一轮按文件粒度复用
        _counts: dict[str, int] = {}
        for cr in chunk_results:
            for s in (cr.get("suggestions") or []):
                fp = s.get("file", "")
                if fp:
                    _counts[fp] = _counts.get(fp, 0) + 1
        self._upsert_file_review_states(files, ws, _publish_head_sha, _counts)

        # skipped_files 已含 test_skipped (上文 skipped_files = list(test_skipped))
        # V6 软阈值化后不再有"限额 overflow 丢弃"——overflow 文件降级为 patch 仍进 LLM
        return self._merge_chunks(
            chunk_results,
            skipped_files=skipped_files,
            file_priorities=_file_priorities,
            new_files=new_files,
            cap_truncated=cap_truncated,
        )

    def _call_chunk(self, prompt: str, ws, file_path: str) -> dict[str, Any]:
        """单个 chunk 的 opencode 调用."""
        logger.info(
            "improve.chunk_start project={} mr={} file={}",
            self.project_id, self.mr_iid, file_path,
        )
        client = get_client()
        oc_result = client.run(
            agent=self.DEFAULT_AGENT,
            prompt=prompt,
            workdir=ws.worktree,
            files=[],  # 不内联文件，prompt 里已包含 diff
            timeout=config.rq_worker_timeout,
        )
        logger.info(
            "improve.chunk_done project={} mr={} file={} tokens_in={} tokens_out={}",
            self.project_id, self.mr_iid, file_path,
            oc_result.prompt_tokens, oc_result.completion_tokens,
        )
        # 返回完整 oc_result (含 token 统计), 在 _merge_chunks 中汇总
        return oc_result

    @staticmethod
    def _split_diff_by_file(diff_file: Path, files: list[str]) -> dict[str, str]:
        """解析 unified diff，按文件拆分. 返回 {file_path: diff_text}."""
        try:
            full_diff = diff_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return {}

        result: dict[str, str] = {}
        # 按 "diff --git" 分割
        parts = re.split(r"(?=^diff --git )", full_diff, flags=re.MULTILINE)
        for part in parts:
            if not part.strip():
                continue
            # 提取文件路径: "diff --git a/xxx b/xxx"
            m = re.match(r"diff --git a/.+ b/(.+)", part)
            if m:
                fp = m.group(1).strip()
                result[fp] = part
        return result

    @staticmethod
    def _extract_identifiers(diff: str) -> list[str]:
        """从 diff 的 `+` 行提取改动的标识符 (def/class/常量/import 名字)."""
        if not diff:
            return []
        # 只看 + 行
        added = "\n".join(ln[1:] for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++"))
        idents: set[str] = set()
        for m in re.finditer(r"^\s*def\s+([A-Za-z_]\w{2,})\s*\(", added, re.MULTILINE):
            idents.add(m.group(1))
        for m in re.finditer(r"^\s*class\s+([A-Za-z_]\w{2,})", added, re.MULTILINE):
            idents.add(m.group(1))
        for m in re.finditer(r"^\s*([A-Z][A-Z0-9_]{2,})\s*[:=]", added, re.MULTILINE):
            idents.add(m.group(1))
        # `from X import A` (单 ident)
        for m in re.finditer(r"^\s*from\s+\S+\s+import\s+([A-Za-z_]\w{2,})\s*(?:$|#)", added, re.MULTILINE):
            idents.add(m.group(1))
        # `from X import A, B, C` (多 ident, 不带括号)
        # 匹配 import 后第一个 ident 后所有逗号分隔 ident
        for m in re.finditer(r"^\s*from\s+\S+\s+import\s+([A-Za-z_]\w{2,})((?:\s*,\s*[A-Za-z_]\w{2,})*)", added, re.MULTILINE):
            idents.add(m.group(1))
            for sub in re.findall(r"\s*,\s*([A-Za-z_]\w{2,})", m.group(2)):
                idents.add(sub)
        # `from X import (A, B)` (括号多 ident)
        for m in re.finditer(r"^\s*from\s+\S+\s+import\s+\(([^)]+)\)", added, re.MULTILINE):
            for part in m.group(1).split(","):
                name = part.strip().split(" as ")[0].strip()
                if len(name) >= 3:
                    idents.add(name)
        # 排除常见噪音
        stop = {"self", "cls", "None", "True", "False", "args", "kwargs"}
        return sorted(idents - stop)[:15]

    @staticmethod
    def _find_cross_file_refs(file_path: str, diff: str, worktree_path, max_refs: int = 6) -> list[dict]:
        """在 worktree 找 diff 改动的标识符在其他文件的 caller 引用.

        用 rg (ripgrep) 排除当前文件, 每个 ident 限 2 条命中, 总数限 max_refs.
        返回 [{"file": rel, "line": N, "content": "..."}, ...]
        """
        from pathlib import Path
        idents = ImproveCommand._extract_identifiers(diff)
        if not idents or not worktree_path:
            return []
        workdir = str(Path(worktree_path))
        refs: list[dict] = []
        seen: set[tuple[str, int]] = set()
        rel_self = file_path
        for ident in idents:
            if len(ident) < 3:
                continue
            try:
                result = subprocess.run(
                    ["rg", "-n", "--no-heading", "-t", "py",
                     rf"\b{ident}\b", ".",
                     "-g", f"!{rel_self}",
                     "-g", "!reviewagent/**",
                     "-g", "!tests/**",
                     "-g", "!.venv/**",
                     "-g", "!**/__pycache__/**",
                     "-g", "!.git/**"],
                    cwd=workdir,
                    capture_output=True, text=True, timeout=8,
                )
            except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
                continue
            hits = 0
            for line in result.stdout.splitlines():
                parts = line.split(":", 2)
                if len(parts) < 3:
                    continue
                f, ln_s, content = parts[0], parts[1], parts[2]
                f_rel = f.lstrip("./")
                if f_rel == rel_self or not f_rel:
                    continue
                try:
                    ln = int(ln_s)
                except ValueError:
                    continue
                key = (f_rel, ln)
                if key in seen:
                    continue
                seen.add(key)
                refs.append({
                    "file": f_rel,
                    "line": ln,
                    "content": content.strip()[:500],
                    "ident": ident,
                })
                hits += 1
                if hits >= 2 or len(refs) >= max_refs:
                    break
            if len(refs) >= max_refs:
                break
        return refs

    def _collect_cross_file_refs_for_mr(
        self, files: list[str], diff_by_file: dict[str, str], worktree_path, max_refs_per_file: int = 6
    ) -> dict[str, list[dict]]:
        """对整个 MR 一次性 rg 找所有 diff 文件的标识符在仓库的 caller 引用.

        Returns: {file_path: [{file, line, content, ident}, ...]}
        - 一次 rg 用所有 ident 的并集作为 pattern, 避免每个文件重启 subprocess
        - 按 ident 分桶, 给每个 diff file 排除自身的 caller
        - 比 per-file 调用更全: file A 的 ident 在 file B 的 caller 也会被发现 (反之亦然)
        """
        from pathlib import Path
        if not worktree_path:
            return {fp: [] for fp in files}
        workdir = str(Path(worktree_path))

        # 1. 收集所有 ident (per file)
        idents_by_file: dict[str, set[str]] = {}
        all_idents: set[str] = set()
        for fp in files:
            idents = set(self._extract_identifiers(diff_by_file.get(fp, "")))
            idents_by_file[fp] = idents
            all_idents |= idents
        # 排除短 ident + 常见噪音
        stop = {"self", "cls", "None", "True", "False", "args", "kwargs"}
        all_idents = {i for i in all_idents if len(i) >= 3} - stop
        if not all_idents:
            return {fp: [] for fp in files}

        # 2. 一次 rg 找所有 ident (按 ident 分类)
        idents_sorted = sorted(all_idents)
        import re as _re
        pattern = r"\b(?:" + "|".join(_re.escape(i) for i in idents_sorted) + r")\b"
        try:
            result = subprocess.run(
                ["rg", "-n", "--no-heading", "-t", "py", pattern, ".",
                 "-g", "!reviewagent/**",
                 "-g", "!tests/**",
                 "-g", "!.venv/**",
                 "-g", "!**/__pycache__/**",
                 "-g", "!.git/**"],
                cwd=workdir,
                capture_output=True, text=True, timeout=30,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, Exception) as e:
            logger.warning("improve.cross_file_global_rg failed: {}", e)
            return {fp: [] for fp in files}

        # 3. 解析: 按 ident 分桶
        refs_by_ident: dict[str, list[dict]] = {i: [] for i in all_idents}
        for line in result.stdout.splitlines():
            parts = line.split(":", 2)
            if len(parts) < 3:
                continue
            f, ln_s, content = parts[0], parts[1], parts[2]
            f_rel = f.lstrip("./")
            if not f_rel:
                continue
            try:
                ln = int(ln_s)
            except ValueError:
                continue
            # 一行只算一个 ident 命中 (按 ident 顺序优先)
            for ident in idents_sorted:
                if _re.search(rf"\b{_re.escape(ident)}\b", content):
                    refs_by_ident[ident].append({
                        "file": f_rel,
                        "line": ln,
                        "content": content.strip()[:500],
                        "ident": ident,
                    })
                    break

        # 4. 给每个 diff file 分配 refs (排除自身, 每个 ident 限 2, 总限 max_refs)
        out: dict[str, list[dict]] = {}
        for fp in files:
            my_idents = idents_by_file.get(fp, set())
            seen: set[tuple[str, int]] = set()
            refs: list[dict] = []
            for ident in my_idents:
                hits = 0
                for r in refs_by_ident.get(ident, []):
                    if r["file"] == fp:
                        continue
                    key = (r["file"], r["line"])
                    if key in seen:
                        continue
                    seen.add(key)
                    refs.append(r)
                    hits += 1
                    if hits >= 2 or len(refs) >= max_refs_per_file:
                        break
                if len(refs) >= max_refs_per_file:
                    break
            out[fp] = refs
        return out

    def _render_cross_file_section(self, refs: list[dict]) -> str:
        """把 cross-file 引用渲染成 chunk prompt 段."""
        if not refs:
            return (
                "## 🟠 优先 1 — 跨文件影响分析 (规则检查之前必做, **P1**)\n\n"
                "Python 端 rg 没找到 cross-file caller 引用 (本文件可能是新增 / 改动是局部 / 仓库无其他文件引用).\n\n"
                "**仍请按 P1 优先级自行判断是否需要产 R-OTHER-IMPACT suggestion** — Python 端仅做粗扫，"
                "如下情况仍可能有跨文件风险:\n"
                "- 新增 / 重命名的公共函数、类、常量 (无 caller 但下游模块可能依赖)\n"
                "- 改了 fixture / 测试 helper (本 diff 未引用但其它 test 可能引用)\n"
                "- 改了 import 路径 (旧路径可能还有引用未被发现)\n"
                "- 改了 SQL / ORM schema (model/migration 可能未同步)\n\n"
                "确认无风险 → 在 summary_md 注明 '未发现 cross-file 关联'。**不要为凑数硬编 R-OTHER-IMPACT**。\n\n"
            )
        section = "## 🟠 优先 1 — 跨文件影响分析 (Python 端已 grep, **P1, 在所有规则检查之前**)\n\n"
        section += f"在 worktree 找到 {len(refs)} 条其他文件对本文件改动的引用:\n\n"
        for ref in refs:
            section += (
                f"**`{ref['file']}:{ref['line']}`** 引用 `{ref['ident']}`:\n"
                f"```\n{ref['content']}\n```\n\n"
            )
        section += (
            "**请逐一分析每条 caller 是否需要同步更新**：\n"
            "- 函数签名变了但 caller 没传新参数 / 类型不匹配 → 产 suggestion, `label: cross-file impact`\n"
            "- 常量改了引用方没同步 → 产 suggestion, `label: cross-file impact`\n"
            "- 删除/重命名的函数别处还在用 → 写进 summary_md 文字, 格式 `> 跨文件影响: <文件> L<行号> <问题>`\n"
            "- 跨文件影响类问题**不要求先命中 R-XXX 20 类**, 命中即产 suggestion\n"
            "- `rationale` 必须以 `R-OTHER-IMPACT:<简短描述>` 开头 (例 `R-OTHER-IMPACT:caller_param` / `R-OTHER-IMPACT:schema_drift` / `R-OTHER-IMPACT:import_path` / `R-OTHER-IMPACT:fixture_break`)\n"
            "- **严格只用于跨文件影响** — 同文件内的资源/异常/循环问题应归 R-RES / R-LOOP / R-ERR 等, 不要错归 R-OTHER-IMPACT\n\n"
        )
        return section

    def _build_chunk_prompt(
        self, file_path: str, file_diff: str, valid_lines: set[int], ws,
        cross_file_refs: list[dict] | None = None,
        *, strategy: str = "full",
        prior_issues: list[dict] | None = None,
    ) -> str:
        """构建单文件的精简 prompt.

        cross_file_refs: 预计算的跨文件 caller 引用列表 (来自 _call_agent 一次全局 rg).
                         None 时回退到 per-file _find_cross_file_refs (单文件走基类路径时用).
        strategy (V6): "full" / "partial" / "patch" — 决定 source_block 详略.
                       full   = 完整源码 (≤5000 行)
                       partial = diff 行 ±N 行 context (overflow 文件兜底)
                       patch  = 仅 diff + 行号 (最弱, 行内识别 only)
        prior_issues (L4): 上一轮该文件的 open suggestions 锚点, 让 LLM 确认而非
                           从零重推, 省 output token + 跨轮连续 (无 flicker).
                           行号已因改动偏移, 仅作语义锚点, 由 LLM 重定位.
        """
        wt = str(ws.worktree)

        if strategy == "full":
            source_block = self._build_full_source_block(file_path)
        elif strategy == "partial":
            source_block = self._build_partial_context_block(file_path, valid_lines, ws)
        elif strategy == "patch":
            source_block = self._build_patch_only_block(file_path, valid_lines)
        else:
            raise ValueError(f"unknown strategy: {strategy}")


        # VALID NEW LINES
        vl_sorted = sorted(valid_lines)
        vl_str = f"{file_path}: {vl_sorted}"

        # 通用规则清单 — **inline 进 chunk prompt**, 不再引用 system prompt.
        # Why: chunk prompt 跟 system prompt 分开发给 LLM, 引用式提示会被
        #      LLM 跳过或被长上下文稀释注意力 → R-XXX 20 类基本不被识别.
        #      把清单 inline 后, LLM 能直接看到每条规则的命中条件.
        from reviewagent.prompts.loader import load_block as _load_block
        _general_rules_block = _load_block("_general_rules_block")

        rules_block = ""
        if self.repo_context:
            rules_block = (
                f"## 🔴 优先 2 — SSD 自定义规则 (项目方定义)\n\n"
                f"先扫下面 SSD 规则命中, 命中即产 suggestion, rationale 引用规则键:\n\n"
                f"{self.repo_context}\n\n"
                f"---\n\n"
                f"{_general_rules_block}\n\n"
            )
        else:
            # 无 SSD 规则时也要给 LLM 通用规则清单
            rules_block = _general_rules_block + "\n\n"

        # 跨文件 caller 引用: 优先用 _call_agent 预计算的全局结果, fallback 到 per-file 调用
        if cross_file_refs is None:
            cross_file_refs = self._find_cross_file_refs(file_path, file_diff, wt)
        cross_file_section = self._render_cross_file_section(cross_file_refs)

        # L4: 已知问题锚点 — 让 LLM 确认而非重推, 聚焦新问题
        prior_section = ""
        if prior_issues:
            _anchors = []
            for i, pi in enumerate(prior_issues, 1):
                sev = (pi.get("severity") or "medium").lower()
                hdr = (pi.get("header") or pi.get("rationale") or "").strip()
                lbl = (pi.get("label") or "").strip()
                _anchors.append(f"{i}. [{sev}] {hdr}" + (f" (label={lbl})" if lbl else ""))
            prior_section = (
                "## 已知问题锚点（上一轮已发现且仍 open；行号可能因改动偏移）\n\n"
                "以下问题在上轮检视中已发现。请：\n"
                "- 逐条核对在 **当前代码** 中是否仍成立：成立 → 保留并重新定位 start_line；不成立 → 不报\n"
                "- **不要仅限于此清单** — 继续主动发现清单之外的新问题\n\n"
                + "\n".join(_anchors) + "\n\n---\n\n"
            )

        return (
            f"{cross_file_section}"
            f"{rules_block}"
            f"{prior_section}"
            f"## 本次检视文件: `{file_path}`\n\n"
            f"### diff\n```diff\n{file_diff}\n```\n\n"
            f"{source_block}\n\n"
            f"### VALID NEW LINES（start_line 只能从此取）\n\n{vl_str}\n\n"
            f"## 输出\n\n"
            f"按 system prompt 输出 JSON。"
            f"**summary_md 输出空字符串 `\"\"`**，只输出本文件的 suggestions。"
        )

    @staticmethod
    def _merge_chunks(results: list[dict[str, Any]], *, skipped_files: list[str] | None = None, file_priorities: dict[str, float] | None = None, new_files: set[str] | None = None, cap_truncated: list[str] | None = None) -> dict[str, Any]:
        """合并多个 chunk 的结果.

        V9 选优: file_priorities 用于 per-file 预算加权 (高优先级文件更多槽位).
        """
        all_suggestions: list[dict[str, Any]] = []
        summaries: list[str] = []

        for r in results:
            if not isinstance(r, dict):
                continue
            suggs = r.get("suggestions") or []
            if isinstance(suggs, list):
                all_suggestions.extend(suggs)
            sm = (r.get("summary_md") or "").strip()
            if sm:
                summaries.append(sm)

        # 去重: 同 file + 同 start_line 只保留 severity 最高的
        seen: dict[str, dict[str, Any]] = {}
        sev_order = {"high": 3, "medium": 2, "low": 1}
        for s in all_suggestions:
            if not isinstance(s, dict):
                continue
            key = f"{s.get('file', '')}:{s.get('start_line', 0)}"
            existing = seen.get(key)
            if existing is None:
                seen[key] = s
            else:
                new_sev = sev_order.get((s.get("severity") or "medium").lower(), 2)
                old_sev = sev_order.get((existing.get("severity") or "medium").lower(), 2)
                if new_sev > old_sev:
                    seen[key] = s

        merged_suggestions = list(seen.values())

        # 严重度过滤: 低于 min_severity 的建议不发布 ({prefix}-RULE 引用豁免)
        min_severity = config.improve_min_severity
        if min_severity:
            sev_levels = {"critical": 4, "high": 3, "medium": 2, "low": 1}
            min_level = sev_levels.get(min_severity, 0)
            if min_level > 0:
                kept = []
                for s in merged_suggestions:
                    rationale = (s.get("rationale") or "")
                    if _RULE_EXEMPT_REGEX.search(rationale):
                        kept.append(s)
                        continue
                    s_sev = (s.get("severity") or "medium").lower()
                    s_level = sev_levels.get(s_sev, 2)
                    if s_level >= min_level:
                        kept.append(s)
                    else:
                        logger.info(
                            "improve.severity_filter file={} line={} severity={} header={!r}",
                            s.get("file"), s.get("start_line"), s_sev, s.get("header"),
                        )
                merged_suggestions = kept

        # 评分过滤: 低于 min_score 的建议不发布 ({prefix}-RULE 引用豁免)
        min_score = config.improve_min_score
        if min_score > 0:
            kept = []
            for s in merged_suggestions:
                rationale = (s.get("rationale") or "")
                if _RULE_EXEMPT_REGEX.search(rationale):
                    kept.append(s)
                    continue
                sc = ImproveCommand._score_suggestion(s)
                if sc >= min_score:
                    kept.append(s)
                else:
                    logger.info(
                        "improve.score_filter file={} line={} score={} header={!r}",
                        s.get("file"), s.get("start_line"), sc, s.get("header"),
                    )
            merged_suggestions = kept

        # V9 选优: (severity, score) 复合键 + priority 加权 per-file 预算 + 全局两遍填充(覆盖优先)
        # 修复: 原来只按 severity 3 档排序, 同档内随机(稳定排序=处理序) → 50文件时 top15
        # 全落前3个文件, 47文件零覆盖. 改为 score 区分同档 + 每文件先取1条保证覆盖.
        sev_rank = {"high": 3, "medium": 2, "low": 1}

        def _key(s):
            # severity 为主键 (遵循 high/medium/low 严格分级), score 为同档内区分器
            return (sev_rank.get((s.get("severity") or "medium").lower(), 2),
                    ImproveCommand._score_suggestion(s))

        # per-file 预算: 按 priority 加权 (高优先级文件更多槽位, 低优先级少占, 下限 min_per_file)
        max_per_file = config.improve_max_suggestions_per_file
        min_per_file = config.improve_min_suggestions_per_file
        _max_prio = max((file_priorities or {}).values()) if file_priorities else 0.0

        def _budget(fp: str) -> int:
            if max_per_file <= 0:
                return 10 ** 9  # 不限
            # A: 新增文件给满额, 不被低优先级压缩 + 豁免单文件截断
            if new_files and fp in new_files:
                return max_per_file
            if not file_priorities or _max_prio <= 0:
                return max_per_file
            norm = (file_priorities.get(fp, 0.0) or 0.0) / _max_prio
            b = min_per_file + round((max_per_file - min_per_file) * norm)
            return max(min_per_file, min(max_per_file, b))

        by_file: dict[str, list[dict[str, Any]]] = {}
        for s in merged_suggestions:
            fp = s.get("file", "") or "_unknown"
            by_file.setdefault(fp, []).append(s)

        # 每文件按复合键留 top budget(fp), 其余计入 per_file_truncated
        per_file_truncated = 0
        kept_list: list[dict[str, Any]] = []
        for fp, suggs in by_file.items():
            suggs.sort(key=_key, reverse=True)
            b = _budget(fp)
            if len(suggs) > b:
                per_file_truncated += len(suggs) - b
                suggs = suggs[:b]
            kept_list.extend(suggs)
        if per_file_truncated > 0:
            logger.info(
                "improve.per_file_limit max_per_file={} min={} truncated={} (降为总览文字)",
                max_per_file, min_per_file, per_file_truncated,
            )
        merged_suggestions = kept_list

        # 全局限流: 两遍填充 — 第1遍每文件1条(覆盖优先), 第2遍按复合键填剩余(质量优先)
        max_suggestions = config.improve_max_suggestions
        truncated_count = 0
        if max_suggestions > 0 and len(merged_suggestions) > max_suggestions:
            by_file2: dict[str, list[dict[str, Any]]] = {}
            for s in merged_suggestions:
                fp = s.get("file", "") or "_unknown"
                by_file2.setdefault(fp, []).append(s)
            # 文件按其最佳建议的复合键降序 (高价值文件先占覆盖槽); 新增文件优先 (A: 豁免截断)
            file_order = sorted(by_file2.keys(),
                                key=lambda fp: (1 if (new_files and fp in new_files) else 0,
                                               _key(by_file2[fp][0])),
                                reverse=True)
            phase1: list[dict[str, Any]] = []
            used: dict[str, int] = {}
            for fp in file_order:
                if len(phase1) >= max_suggestions:
                    break
                phase1.append(by_file2[fp][0])  # 每文件1条 → 覆盖最大化
                used[fp] = 1
            # 第2遍: 剩余候选按复合键降序填, 受 budget 约束
            remaining = [s for fp in by_file2 for s in by_file2[fp][1:]]
            remaining.sort(key=_key, reverse=True)
            for s in remaining:
                if len(phase1) >= max_suggestions:
                    break
                fp = s.get("file", "") or "_unknown"
                if used.get(fp, 0) < _budget(fp):
                    phase1.append(s)
                    used[fp] = used.get(fp, 0) + 1
            truncated_count = len(merged_suggestions) - len(phase1)
            merged_suggestions = phase1
            logger.info(
                "improve.suggestion_limit kept={} truncated={} files_covered={}",
                max_suggestions, truncated_count,
                len({s.get("file") for s in phase1}),
            )

        # summary: 从 suggestions 列表生成概览
        total_suggestions = len(merged_suggestions)
        if total_suggestions > 0:
            items: list[str] = []
            for s in merged_suggestions:
                fp = s.get("file", "")
                line = s.get("start_line", "")
                header = (s.get("header") or "").strip()
                severity = (s.get("severity") or "medium").upper()
                label = s.get("label", "")
                rationale = (s.get("rationale") or "").strip()
                # 简短版: 文件 + 行号 + 标签 + header + 理由首句
                short = rationale.split("。")[0].split("，")[0]
                line_str = f"L{line}" if line else ""
                items.append(
                    f"- **`{fp}`**{line_str} — **{header}** [{severity}/{label}]: {short}"
                )
            merged_summary = (
                "## 改进总览\n\n"
                + "\n".join(items)
            )
            if truncated_count > 0:
                merged_summary += f"\n\n> ℹ️ 另有 {truncated_count} 条低优先级建议未展示（上限 {max_suggestions} 条）"
            if per_file_truncated > 0:
                merged_summary += f"\n\n> ℹ️ 另有 {per_file_truncated} 条建议因单文件上限（{max_per_file} 条/文件）降为总览文字"
            if skipped_files:
                # V6: skipped_files 现在仅含测试/配置文件 (IMPROVE_FULL_FILES 软阈值化后不再丢文件)
                display = skipped_files[:10]
                suffix = f" 等 {len(skipped_files)} 个" if len(skipped_files) > 10 else ""
                merged_summary += (
                    f"\n\n> ⏭️ 以下 {len(skipped_files)} 个测试/配置文件跳过检视 (配置 IMPROVE_SKIP_TEST_PATHS 调整): "
                    f"{', '.join(display)}{suffix}"
                )
        else:
            merged_summary = "## 改进总览\n\n未发现问题。"
            if skipped_files:
                display = skipped_files[:10]
                suffix = f" 等 {len(skipped_files)} 个" if len(skipped_files) > 10 else ""
                merged_summary += (
                    f"\n\n> ⏭️ 以下 {len(skipped_files)} 个测试/配置文件跳过检视: "
                    f"{', '.join(display)}{suffix}"
                )

        # D: 文件数硬上限截断的低优先级文件 (与 test_skipped 分开提示)
        if cap_truncated:
            _display = cap_truncated[:10]
            _suffix = f" 等 {len(cap_truncated)} 个" if len(cap_truncated) > 10 else ""
            merged_summary += (
                f"\n\n> ✂️ 另有 {len(cap_truncated)} 个低优先级文件因超出文件数上限"
                f"（IMPROVE_MAX_FILES_TO_REVIEW={config.improve_max_files_to_review}）未检视: "
                f"{', '.join(_display)}{_suffix}"
            )

        return {
            "summary_md": merged_summary,
            "suggestions": merged_suggestions,
        }

    # ---------- helpers ----------
    @staticmethod
    def _make_token_summary(prompt_tokens: int, completion_tokens: int, model: str, cost_credits: float = 0.0):
        """创建汇总 token 统计的 LLMResult (仅用于 _last_oc_result 替代)."""
        from reviewagent.llm.base import LLMResult
        return LLMResult(
            data={}, prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens, cost_credits=cost_credits, model=model,
        )

    def _get_mr_head_sha(self) -> str | None:
        """获取当前 MR 的 head_sha (用于 record_suggestion 时记录发布 SHA)."""
        try:
            refs = self.gitlab.get_mr_diff_refs(self.project_id, self.mr_iid)
            return refs.get("head_sha") or refs.get("start_sha")
        except Exception as e:
            logger.warning("improve.get_mr_head_sha failed: {}", e)
            return None

    def _file_blob_sha(self, file_path: str, ws) -> str | None:
        """V9: 文件内容指纹 (sha1 of raw bytes at worktree head).

        相同 blob = 文件字节相同 = 上轮 LLM(无状态)的建议必然仍成立 → 可安全复用.
        不依赖 git 历史 (workspace 是 shallow fetch, 旧 head 可能已裁剪).
        """
        import hashlib
        try:
            ws_root = Path(ws.worktree)
            p = ws_root / file_path
            if not p.is_file():
                try:
                    p = (ws_root / file_path).resolve()
                except OSError:
                    return None
            if not p.is_file():
                return None
            return hashlib.sha1(p.read_bytes()).hexdigest()
        except OSError:
            return None

    def _upsert_file_review_states(
        self, files: list[str], ws, head_sha: str,
        per_file_counts: dict[str, int],
    ) -> None:
        """V9: 记录本轮每个文件的 blob + 建议数, 供下一轮按文件粒度复用.

        所有文件都 upsert (含复用文件 — 其 blob 不变但 reviewed_at 刷新).
        失败不阻塞发布流程 (non-fatal).
        """
        try:
            from reviewagent.telemetry.store import get_store
            _store = get_store()
            for fp in files:
                _store.upsert_file_review_state(
                    project_id=self.project_id, mr_iid=self.mr_iid,
                    file_path=fp, blob_sha=self._file_blob_sha(fp, ws),
                    head_sha=head_sha, suggestion_count=per_file_counts.get(fp, 0),
                )
        except Exception as e:
            logger.warning("improve.upsert_file_state_failed (non-fatal): {}", e)

    def _diff_line_map(self) -> dict[str, set[int]]:
        """读 self.ws.diff_file 解析每个文件的 valid new_line 集合."""
        if not self.ws or not self.ws.diff_file:
            return {}
        try:
            diff_text = Path(self.ws.diff_file).read_text(encoding="utf-8")
        except OSError:
            return {}
        return parse_diff_line_map(diff_text)

    # ---------- 文件筛选 / 抽样护栏 (2026-08-17) ----------

    @staticmethod
    def _new_files_from_diff(diff_file) -> set[str]:
        """从 unified diff 解析新增文件集合 (new file mode / --- /dev/null)."""
        try:
            text = diff_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return set()
        new_files: set[str] = set()
        cur: str | None = None
        for line in text.splitlines():
            if line.startswith("diff --git "):
                parts = line.split()
                cur = parts[-1].removeprefix("b/") if len(parts) >= 4 else None
            elif line.startswith("new file mode") and cur:
                new_files.add(cur)
            elif line.startswith("--- "):
                p = line.removeprefix("--- ").split("\t", 1)[0].strip()
                if p == "/dev/null" and cur:
                    new_files.add(cur)
        return new_files

    @staticmethod
    def _sample_case_files(
        all_files: list[str], patterns: tuple[str, ...], max_n: int,
    ) -> tuple[list[str], list[str]]:
        """确定性抽样: 命中 patterns 的文件超过 max_n 时, 按路径 md5 排序取前 max_n.

        Returns: (kept_all_files, unsampled_list)
            - kept_all_files: 抽样后剩余的 all_files (顺序不变, 仅移除未抽中者)
            - unsampled_list: 被抽样剔除的文件 (已排序)
        """
        import hashlib
        from pathlib import Path as _P
        case_files = [
            fp for fp in all_files
            if any(pat in fp or _P(fp).name.startswith(pat.rstrip("/")) for pat in patterns)
        ]
        if len(case_files) <= max_n:
            return list(all_files), []
        sampled = sorted(case_files, key=lambda p: hashlib.md5(p.encode("utf-8")).hexdigest())[:max_n]
        sampled_set = set(sampled)
        unsampled = [fp for fp in case_files if fp not in sampled_set]
        kept = [fp for fp in all_files if fp not in set(unsampled)]
        return kept, sorted(unsampled)

    @staticmethod
    def _apply_file_count_cap(
        files_to_review: list[str], new_files: set[str], keyword_paths: tuple[str, ...],
        max_files: int,
    ) -> tuple[list[str], list[str]]:
        """文件数硬上限: 必保(新增+关键路径)外的低优先级 tail 截断.

        files_to_review 须已按优先级降序.
        Returns: (kept, truncated)
        """
        if max_files <= 0 or len(files_to_review) <= max_files:
            return list(files_to_review), []
        must: list[str] = []
        optional: list[str] = []
        for fp in files_to_review:
            is_must = (fp in new_files) or any(
                kw.lstrip("/") in fp or kw in fp.lstrip("/") for kw in keyword_paths
            )
            (must if is_must else optional).append(fp)
        budget = max(0, max_files - len(must))
        kept_optional = optional[:budget]
        truncated = optional[budget:]
        return must + kept_optional, truncated

    def _read_file_line_count(self, file_path: str, ws) -> int:
        """读 worktree 文件行数, 用于 priority 排序 + strategy 分配.
        失败返回 -1 (排序时按最低优先级)."""
        try:
            wt_path = Path(ws.worktree) / file_path
            if not wt_path.is_file():
                return -1
            with wt_path.open("r", encoding="utf-8", errors="replace") as f:
                return sum(1 for _ in f)
        except (OSError, UnicodeDecodeError):
            return -1

    def _test_feature_density(self, file_path: str, file_size: int) -> float:
        """V7 测试特征密度 (0~1): 绝对命中数 + 密度组合.

        高分 → 高风险测试文件 (含 fixture/mock/parametrize/assert/setup-teardown).
        用绝对数 + 密度组合, 避免大文件被稀释 (2000行100assert ≠ 50行10assert).
        """
        if file_size <= 0:
            return 0.0
        lines = self._read_file_lines(file_path)
        if not lines:
            return 0.0
        keywords = config.improve_test_feature_keywords
        hits = sum(1 for ln in lines if any(kw in ln for kw in keywords))
        if hits == 0:
            return 0.0
        # 绝对数: 30 命中即满分 0.5
        abs_score = min(hits, 30) / 30 * 0.5
        # 密度: 5% 命中率即满分 0.5
        density = hits / len(lines)
        density_score = min(density / 0.05, 1.0) * 0.5
        return abs_score + density_score

    def _assign_strategy(
        self, sorted_files: list[str], file_meta: dict[str, tuple[int, int]],
        overflow_set: set[str],
    ) -> dict[str, str]:
        """V7 strategy 分配 (full / partial / patch) — 与 priority 解耦.

        full    = 完整源码 (≤ full_files 配额, 优先高 priority 文件)
        partial = diff 行 ±N 行 context (中等文件 / 关键路径降级)
        patch   = diff + ±patch_context_lines 行最小 context (overflow / 大文件)
        """
        strategy: dict[str, str] = {}
        full_quota = config.improve_full_files
        for fp in sorted_files:
            if fp in overflow_set:
                strategy[fp] = "patch"  # MAX_DIFF_CHARS overflow → 强制 patch
                continue
            if full_quota > 0 and sum(1 for s in strategy.values() if s == "full") < full_quota:
                strategy[fp] = "full"
                continue
            file_size = file_meta[fp][1]
            if file_size > 0 and file_size < 2000:
                strategy[fp] = "partial"
            elif any(kw.lstrip("/") in fp or kw in fp.lstrip("/") for kw in config.improve_keyword_paths):
                strategy[fp] = "partial"
            else:
                strategy[fp] = "patch"
        return strategy

    def _build_full_source_block(self, file_path: str) -> str:
        """full 模式: 读 worktree 文件, 拼完整源码 (≤ 5000 行, 超出截断)."""
        _MAX = 5000
        lines = self._read_file_lines(file_path)
        if not lines:
            return f"### 完整源码\n(无法读取 {file_path})"
        total = len(lines)
        if total > _MAX:
            kept = lines[:_MAX]
            numbered = "\n".join(f"{i+1:4d}| {ln}" for i, ln in enumerate(kept))
            return (
                f"### 完整源码: `{file_path}` (共 {total} 行, **已截断到前 {_MAX} 行**)\n"
                f"⚠️ 末尾 {total - _MAX} 行未加载, 如需检视请缩小 diff 范围或拆分 MR\n```\n{numbered}\n```"
            )
        numbered = "\n".join(f"{i+1:4d}| {ln}" for i, ln in enumerate(lines))
        return f"### 完整源码: `{file_path}` (共 {total} 行)\n```\n{numbered}\n```"

    def _build_partial_context_block(self, file_path: str, valid_lines: set[int], ws) -> str:
        """partial 模式 (V6): 仅取 diff 行 ±N 行 context, 多段合并.
        适用: 超出 IMPROVE_FULL_FILES 但仍需 LLM 检视的中等文件.
        性能: 比 full 模式省 token, 比 patch 模式多 ±N 行 context (跨行识别).
        """
        lines = self._read_file_lines(file_path)
        if not lines:
            return f"### 上下文 (partial)\n(无法读取 {file_path})"
        n = config.improve_partial_context_lines
        ranges: list[tuple[int, int]] = []
        for vl in sorted(valid_lines):
            lo, hi = max(1, vl - n), min(len(lines), vl + n)
            ranges.append((lo, hi))
        # 合并重叠/相邻段
        merged: list[list[int]] = []
        for lo, hi in sorted(ranges):
            if merged and lo <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], hi)
            else:
                merged.append([lo, hi])
        blocks: list[str] = []
        for lo, hi in merged:
            chunk = lines[lo - 1:hi]
            numbered = "\n".join(f"{lo+i:4d}| {ln}" for i, ln in enumerate(chunk))
            blocks.append(numbered)
        return (
            f"### diff 上下文 (partial, ±{n} 行): `{file_path}`\n"
            f"⚠️ 此文件超出 IMPROVE_FULL_FILES={config.improve_full_files} 配额, 已降级为 partial 模式\n"
            f"⚠️ 仅显示 diff 附近 {len(merged)} 段 (±{n} 行), 跨段语义识别能力下降\n"
            f"```\n" + "\n...\n".join(blocks) + "\n```"
        )

    def _build_patch_only_block(self, file_path: str, valid_lines: set[int]) -> str:
        """patch-only 模式 (V7): diff 行 ±N 行最小 context.

        V6 纯行号基本检不出跨行问题; V7 加 ±patch_context_lines 行 source,
        让 LLM 能看到局部结构 (函数签名/缩进/上下文), 从"摆设"变"能用".
        适用: MAX_DIFF_CHARS overflow 文件 + 低优先级大文件.
        """
        n = config.improve_patch_context_lines
        lines = self._read_file_lines(file_path)
        vl = sorted(valid_lines)
        vl_display = vl[:20]
        vl_suffix = "..." if len(vl) > 20 else ""

        # ±N 行最小 context (与 partial 同算法, 但段数不限, 只取前 5 段控成本)
        context_block = ""
        if lines and n > 0:
            ranges: list[tuple[int, int]] = []
            for v in vl:
                lo, hi = max(1, v - n), min(len(lines), v + n)
                ranges.append((lo, hi))
            merged: list[list[int]] = []
            for lo, hi in sorted(ranges):
                if merged and lo <= merged[-1][1] + 1:
                    merged[-1][1] = max(merged[-1][1], hi)
                else:
                    merged.append([lo, hi])
            blocks: list[str] = []
            for lo, hi in merged[:5]:  # 最多 5 段, 控 token 成本
                chunk = lines[lo - 1:hi]
                numbered = "\n".join(f"{lo+i:4d}| {ln}" for i, ln in enumerate(chunk))
                blocks.append(numbered)
            omitted = len(merged) - 5 if len(merged) > 5 else 0
            context_block = (
                f"\n### 最小上下文 (patch, ±{n} 行, 前 {len(merged[:5])} 段)\n"
                f"```\n" + "\n...\n".join(blocks) + "\n```\n"
                + (f"(另有 {omitted} 段未展示)\n" if omitted else "")
            )

        return (
            f"### patch-only: `{file_path}`\n"
            f"⚠️ 已降级为 patch-only (overflow 或低优先级), 仅 diff + ±{n} 行最小 context\n"
            f"⚠️ 跨行/跨段语义识别能力有限, 优先识别行内问题 (裸 except / 可变默认参数 / 硬编码等)\n"
            f"建议关注行: {vl_display}{vl_suffix}\n"
            f"{context_block}"
        )

    def _read_file_lines(self, file_path: str) -> list[str]:
        """从 worktree 读 file 源，按行 split；读不到返回 []."""
        if not self.ws:
            return []
        # 绝对路径 / 相对路径都兼容
        ws_root = Path(self.ws.worktree)
        candidates = [ws_root / file_path]
        # 若 file_path 含 ../ 等相对引用，从 worktree 解析
        try:
            candidates.append((ws_root / file_path).resolve())
        except OSError:
            pass
        for p in candidates:
            try:
                if p.is_file():
                    return p.read_text(encoding="utf-8").splitlines(keepends=False)
            except OSError:
                continue
        return []

    @staticmethod
    def _find_line_by_existing_code(
        file_lines: list[str],
        existing_code: str,
        *,
        hint_line: int = 0,
        max_window: int = 5,
    ) -> int | None:
        """在 file_lines 中搜索 existing_code 块，返回匹配首行行号（1-based）.

        匹配策略（按优先级）:
          1. 从 hint_line 附近 ±max_window 找最接近的完整块匹配
          2. 整文件从头到尾找第一个完整块匹配
          3. 找首行精确匹配（fallback — 处理 existing_code 只有一行的情况）

        返回 None 表示没找到.
        """
        if not file_lines or not existing_code.strip():
            return None
        target_lines = existing_code.strip("\n").split("\n")
        # 裁掉 leading trivial 行，避免定位到非代码行
        # 策略：只裁掉明确的非代码行，保留注释/关键字/运算符行
        while target_lines:
            first = target_lines[0].strip()
            if not first:
                # 空行 → 裁掉
                target_lines.pop(0)
                continue
            if first in ('"""', "'''"):
                # 单行 docstring 标记 → 裁掉
                target_lines.pop(0)
                continue
            # 保留注释行
            if first.startswith('#'):
                break
            # 保留关键字开头的行
            if first.startswith(('def ', 'class ', 'if ', 'for ', 'while ', 'return ',
                                  'import ', 'from ', 'try', 'except', 'with ', 'yield ',
                                  'else', 'elif ', 'case ', 'raise ', 'pass', 'break', 'continue')):
                break
            # 保留包含运算符/括号的行
            if any(c in first for c in '()[]{}=+-*/<>!&|'):
                break
            # 保留以冒号结尾的行（块开始，如 else:）
            if first.endswith(':'):
                break
            # 保留看起来像变量/函数调用的行（包含点号或下划线）
            if '.' in first or '_' in first:
                break
            # 纯文本行（docstring 内容）→ 裁掉
            target_lines.pop(0)
        target_first = target_lines[0].strip() if target_lines else ""
        if not target_first:
            return None

        n = len(file_lines)
        m = len(target_lines)

        def _block_matches_at(start_idx: int) -> bool:
            if start_idx + m > n:
                return False
            for j in range(m):
                if file_lines[start_idx + j].strip() != target_lines[j].strip():
                    return False
            return True

        # 1. hint_line 附近（±max_window）
        if hint_line >= 1:
            lo = max(1, hint_line - max_window)
            hi = min(n - m + 1, hint_line + max_window)
            best: int | None = None
            best_dist = max_window + 1
            for s in range(lo, hi + 1):
                if _block_matches_at(s - 1):
                    dist = abs(s - hint_line)
                    if dist < best_dist:
                        best = s
                        best_dist = dist
            if best is not None:
                return best

        # 2. 整文件从头找
        for s in range(1, n - m + 2):
            if _block_matches_at(s - 1):
                return s

        # 3. 首行精确匹配（fallback）
        for i, line in enumerate(file_lines, start=1):
            if line.strip() == target_first:
                return i
        return None

    def _build_user_prompt(self) -> str:
        """把 diff 的 valid new_line 集合 + 完整文件源码喂给 agent —
        严格约束它的 start_line 取值，让模型能精确数出文件行号。"""
        line_map = self._diff_line_map()

        # 仓库规则上下文 (AGENTS.md) — 优先遵循
        repo_ctx_block = ""
        if self.repo_context:
            repo_ctx_block = (
                "## 仓库规则 (AGENTS.md) — 优先遵循\n\n"
                "以下是本仓库的编码规范 / 检视规则。"
                "**你的建议必须优先覆盖这些规则**：如果 diff 中存在违反下列规则的地方，"
                "必须给出对应的 inline suggestion。\n\n"
                f"{self.repo_context}\n\n"
            )

        # === C. 已发过的建议列表 — 让 agent 知道自己说过了 ===
        # 这样 agent 看到 diff 时, 如果是同样问题就 skip, 不会重复发
        already_suggested_block = ""
        try:
            from reviewagent.telemetry.store import get_store
            existing = get_store().list_suggestion_headers(
                self.project_id, self.mr_iid
            )
            if existing:
                items = []
                for s in existing[:50]:  # 限制 50 条避免 prompt 过长
                    fp = s.get("fp_short", "?")
                    sev = (s.get("severity") or "?").upper()
                    st = (s.get("state") or "?").upper()
                    fp_path = s.get("file_path", "?")
                    line = s.get("target_line", "?")
                    hdr = (s.get("header") or "?").strip()[:30]
                    items.append(
                        f"- `{fp_path}` L{line} — {hdr} [{sev}/{st}] (fp={fp})"
                    )
                already_suggested_block = (
                    "## ⚠️ 已发过的建议（不要重复）\n\n"
                    f"本 MR 已发布 **{len(existing)}** 条 suggestion. "
                    "**绝对不要重复这些**，即使代码再次出现也一样.\n"
                    "如果你认为某条已 applied / 关闭但应该重新评估，"
                    "**只在 summary_md 里文字描述**，不要发新 suggestion。\n\n"
                    + "\n".join(items)
                    + "\n\n"
                )
        except Exception as e:
            logger.warning("improve.list_existing failed (non-fatal): {}", e)

        if not line_map:
            return (
                f"{repo_ctx_block}"
                f"{already_suggested_block}"
                "请按你的 system prompt 处理当前 MR 的 diff"
                "（变更内容见上方附件文件）。"
            )
        formatted = format_line_map_for_prompt(line_map)

        # 收集每个文件的源码（带 `<行号>: ` 前缀，方便模型精确数行）
        file_blocks: list[str] = []
        for fp in sorted(line_map.keys()):
            lines = self._read_file_lines(fp)
            if not lines:
                continue
            numbered = "\n".join(f"{i+1:4d}| {ln}" for i, ln in enumerate(lines))
            file_blocks.append(
                f"### 完整源码：`{fp}`（共 {len(lines)} 行；行号在左侧）\n```\n{numbered}\n```"
            )
        files_text = "\n\n".join(file_blocks) if file_blocks else "(no files)"

        return (
            f"{repo_ctx_block}"
            "请按你的 system prompt 处理当前 MR 的 diff\n"
            "（变更内容见上方附件文件）。\n\n"
            "## diff 有效新增行（VALID NEW LINES）\n\n"
            "下面列出本次 diff 里每个文件所有以 `+` 开头的**新文件行号**。"
            "**你的 `start_line` 必须且只能从此集合中取**；"
            "若你怀疑某 issue 的目标行不在此集合里（context 行 / 删除行 / "
            "跨文件推断），请放弃 `improved_code`，改为在 `summary_md` 里文字描述，"
            "不要强行填一个错位的 suggestion。\n\n"
            f"{formatted}\n\n"
            "## 完整文件源码（带行号）\n\n"
            "**强烈建议**：每条 suggestion 的 `start_line` 必须**精确等于** "
            "下方源码里 `existing_code` 第一行对应的行号。"
            "Python 端会用 `existing_code` 反查行号校验 — 行号错位的会被自动降级。\n\n"
            f"{files_text}\n"
        )

    def _build_summary_placeholder(
        self,
        inline_posted: list[dict[str, Any]],
        inline_skipped: list[dict[str, Any]],
        total_agent_suggestions: int,
    ) -> str:
        """生成 summary placeholder (在 inline 循环之前先发, 拿到 note_id 后 edit 为完整内容).

        placeholder 包含版本号 V{N} 让用户在看到第一条 inline 之前就知道这是第几次检视.
        内容会在循环结束后被 edit 为 _build_summary_v2 的完整输出.
        """
        try:
            from reviewagent.telemetry.store import get_store
            store = get_store()
            runs = store.list_runs(
                project_id=self.project_id,
                mr_iid=self.mr_iid,
                command="improve",
                limit=1000,
            )
            version = len(runs) or 1
        except Exception:
            version = 1
        return f"## 改进总览 V{version}\n\n_加载中…_"

    @staticmethod
    def _collect_defined_names(
        tree: "ast.AST",
        ast_module,
    ) -> set[str]:
        """从 AST 中收集所有「定义」的符号名.

        覆盖: ClassDef / FunctionDef / AsyncFunctionDef / Lambda args /
        Import / ImportFrom / Assign / AnnAssign / NamedExpr /
        For target / With optional_vars / global / nonlocal / decorator names.
        """
        defs: set[str] = set()

        def _add_target(tgt):
            if isinstance(tgt, ast_module.Name):
                defs.add(tgt.id)
            elif isinstance(tgt, ast_module.Tuple):
                for el in tgt.elts:
                    _add_target(el)
            elif isinstance(tgt, ast_module.Starred):
                _add_target(tgt.value)

        for node in ast_module.walk(tree):
            if isinstance(node, ast_module.ClassDef):
                defs.add(node.name)
            elif isinstance(node, (ast_module.FunctionDef, ast_module.AsyncFunctionDef)):
                defs.add(node.name)
                args = node.args
                for arg in (args.posonlyargs + args.args +
                            args.kwonlyargs + [args.vararg, args.kwarg]):
                    if arg:
                        defs.add(arg.arg)
                for dec in node.decorator_list:
                    if isinstance(dec, ast_module.Name):
                        defs.add(dec.id)
            elif isinstance(node, ast_module.Lambda):
                args = node.args
                for arg in (args.posonlyargs + args.args +
                            args.kwonlyargs + [args.vararg, args.kwarg]):
                    if arg:
                        defs.add(arg.arg)
            elif isinstance(node, (ast_module.Import, ast_module.ImportFrom)):
                for alias in node.names:
                    defs.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(node, ast_module.Assign):
                for tgt in node.targets:
                    _add_target(tgt)
            elif isinstance(node, ast_module.AnnAssign):
                if node.target:
                    _add_target(node.target)
            elif isinstance(node, ast_module.NamedExpr):
                _add_target(node.target)
            elif isinstance(node, ast_module.For):
                if node.target:
                    _add_target(node.target)
            elif isinstance(node, ast_module.comprehension):
                for gen in [node]:
                    if gen.target:
                        _add_target(gen.target)
            elif isinstance(node, ast_module.With):
                for item in node.items:
                    if item.optional_vars:
                        _add_target(item.optional_vars)
            elif isinstance(node, ast_module.Global):
                for name in node.names:
                    defs.add(name)
            elif isinstance(node, ast_module.Nonlocal):
                for name in node.names:
                    defs.add(name)
        return defs

    def _safe_parse_defs(
        self,
        src: str,
        _ast_module,
    ) -> tuple[set[str], "SyntaxError | None"]:
        """解析 src 收集 defs, 带 partial fallback.

        返回 (defs, error):
          - 完整 parse 成功 → (full_defs, None)
          - 完整 parse 失败但 first error 之前能 parse → (partial_defs, error)
          - 第一行就错, partial 也失败 → (empty, error)

        partial 策略: 切到 first error 行之前再往前逐行回退 (避免 dangling def header),
        直到 partial 能 parse 或到达 src 起点. 这样能最大限度保留 file-level defs
        (import/def/class) 用于 missing 检测.

        Why: 旧逻辑裸 `except SyntaxError: pass` 让 local_defs 留空,
             file-level import/def/class 全部被误判为 missing
             (MR299 L58 scene: big_service.py L63 cart} SyntaxError
             引发对所有 fetch_* 改进误报 '目标文件未定义 requests/user_id').
        """
        if not src:
            return set(), None
        try:
            tree = _ast_module.parse(src)
            return self._collect_defined_names(tree, _ast_module), None
        except SyntaxError as e:
            if not e.lineno or e.lineno <= 1:
                return set(), e
            lines = src.splitlines()
            # 从 e.lineno - 1 行往前逐行回退, 直到 partial 能 parse 或归 0
            for end in range(e.lineno - 1, 0, -1):
                head_src = "\n".join(lines[:end])
                try:
                    tree_p = _ast_module.parse(head_src)
                    return self._collect_defined_names(tree_p, _ast_module), e
                except SyntaxError:
                    continue
            return set(), e

    def _detect_apply_risk(
        self,
        *,
        file_path: str,
        improved_code: str,
        file_sources: dict[str, list[str]],
        suggestion_text: str = "",
    ) -> tuple[str, list[str]]:
        """识别 improved_code 中引用了 file_sources 未定义的符号.

        返回 (level, msgs):
          - ("ok", [])              : 全部符号都能解析, apply 安全
          - ("warn", ["...", ...])  : 引用了 missing 符号, apply 后会 NameError / ImportError;
                                       review 中加 ⚠️ 提示, 但保留 Apply 按钮让 reviewer 自行处理

        排除: builtins / 关键字 / self / cls / 已定义 (def/class/import/赋值/函数参数/
        lambda/AnnAssign/NamedExpr/For target/With as/global/nonlocal/decorator).
        suggestion_text: 建议正文 (rationale/header). 若某个 missing 符号在建议正文里
        已声明补救方式 (补 import / 定义), 则降级为 "按建议手动补上" 的提示, 不再误报
        "apply 后会 NameError" (避免提醒与建议内容错位, MR178 4923 场景).
        改进片段本身 AST 解析 syntax error → 尝试 textwrap.dedent + 补 pass 再 parse,
        仍失败则视为 ok (治本靠 prompt 约束 8).
        """
        import ast as _ast
        import textwrap as _tw

        tree = None
        try:
            tree = _ast.parse(improved_code)
        except SyntaxError:
            # 改进片段常不完整 (def 缺 body / while 缺 body), 尝试 dedent + 补 pass
            for suffix in ("pass", "pass", "pass", "pass"):
                candidate = _tw.dedent(improved_code).rstrip() + "\n" + suffix + "\n"
                try:
                    tree = _ast.parse(candidate)
                    break
                except SyntaxError:
                    continue
            if tree is None:
                return "ok", []

        file_lines_local = file_sources.get(file_path, [])
        if not file_lines_local:
            # 没源文件 → 不警告 (避免误报)
            return "ok", []

        file_src = "\n".join(file_lines_local)

        # === 收集本文件已定义符号 (含 SyntaxError partial fallback) ===
        local_defs, file_syntax_err = self._safe_parse_defs(file_src, _ast)
        # target_unparseable: partial 也没成功 (e.g. 首行即错) →
        # 不能可靠判断 missing, 后续走 file-level hint 路径不发 "目标文件未定义 X" 误报
        target_unparseable = (
            file_syntax_err is not None and not local_defs
        )

        import builtins as _bi
        builtin_names = set(dir(_bi))
        excluded = {"True", "False", "None", "self", "cls"}
        excluded.update(builtin_names)

        # === 收集 improved_code 自身定义的符号 ===
        defined_in_improved = self._collect_defined_names(tree, _ast)

        all_defined = local_defs | defined_in_improved | excluded

        # === 收集 missing Name + Attribute base ===
        # 规则:
        #   - Name: 必须已定义 (否则 missing)
        #   - Attribute: base 是 self/cls → 跳过 (实例属性动态)
        #              base 是已定义 Name (e.g. logger) → 跳过
        #              base 是未定义 Name → 标 base (避免重复标 attr)
        # 注解上下文豁免: 目标文件有 `from __future__ import annotations` 时,
        # 注解在运行时不被求值, 只出现在注解里的符号不会触发 NameError
        # (e.g. def f(x: Any) -> None, Any 未 import 也能正常运行).
        has_future_annotations = False
        if not target_unparseable and file_src:
            # SyntaxError 时 partial head 已 parse 过, 直接用 partial 段重新检查
            # __future__ import 通常在文件顶部, partial head 一定包含
            fa_src = (
                "\n".join(file_src.splitlines()[: file_syntax_err.lineno - 1])
                if file_syntax_err and file_syntax_err.lineno
                else file_src
            )
            try:
                mod_fa = _ast.parse(fa_src)
                for n in mod_fa.body:
                    if isinstance(n, _ast.ImportFrom) and n.module == "__future__":
                        if any(alias.name == "annotations" for alias in n.names):
                            has_future_annotations = True
            except SyntaxError:
                pass

        annot_use: dict[str, int] = {}
        total_use: dict[str, int] = {}
        if has_future_annotations:
            for node in _ast.walk(tree):
                _ann_nodes: list[_ast.AST] = []
                if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                    if node.returns:
                        _ann_nodes.append(node.returns)
                    for arg in (
                        list(node.args.posonlyargs) + list(node.args.args)
                        + list(node.args.kwonlyargs)
                    ):
                        if arg.annotation:
                            _ann_nodes.append(arg.annotation)
                    if node.args.vararg and node.args.vararg.annotation:
                        _ann_nodes.append(node.args.vararg.annotation)
                    if node.args.kwarg and node.args.kwarg.annotation:
                        _ann_nodes.append(node.args.kwarg.annotation)
                elif isinstance(node, _ast.AnnAssign) and node.annotation:
                    _ann_nodes.append(node.annotation)
                else:
                    continue
                for _an in _ann_nodes:
                    for _n in _ast.walk(_an):
                        if isinstance(_n, _ast.Name) and not isinstance(_n.ctx, _ast.Store):
                            annot_use[_n.id] = annot_use.get(_n.id, 0) + 1
            for node in _ast.walk(tree):
                if isinstance(node, _ast.Name) and not isinstance(node.ctx, _ast.Store):
                    total_use[node.id] = total_use.get(node.id, 0) + 1

        def _is_annotation_only(name: str) -> bool:
            return (
                has_future_annotations
                and annot_use.get(name, 0) > 0
                and annot_use.get(name, 0) == total_use.get(name, 0)
            )

        missing: list[str] = []
        seen_attr_bases: set[int] = set()
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Name):
                if isinstance(node.ctx, _ast.Store):
                    continue
                if node.id in all_defined:
                    continue
                if _is_annotation_only(node.id):
                    continue
                missing.append(node.id)
            elif isinstance(node, _ast.Attribute):
                base = node.value
                if isinstance(base, _ast.Name) and base.id in all_defined:
                    continue
                if isinstance(base, _ast.Name) and id(base) not in seen_attr_bases:
                    seen_attr_bases.add(id(base))
                    if base.id not in all_defined and not _is_annotation_only(base.id):
                        missing.append(base.id)

        # 去重保序
        seen: set[str] = set()
        uniq_missing: list[str] = []
        for n in missing:
            if n not in seen:
                seen.add(n)
                uniq_missing.append(n)

        # 完整文件 (file_syntax_err is None) 时, missing 为空 → ok
        # 文件本身有 SyntaxError 时, 即使 partial 拿到 defs 让 missing 为空,
        # 仍要继续往后走, 让用户看到根本 SyntaxError hint (而不是看似 ok 忽略)
        if not uniq_missing and not file_syntax_err:
            return "ok", []

        # 建议正文已声明补救的符号 (补 import / from-import / 定义 X): 降级提示,
        # 避免"目标文件未定义/NameError"与建议内容打架 (MR178 4923 错位场景).
        remedied = self._extract_remedied_symbols(suggestion_text)
        remedied_missing = [n for n in uniq_missing if n in remedied]
        hard_missing = [n for n in uniq_missing if n not in remedied]

        msgs: list[str] = []
        if target_unparseable:
            # 文件本身 SyntaxError 且 partial 也无法 parse → 不能可靠判断 missing,
            # 不发"目标文件未定义 X"误报; 改为告诉用户先修根本问题.
            line_no = file_syntax_err.lineno if file_syntax_err else 0
            err_msg = file_syntax_err.msg if file_syntax_err else "unknown syntax error"
            msgs.append(
                f"**目标文件本身有 SyntaxError** (line {line_no}: {err_msg}); "
                f"apply 当前行可以, 但请先修复根本 SyntaxError 再运行文件."
            )
            return "warn", msgs
        if hard_missing:
            symbols_str = ", ".join(hard_missing[:8])
            first = hard_missing[0]
            msgs.append(
                f"**目标文件未定义** ({symbols_str}) — "
                f"apply 后会 `NameError`；请先在文件里 `add {first} = <value>` "
                f"或 `import {first}` 后再 apply"
            )
        if remedied_missing:
            syms_str = ", ".join(remedied_missing[:8])
            msgs.append(
                f"**建议要求补充** ({syms_str}) — 建议正文已说明补救方式 "
                f"(补 import / 定义)；Apply 只替换当前行，请按建议手动补上后再运行"
            )
        # partial parse 成功 (target_unparseable=False) 时也要附加 file-level hint,
        # 让用户知道目标文件本身有错, missing 列表可能不完整 (例如 partial 后段
        # 还有 defs 导致 missing 漏报, 即使 uniq_missing 为空也建议先修根本).
        if file_syntax_err:
            line_no = file_syntax_err.lineno or 0
            err_msg = file_syntax_err.msg or "unknown"
            msgs.append(
                f"ℹ️ 目标文件本身另有 SyntaxError (line {line_no}: {err_msg}); "
                f"partial parse 后 missing 列表可能不完整, 请先修复根本问题."
            )

        return "warn", msgs

    @staticmethod
    def _extract_remedied_symbols(text: str) -> set[str]:
        """从建议正文里提取"已声明补救"的符号.

        命中模式: `from X import A` / `import A` / `补 A` / `添加 A` / `定义 A` 等.
        这些符号虽然当前不在目标文件中, 但建议正文已经明确给出补救方式,
        静态检测不应再报 "apply 后会 NameError" 的错位警告.
        """
        found: set[str] = set()
        if not text:
            return found
        # from X import A, B as C / from X import (A, B)
        for m in re.finditer(r"\bfrom\s+[\w.]+\s+import\s+\(?([^()（）\n]+)\)?", text):
            for part in m.group(1).split(","):
                name = part.strip().split(" as ")[0].strip()
                if re.fullmatch(r"[A-Za-z_]\w*", name):
                    found.add(name)
        # import X / import X.Y / import X as Y / import X, Y
        for m in re.finditer(
            r"\bimport\s+([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*(?:\s+as\s+[A-Za-z_]\w*)?"
            r"(?:\s*,\s*[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*(?:\s+as\s+[A-Za-z_]\w*)?)*)",
            text,
        ):
            for part in m.group(1).split(","):
                name = part.strip().split(" as ")[0].strip()
                base = name.split(".")[0]
                if re.fullmatch(r"[A-Za-z_]\w*", base):
                    found.add(base)
        # 中文: 补/添加/定义/引入/新增/补充 X (可带反引号 / import 前缀)
        for m in re.finditer(
            r"(?:补|添加|定义|引入|新增|补充)\s+[`\s]*(?:(?:from\s+[\w.]+\s+import\s+)|(?:import\s+))?([A-Za-z_]\w*)",
            text,
        ):
            found.add(m.group(1))
        return found

    def _build_summary_v2(
        self,
        inline_posted: list[dict[str, Any]],
        inline_skipped: list[dict[str, Any]],
        total_agent_suggestions: int,
    ) -> str:
        """生成 V{N} 格式的顶层 summary, 只展示本次循环内新发布的 suggestions.

        与旧 _merge_chunks 生成的 summary_md 区别:
        - 旧: 基于 LLM 给的所有 suggestions (含 dedup skip 的) → 看起来像汇总
        - 新: 基于 inline_posted (本次实际发布到 GitLab 的) → 只显示本次新发现
        - 标题: `改进总览 V{N}` (该 MR 第几次 improve 触发, N 从 1 开始)

        返回 markdown 字符串. 如果本次没有任何新发布, 返回 "未发现新问题." 占位.
        """
        # V{N} 版本号: 该 MR 第几次 improve
        try:
            from reviewagent.telemetry.store import get_store
            store = get_store()
            runs = store.list_runs(
                project_id=self.project_id,
                mr_iid=self.mr_iid,
                command="improve",
                limit=1000,
            )
            version = len(runs) or 1  # 含本次正在跑的, 所以就是当前这次是第 N 次; 0 条时至少为 1
        except Exception as e:
            logger.warning("improve.summary_version_query failed (non-fatal): {}", e)
            version = 1

        if not inline_posted:
            # 没有新发布: 区分两种情况
            skipped_dup = sum(1 for s in inline_skipped if s.get("reason") in ("duplicate_at_line", "duplicate_fingerprint"))
            skipped_other = len(inline_skipped) - skipped_dup
            if skipped_dup and not skipped_other:
                # 全部是重复 (LLM 跟之前识别了同样问题) — 表示本次确实没新发现
                return (
                    f"## 改进总览 V{version}\n\n"
                    f"✅ 本次未发现新问题（已发过的 {skipped_dup} 条跳过，重复 issue 不重复发）\n\n"
                    f"如需主动清理历史建议, 可逐条 `/dismiss [理由]` 或合并 MR 后由下轮触发重检视."
                )
            if skipped_other and not skipped_dup:
                # 全部是校验失败 (LLM 给的建议但都没通过校验) — 表示 LLM 想提但都提不出
                return (
                    f"## 改进总览 V{version}\n\n"
                    f"ℹ️ 本次未发布建议（{skipped_other} 条建议因行号/校验未通过未发布）\n\n"
                    f"如果反复看到此提示, 检视能力可能需调整 (见 ReviewAgent 文档)."
                )
            # 都没 inline_posted 也没 skipped — 真的空
            return (
                f"## 改进总览 V{version}\n\n"
                f"✅ 本次未发现新问题.\n\n"
                f"如需主动发现潜在问题, 可在 MR 评论中 `/improve` 强制重检视."
            )

        items: list[str] = []
        for entry in inline_posted:
            norm = entry.get("normalised") or {}
            raw = entry.get("raw") or {}
            fp = norm.get("file") or raw.get("file", "")
            line = norm.get("new_line") or raw.get("start_line", "")
            header = (norm.get("header") or raw.get("header") or "").strip()
            severity = (norm.get("severity") or raw.get("severity") or "medium").upper()
            label = norm.get("label") or raw.get("label") or ""
            rationale = (norm.get("rationale") or raw.get("rationale") or "").strip()
            kind = entry.get("kind", "inline")
            kind_tag = "" if kind == "inline" else " [仅评论, 无 Apply]"
            # 简短版: rationale 第一句 (按 。/， 截断)
            short = rationale.split("。")[0].split("，")[0]
            line_str = f"L{line}" if line else ""
            if line_str:
                items.append(
                    f"- **`{fp}` `{line_str}`** — **{header}** [{severity}/{label}]{kind_tag}: {short}"
                )
            else:
                items.append(
                    f"- **`{fp}`** — **{header}** [{severity}/{label}]{kind_tag}: {short}"
                )

        body = "\n".join(items)
        skipped_dup = sum(
            1 for s in inline_skipped
            if s.get("reason") in ("duplicate_at_line", "duplicate_fingerprint")
        )
        skipped_other = len(inline_skipped) - skipped_dup

        summary = f"## 改进总览 V{version}\n\n本次新发现 {len(inline_posted)} 条建议:\n\n{body}"
        if skipped_dup > 0:
            summary += f"\n\n> ℹ️ 另有 {skipped_dup} 条已发过 (重复 issue 跳过)"
        if skipped_other > 0:
            summary += f"\n\n> ⚠️ 另有 {skipped_other} 条因校验未通过未发布"
        return summary

    def _publish(self, agent_result: dict[str, Any]) -> dict[str, Any]:
        summary_md = (agent_result.get("summary_md") or "").strip()
        suggestions = agent_result.get("suggestions") or []
        if not isinstance(suggestions, list):
            raise OpencodeOutputError(
                f"agent output 'suggestions' must be list, got {type(suggestions).__name__}"
            )

        line_map = self._diff_line_map()
        file_sources: dict[str, list[str]] = {}

        # === Head SHA 提前到循环外算一次 (race 修复 Fix A) ===
        # Why: 之前在 dedup check 和 record_suggestion 里各调一次 _get_mr_head_sha (网络),
        #      6 条 suggestion 会触发 12 次 get_mr_diff_refs 调用, 单条 200-800ms 网络往返.
        #      串行叠加后 ~2-9 秒, 这段时间里 _publish 完成 post_mr_discussion 但 SQLite 还没 INSERT,
        #      webhook 上的 /adopt 抢先查到 get_suggestion_by_note_id 返回 None → 误入 no_record 分支
        #      ("✅ 已采纳建议 (无历史记录)").
        # 修复: 进入循环前调用一次 _get_mr_head_sha, 把 head_sha 缓存给所有 iteration 共用.
        # 边界: _get_mr_head_sha 内部已经 try/except (网络失败返回 None), 不会阻塞发布流程.
        _publish_head_sha = self._get_mr_head_sha() or ""

        # === 0a. 顶部"检视汇总"持久评论: 循环前先创建或刷新一次 ===
        # pr_agent 风格: header "## 检视汇总" 自描述锚点, list_mr_notes 本地过滤
        # 找到则 update, 没找到则 post (不存 note_id 到 DB, 避免 schema 迁移).
        # Why 先于 placeholder 创建: GitLab UI 按 created_at 升序展示, 检视汇总要排在
        #      改进总览 V{N} 之上. 失败非致命, 循环结束后还会再调一次.
        try:
            publish_overview(
                project_id=self.project_id, mr_iid=self.mr_iid,
                inline_posted_count=len(suggestions),
                head_sha=_publish_head_sha,
                gitlab=self.gitlab,
                run_late_detect=True,
            )
        except Exception as _e:
            logger.warning("improve.overview_initial_failed (non-fatal): {}", _e)

        # === 0b. 顶层 summary placeholder: 必须在 inline 循环之前先发, ===
        # === 这样 GitLab UI 按 created_at 排序时 summary 永远在该 run 顶部. ===
        # Why: 之前 V{N} 实现的 placeholder 创建位置写在循环后, 仍排 inline 之后.
        #      修复: placeholder 循环前发, edit 留到循环后.
        # 顺序: 检视汇总 (0a) → 改进总览 placeholder (0b) → inline 循环 → 两者最终刷新.
        top_comment_id: int | None = None
        try:
            placeholder_body = self._build_summary_placeholder([], [], len(suggestions))
            top_comment_id = self.gitlab.post_mr_comment(
                self.project_id, self.mr_iid, placeholder_body
            )
        except GitLabError as e:
            logger.warning(
                "improve.post_summary_placeholder_failed (non-fatal) project={} mr={} err={}",
                self.project_id, self.mr_iid, e,
            )

        # 1. 每条 suggestion：先校验 new_line + improved_code 对齐
        # 注意: 顶层 summary 不再这里发, 改到循环结束后基于 inline_posted 重新生成.
        # Why: 之前 summary 在循环前基于 agent 给的所有 suggestions 生成, 会包含
        #      被 dedup skip 掉的旧项, 看起来像"汇总上次"而非"本次新发现".
        #      现在 summary 只显示本次循环内 inline_posted 的内容, 标题带 V{N} 版本号.
        inline_posted: list[dict[str, Any]] = []
        inline_skipped: list[dict[str, Any]] = []

        # 0c. Pre-reconcile: 发布前先同步 GitLab resolved 状态到 DB.
        # Why: 用户可能在上一轮检视后于 GitLab UI 点了 ✓ resolve / Apply,
        #      但如果 GitLab 没发 webhook (或 webhook 被 cooldown 吞), DB 里
        #      这些 suggestion 还是 open → dedup 第一查 (processed states) miss
        #      → 同位置重复发布 (MR301 V2 重复 V1 的根因之一).
        #      发布前扫一次, 把 GitLab 已 resolved 的标 resolved, dedup 就能命中.
        try:
            from reviewagent.commands.suggestion_actions import _scan_and_mark_resolved_silent
            _pre_reconcile = _scan_and_mark_resolved_silent(
                project_id=self.project_id, mr_iid=self.mr_iid,
                actor_username="improve_pre_publish_reconcile",
                adoption_source="improve_pre_publish",
                reason="improve 发布前 pre-reconcile: GitLab UI 已 resolve 但 DB 还 open",
                validation_status="improve_pre_publish_reconcile",
            )
            if _pre_reconcile.get("updated"):
                logger.info(
                    "improve.pre_publish_reconcile updated={} note_ids={}",
                    _pre_reconcile["updated"],
                    [n[:8] for n in _pre_reconcile["note_ids"]],
                )
        except Exception as e:
            logger.warning("improve.pre_publish_reconcile failed (non-fatal): {}", e)

        for raw in suggestions:
            try:
                normalised = self._normalise_suggestion(raw)
            except ValueError as e:
                logger.warning(
                    "improve.skip_invalid_suggestion project={} mr={} sugg={} err={}",
                    self.project_id, self.mr_iid, raw, e,
                )
                inline_skipped.append({"suggestion": raw, "reason": str(e)})
                continue

            file_path = normalised["file"]
            start_line = normalised["new_line"]
            improved = normalised["improved_code"]
            existing = (raw.get("existing_code") or "").strip("\n") if isinstance(raw, dict) else ""
            decision = self._validate_suggestion(
                file_path=file_path,
                start_line=start_line,
                improved_code=improved,
                existing_code=existing,
                line_map=line_map,
                file_sources=file_sources,
            )
            # === MR1121 修复: rationale 明确说目标行不在 VALID NEW LINES 内 → 强制降级 general.
            # Why: LLM 有时在 rationale 里写"降级为普通评论"，但 improved_code 仍指向
            #      VALID NEW LINES 里的其他行 (占位锚点), 导致建议指向错误代码.
            # 检测: rationale 包含"不在 VALID NEW LINES" / "不在本次 diff" / "无法提供可 Apply" 等关键词.
            if decision["action"] == "post":
                _rationale = (normalised.get("rationale") or "").lower()
                _not_in_diff_keywords = (
                    "不在 valid new lines",
                    "不在本次 diff",
                    "无法提供可 apply",
                    "降级为普通评论",
                    "not in valid new lines",
                    "not in diff",
                )
                if any(kw in _rationale for kw in _not_in_diff_keywords):
                    logger.info(
                        "improve.force_general_not_in_diff project={} mr={} file={} line={} rationale={}",
                        self.project_id, self.mr_iid, file_path, start_line,
                        (normalised.get("rationale") or "")[:80],
                    )
                    decision = {"action": "general", "new_line": start_line,
                                "reason": "rationale indicates target not in VALID NEW LINES"}
            # === A. 跨次去重 — 守卫所有发布动作 (post / general).
            # Why: 此前 dedup 只在 action == "post" 分支内, 收缩建议
            #      (action == "general") 走另一条路径直接发评论, 同一行
            #      在第二轮 improve 时会重复发 [issue: MR155 line 12 x 2].
            #      把 dedup 提前到 _validate_suggestion 之后, 让所有
            #      会发到 GitLab 的分支都先查重.
            if decision["action"] in ("post", "general"):
                _sev = (normalised.get("severity") or "medium").lower()
                _head_sha = _publish_head_sha  # Fix A: 用循环前缓存值, 避免每条重复网络调用
                try:
                    from reviewagent.telemetry.store import get_store as _dedup_store
                    _dedup_db = _dedup_store()
                    # rule_keys 用于 dedup 精确化: 不同规则即使 (file, line±2) 重叠
                    # 也不应误杀 (例: NO-MUTABLE-DEFAULT L10 vs NO-LOG-EXC L12)
                    _raw_rks = (raw.get("rule_keys") if isinstance(raw, dict) else None)
                    if isinstance(_raw_rks, list):
                        _dedup_rks_list = [str(x) for x in _raw_rks if x]
                    else:
                        _dedup_rks_list = []
                    # === 兜底: LLM 不输出 rule_keys 字段时, 从 rationale/header
                    #     抽 R-XXX / R-OTHER:* / SSD-RULE-* 前缀, 避免 dedup 退化为
                    #     纯 (file, line) 兜底而误杀不同规则的建议
                    #     (MR 239 other.py L4 medium R-OTHER:magic_number
                    #      被 L2 low SSD-RULE-TYPEHINTS 命中 → skip).
                    if not _dedup_rks_list:
                        try:
                            _dedup_text = (
                                (normalised.get("rationale") or "")
                                + "\n"
                                + (normalised.get("header") or "")
                            )
                            _dedup_rks_list = _RULE_REF_REGEX.findall(_dedup_text)
                        except Exception:
                            _dedup_rks_list = []
                    _dedup_rks = ",".join(_dedup_rks_list)
                    if _dedup_db.suggestion_exists_at_line(
                        self.project_id, self.mr_iid, file_path,
                        decision["new_line"], _sev, head_sha=_head_sha,
                        line_tolerance=2,
                        rule_keys=_dedup_rks or None,
                        existing_code=existing or "",
                    ):
                        logger.info(
                            "improve.skip_at_line project={} mr={} file={} line={} severity={} head_sha={}",
                            self.project_id, self.mr_iid, file_path,
                            decision["new_line"], _sev,
                            _head_sha[:8] if _head_sha else "",
                        )
                        inline_skipped.append({"suggestion": raw, "reason": "duplicate_at_line"})
                        continue
                    import hashlib as _dedup_hl
                    # Fingerprint (file:line:header_normalized) — 同位置同类问题永远同 fp.
                    # helper 在模块顶部定义, record_suggestion 处复用.
                    _dedup_fingerprint = _suggestion_fingerprint(
                        file_path, decision["new_line"], normalised.get("header")
                    )
                    # Z3+: fingerprint dedup 也传 head_sha, 跟 line dedup 语义保持一致.
                    # 已处理状态 (applied/dismissed/resolved) 永远命中;
                    # state='open' 必须 head_sha 一致才命中 (force-push 残留 → 放行).
                    if _dedup_db.suggestion_exists_by_fingerprint(
                        self.project_id, self.mr_iid, _dedup_fingerprint,
                        head_sha=_head_sha,
                    ):
                        logger.info(
                            "improve.skip_duplicate project={} mr={} file={} line={} fingerprint={}",
                            self.project_id, self.mr_iid, file_path,
                            decision["new_line"], _dedup_fingerprint,
                        )
                        inline_skipped.append({"suggestion": raw, "reason": "duplicate_fingerprint"})
                        continue
                except Exception as _e:
                    logger.warning("improve.dedup_check failed (non-fatal): {}", _e)

            if decision["action"] == "post":
                # === apply 风险检测: 引用了 missing 符号时, 在 review 中加 ⚠️ 提示
                # 永远走 post 路径 (保留 Apply 按钮), reviewer 自己决定怎么处理
                _risk_level, _risk_msgs = self._detect_apply_risk(
                    file_path=file_path,
                    improved_code=decision.get("normalised_code") or normalised["improved_code"],
                    file_sources=file_sources,
                    suggestion_text=(
                        f"{normalised.get('rationale') or ''}\n"
                        f"{normalised.get('header') or ''}"
                    ),
                )
                _warn_block = ""
                if _risk_level == "warn" and _risk_msgs:
                    _warn_block = (
                        "> ⚠️ **apply 前请确认** — 建议引入了目标文件中尚未存在的符号；\n"
                        + "\n".join(f"> • {m}" for m in _risk_msgs) + "\n"
                        + "> 💡 apply 后请补全缺失的 import / 常量 / 方法, 否则会 NameError 或 ImportError.\n\n"
                    )
                    logger.info(
                        "improve.apply_risk_warn project={} mr={} file={} line={} missing={}",
                        self.project_id, self.mr_iid, file_path, decision["new_line"],
                        _risk_msgs,
                    )
                body_to_post = normalised["body"]
                nc = decision.get("normalised_code") or normalised["improved_code"]
                n_lines = len(nc.split("\n"))
                if nc != normalised["improved_code"]:
                    logger.info(
                        "improve.fix_indent project={} mr={} file={} line={}",
                        self.project_id, self.mr_iid, file_path, decision["new_line"],
                    )
                    sev = normalised.get("severity", "medium").upper()
                    # suggestion:-0+N: +N = N lines AFTER comment line
                    # 替换 N+1 行 (注释行 + N 行后续行)
                    # 要替换 len(existing_lines) 行 → N = len - 1
                    existing = (raw.get("existing_code") or "").strip("\n") if isinstance(raw, dict) else ""
                    existing_lines = existing.split("\n") if existing else []
                    n_replace = max(0, len(existing_lines) - 1)
                    body_to_post = (
                        f"**[{sev}]** **{normalised['header']}** — {normalised['label']}\n\n"
                        f"{normalised['rationale']}\n\n"
                        f"{_warn_block}"
                        f"```suggestion:-0+{n_replace}\n{nc}\n```"
                        + self.HELP_TEXT_FOOTER
                    )
                    _warn_block = ""  # 已结构化注入, 避免下面重复

                # === apply 风险: else 分支 (body_to_post 直接用 normalised["body"]) 也需注入 ===
                if _warn_block:
                    # 结构化注入: warn_block 紧贴 suggestion 块上方
                    idx = body_to_post.find("```suggestion")
                    if idx != -1:
                        body_to_post = body_to_post[:idx] + _warn_block + body_to_post[idx:]

                note_id = self.gitlab.post_mr_discussion(
                    self.project_id,
                    self.mr_iid,
                    body_to_post,
                    file_path=file_path,
                    new_line=decision["new_line"],
                )
                if note_id:
                    inline_posted.append({
                        "note_id": note_id,
                        "raw": raw if isinstance(raw, dict) else {},
                        "normalised": normalised,
                        "kind": "inline",
                    })
                    logger.info(
                        "improve.post_inline project={} mr={} file={} line={}",
                        self.project_id, self.mr_iid, file_path, decision["new_line"],
                    )
                    # 实时刷新顶部"检视汇总": 每发一条 inline 就 update 一次,
                    # 让 reviewer 在 GitLab UI 上看到汇总表随检视实时生长.
                    # 失败非致命 (网络抖动 / 限速), 循环后还会再刷一次最终版.
                    try:
                        publish_overview(
                            project_id=self.project_id, mr_iid=self.mr_iid,
                            inline_posted_count=len(inline_posted),
                            head_sha=_publish_head_sha,
                            gitlab=self.gitlab,
                            run_late_detect=False,
                        )
                    except Exception as _e:
                        logger.warning(
                            "improve.overview_refresh_after_post failed (non-fatal): {}",
                            _e,
                        )
                    # 记录 suggestion 到 telemetry (用于后续 /adopt 验证 + 跨次去重)
                    try:
                        from reviewagent.telemetry.store import get_store
                        head_sha = _publish_head_sha  # Fix A: 用循环前缓存值, race window 几乎归零
                        existing = (raw.get("existing_code") or "").strip("\n") if isinstance(raw, dict) else ""
                        # === 跨次去重: file+line+existing_code 的指纹 ===
                        import hashlib as _hl
                        # LLM 不输出 rule_keys 字段 → 从 rationale 文本里抽 R-XXX / R-OTHER / SSD-RULE-* 前缀
                        _rationale = (normalised.get("rationale") or "") if isinstance(normalised, dict) else ""
                        _header = (normalised.get("header") or "") if isinstance(normalised, dict) else ""
                        _rk = re.findall(r"(?:^|[^A-Z0-9-])(R-OTHER-IMPACT:[a-z0-9_]+|R-OTHER:[a-z0-9_]+|R-[A-Z]+(?:-[A-Z0-9_]+)*|SSD-RULE-[A-Z0-9-]+)", _rationale)
                        _rk = list(dict.fromkeys(_rk))  # 去重保序
                        rule_keys = (raw.get("rule_keys") if isinstance(raw, dict) else None) or _rk
                        # 后处理: SSD 硬规则 violation 优先用 SSD-RULE-* key.
                        # 常见情况: LLM 看到 `from X import *` 但 rule_keys 输出 R-OTHER-IMPACT:wildcard_import,
                        # 应该是 SSD-RULE-FORBIDDEN-WILDCARD-IMPORT (hard-stop).
                        if "SSD-RULE-FORBIDDEN-WILDCARD-IMPORT" not in rule_keys:
                            if "wildcard" in _header.lower() or "wildcard" in _rationale.lower() or "from " in _rationale and "import *" in _rationale:
                                rule_keys = list(rule_keys) + ["SSD-RULE-FORBIDDEN-WILDCARD-IMPORT"]
                        # 与 dedup_check 同一 helper — 保证两边 fp 一致.
                        fingerprint = _suggestion_fingerprint(
                            file_path, decision["new_line"], normalised.get("header")
                        )
                        content_fingerprint = _suggestion_content_fingerprint(existing)
                        # cohort_key 用 file + header (不用 rule_keys / line):
                        # rule_keys LLM 可能不输出 (→ 空 → key 不稳定);
                        # line 在 apply 后会漂移. header 同文件同类问题稳定.
                        _cohort_hdr = re.sub(r"\s+", " ", (normalised.get("header") or "").strip().lower())
                        cohort_key = _hl.sha256(
                            f"{file_path}:{_cohort_hdr}".encode("utf-8")
                        ).hexdigest()[:24]
                        get_store().record_suggestion(
                            project_id=self.project_id,
                            mr_iid=self.mr_iid,
                            note_id=note_id,
                            file_path=file_path,
                            target_line=decision["new_line"],
                            target_line_end=(decision["new_line"] + n_lines - 1) if n_lines > 1 else decision["new_line"],
                            existing_code=existing,
                            improved_code=nc,
                            header=normalised.get("header"),
                            severity=normalised.get("severity"),
                            head_sha=head_sha,
                            label=normalised.get("label"),
                            rule_keys=rule_keys,
                            one_sentence_summary=(raw.get("one_sentence_summary") or normalised.get("rationale")) if isinstance(raw, dict) else None,
                            importance=raw.get("importance") if isinstance(raw, dict) else None,
                            fingerprint=fingerprint,
                            content_fingerprint=content_fingerprint,
                            cohort_key=cohort_key,
                        )
                        # Batch3: 同 cohort 上一代是 applied/dismissed 且当前问题
                        # 仍然存在 → supersede 旧记录, 把新一条标 generation+1.
                        try:
                            _handle_cohort_reoccurrence(
                                store=get_store(),
                                project_id=self.project_id, mr_iid=self.mr_iid,
                                cohort_key=cohort_key,
                                new_note_id=note_id,
                                file_path=file_path,
                                target_line=decision["new_line"],
                                target_line_end=(
                                    decision["new_line"] + n_lines - 1
                                ) if n_lines > 1 else decision["new_line"],
                                existing=existing,
                                head_sha=head_sha,
                                gitlab=self.gitlab,
                            )
                        except Exception as _e:  # noqa: BLE001
                            logger.warning(
                                "improve.cohort_reoccurrence failed (non-fatal): {}", _e,
                            )
                    except Exception as e:
                        logger.warning("improve.record_suggestion failed: {}", e)
                else:
                    inline_skipped.append({"suggestion": raw, "reason": "gitlab_rejected"})
            elif decision["action"] == "general":
                # 收缩建议: 发普通评论（无 Apply 按钮），让用户看到建议但不能一键删除代码
                sev = normalised.get("severity", "medium").upper()
                general_body = (
                    f"**[{sev}]** **{normalised['header']}** — {normalised['label']}\n\n"
                    f"{normalised['rationale']}\n\n"
                    f"> ⚠️ 该建议涉及代码收缩（删除行），请人工评估后手动修改"
                    + self.HELP_TEXT_FOOTER
                )
                note_id = self.gitlab.post_mr_discussion(
                    self.project_id,
                    self.mr_iid,
                    general_body,
                    file_path=file_path,
                    new_line=decision["new_line"],
                )
                if note_id:
                    inline_posted.append({
                        "note_id": note_id,
                        "raw": raw if isinstance(raw, dict) else {},
                        "normalised": normalised,
                        "kind": "general",
                    })
                    logger.info(
                        "improve.post_general project={} mr={} file={} line={} reason={}",
                        self.project_id, self.mr_iid, file_path, decision["new_line"],
                        decision["reason"],
                    )
                    try:
                        publish_overview(
                            project_id=self.project_id, mr_iid=self.mr_iid,
                            inline_posted_count=len(inline_posted),
                            head_sha=_publish_head_sha,
                            gitlab=self.gitlab,
                            run_late_detect=False,
                        )
                    except Exception as _e:
                        logger.warning(
                            "improve.overview_refresh_after_general failed (non-fatal): {}",
                            _e,
                        )
                else:
                    inline_skipped.append({"suggestion": raw, "reason": "gitlab_rejected"})
            else:
                # action == "drop" — 不发任何评论, 仅记 telemetry
                inline_skipped.append({"suggestion": raw, "reason": decision["reason"]})

        # 3. 顶部"检视汇总"最终刷新 (含 skipped 计数 + 完整 inline_posted).
        #    循环里每条 inline 已实时刷过, 此处再做一次以确保最终状态完整.
        try:
            publish_overview(
                project_id=self.project_id, mr_iid=self.mr_iid,
                inline_posted_count=len(inline_posted),
                head_sha=_publish_head_sha,
                gitlab=self.gitlab,
                run_late_detect=True,
            )
        except Exception as _e:
            logger.warning("improve.overview_final_failed (non-fatal): {}", _e)

        # 4. edit placeholder 为完整 summary (含实际 inline_posted 列表).
        #    placeholder 已在循环前创建 (见顶部 step 0), 此处只更新正文.
        summary_md = self._build_summary_v2(inline_posted, inline_skipped, len(suggestions))
        if top_comment_id and summary_md:
            try:
                self.gitlab.update_mr_comment(
                    self.project_id, self.mr_iid, top_comment_id, summary_md
                )
            except GitLabError as e:
                logger.warning(
                    "improve.update_summary_failed (non-fatal) project={} mr={} err={}",
                    self.project_id, self.mr_iid, e,
                )

        return {
            "top_comment_id": top_comment_id,
            "suggestions_count": len(suggestions),
            "inline_posted": len(inline_posted),
            "inline_skipped": len(inline_skipped),
        }

    def _validate_suggestion(
        self,
        *,
        file_path: str,
        start_line: int,
        improved_code: str,
        existing_code: str = "",
        line_map: dict[str, set[int]],
        file_sources: dict[str, list[str]],
    ) -> dict[str, Any]:
        """校验 + snap — 返回 {"action": "post"|"drop", "new_line": int, "reason": str}.

        校验顺序:
          1. file 在 diff 中？否则 drop
          2. 优先用 existing_code 反查真实行号（model 经常把 start_line 写错，
             但 existing_code 内容是对的）— 比 snap 准
          3. 反查结果不在 diff valid 集合 → snap 到最近 valid
          4. improved_code 第一行 vs file[start_line-1] 不匹配 → degrade
        """
        valid = line_map.get(file_path)
        # 文件不在 diff 中（agent 乱猜）→ drop
        if valid is None:
            return {"action": "drop", "new_line": start_line,
                    "reason": f"file '{file_path}' not in diff"}

        # 预读文件
        if file_path not in file_sources:
            file_sources[file_path] = self._read_file_lines(file_path)
        file_lines = file_sources[file_path]

        # 2. 用 existing_code 反查真实行号
        actual_line: int | None = None
        if existing_code and existing_code.strip():
            actual_line = self._find_line_by_existing_code(
                file_lines, existing_code, hint_line=start_line, max_window=8,
            )
            if actual_line is not None and actual_line != start_line:
                logger.info(
                    "improve.snap_to_existing project={} mr={} file={} {} -> {} (from existing_code)",
                    self.project_id, self.mr_iid, file_path, start_line, actual_line,
                )
                start_line = actual_line
            elif actual_line is None and start_line in valid:
                # 反查失败但 start_line 在 valid set 内 — agent 给的 existing_code
                # 在文件里搜不到 (典型: 视图过期 / 行号填错 / 文件已被同步之前改动).
                # 不再走 snap + step 4 对齐 (那会 snap 到 valid 内某行然后对不上 → general,
                # 误以为 "diff 范围"问题). 视为 agent 输出不连贯, 直接 drop.
                logger.warning(
                    "improve.existing_code_not_found project={} mr={} file={} "
                    "start_line={} hint='{}'",
                    self.project_id, self.mr_iid, file_path, start_line,
                    existing_code[:80].replace("\n", " "),
                )
                return {"action": "drop", "new_line": start_line,
                        "reason": "existing_code not found near start_line in worktree — agent output inconsistent"}

        # 3. snap 到最近 valid（如果上面没改）
        if actual_line is None:
            snapped = find_nearest_valid_line(file_path, start_line, line_map)
            if snapped is None:
                return {"action": "drop", "new_line": start_line,
                        "reason": "no valid line in file"}
            if snapped != start_line:
                logger.info(
                    "improve.snap_line project={} mr={} file={} {} -> {}",
                    self.project_id, self.mr_iid, file_path, start_line, snapped,
                )
                start_line = snapped

        # 4. improved_code 第一行 vs file[start_line-1] 对齐检查
        if not file_lines or start_line - 1 >= len(file_lines):
            return {"action": "drop", "new_line": start_line,
                    "reason": "file content unavailable for alignment check"}

        target_line_raw = file_lines[start_line - 1] if start_line - 1 < len(file_lines) else ""
        target_line = target_line_raw.strip()
        imp_first = (improved_code.splitlines()[0] if improved_code else "").strip()

        # 4a. 多行替换（improved 行数 > existing 行数）→ 第一行可以完全不同
        #     场景: return open(p).read() → with open(p) as f: \n return f.read()
        #     此时第一行是 with 而原行是 return — 这是合法的 "1→N" 替换
        existing_lines = existing_code.strip("\n").split("\n") if existing_code and existing_code.strip() else []
        improved_lines = improved_code.strip("\n").split("\n") if improved_code else []
        is_multi_line_replacement = (
            bool(existing_lines)
            and len(improved_lines) > len(existing_lines)
            and existing_lines[0].strip() == target_line
        )

        if is_multi_line_replacement:
            # 信任模型: existing_code 已通过反查定位
            # suggestion:-0+{len(existing_lines)-1} 精确替换 existing 范围

            # 尾部去重: agent 有时把 existing 之后的文件行也写进 improved_code,
            # 但 -0+N 不会删除那些行, 导致 Apply 后出现重复行.
            # 检测: improved 末尾的 (improved-existing) 行是否与文件后续行一致, 是则裁掉.
            n_added = len(improved_lines) - len(existing_lines)
            if n_added > 0:
                after_start = start_line - 1 + len(existing_lines)  # 0-based
                after_lines = file_lines[after_start:after_start + n_added]
                tail_lines = improved_lines[-n_added:]
                if len(after_lines) == n_added and all(
                    t.strip() == a.strip() for t, a in zip(tail_lines, after_lines)
                ):
                    improved_lines = improved_lines[:-n_added]
                    improved_code = "\n".join(improved_lines)
                    logger.info(
                        "improve.trim_trailing_dup project={} mr={} file={} "
                        "line={} trimmed={}",
                        self.project_id, self.mr_iid, file_path,
                        start_line, n_added,
                    )

            normalised_code = self._fix_indent(target_line_raw, improved_code)
            logger.info(
                "improve.multiline_replace project={} mr={} file={} line={} existing_lines={} improved_lines={}",
                self.project_id, self.mr_iid, file_path, start_line,
                len(existing_lines), len(improved_lines),
            )
            # 更新 normalised_code 到 normalisation 结果 (在 publish 阶段生效)
            return {"action": "post", "new_line": start_line, "reason": "multi_line_replacement",
                    "normalised_code": normalised_code}

        # 4b. 结构重组旁路 (放在 4c 对齐检查之前, 防 with/try 包装被误 drop)
        #     with-wrap / try-wrap 时 improved 第一行 ≠ target_line (with 与 return 不同),
        #     但 improved 引入了新的「块起始行」 (with/try/if/class/def), 是结构重组,
        #     不应走 4c 的「首行对齐」检查, 直接放行到 4d publish.
        #     例: `f = open() / f.write / f.close` → `with open as f: / f.write`
        #         improved=[with..., f.write] < existing=[f=open, f.write, f.close]
        #         improved[0]=`with ...` ≠ target_line=`f = open(...)`, 但有 `with` 块起始.
        _BLOCK_OPEN_RE = re.compile(r'^\s*(?:with\s|try\s*:|if\s|for\s|while\s|async\s|class\s|def\s)')
        is_structural_refactor = False
        if len(improved_lines) < len(existing_lines) and improved_lines:
            norm_existing_set = {ln.strip() for ln in existing_lines if ln.strip()}
            new_block_openers = [
                ln.strip() for ln in improved_lines
                if ln.strip() and _BLOCK_OPEN_RE.match(ln.strip())
                and ln.strip() not in norm_existing_set
            ]
            if new_block_openers:
                is_structural_refactor = True
                logger.info(
                    "improve.structural_refactor project={} mr={} file={} "
                    "line={} existing={} improved={} new_block_openers={}",
                    self.project_id, self.mr_iid, file_path,
                    start_line, len(existing_lines), len(improved_lines), len(new_block_openers),
                )

        # 4c. 对齐检查
        # N→N 等行数替换: existing_code 已在文件中定位 (actual_line 非 None),
        #   agent 有意改写代码 (e.g. print→logger.info), 第一行不需要匹配.
        # 1→1 且 existing_code 未找到: 仍需校验第一行对齐, 防止模型乱发.
        # 例外: improved 为空 (删除整段) → 不需要对齐第一行
        # 例外: 结构重组 (4b 旁路命中) → with/try 包装首行 ≠ target_line, 跳过对齐
        is_same_line_count = bool(existing_lines) and len(improved_lines) == len(existing_lines)
        if not improved_lines:
            pass  # 删除场景, 不需要对齐
        elif is_structural_refactor:
            pass  # 结构重组旁路: 跳过对齐检查, 直接放行
        elif not (is_same_line_count and actual_line is not None):
            if not _code_first_line_matches(target_line, imp_first):
                return {"action": "drop", "new_line": start_line,
                        "reason": f"improved_code first line doesn't match file:{start_line} ({target_line!r} vs {imp_first!r})"}

        # 4d. 收缩检查: M < N 时区分「真删行」与「结构重组」
        #     结构重组 (上面 4b 已识别, 已放行) → publish
        #     真删 (improved ⊂ existing, 没新内容) → general (无 Apply), 防 agent 误删
        # 例外: M == 0 且 existing 非空 → 整段删除 (duplicated_definition / dead_code),
        #     允许多行删除 suggestion 走到 publish 阶段.
        if len(improved_lines) == 0 and existing_lines:
            logger.info(
                "improve.delete_range project={} mr={} file={} "
                "line={} delete_lines={}",
                self.project_id, self.mr_iid, file_path,
                start_line, len(existing_lines),
            )
        elif len(improved_lines) < len(existing_lines) and not is_structural_refactor:
            # 走到这里说明: M < N 且 没引入新块起始行 → 真删, 降级 general
            logger.info(
                "improve.shrink_to_general project={} mr={} file={} "
                "line={} existing={} improved={}",
                self.project_id, self.mr_iid, file_path,
                start_line, len(existing_lines), len(improved_lines),
            )
            return {"action": "general", "new_line": start_line,
                    "reason": f"shrinking suggestion ({len(existing_lines)} -> {len(improved_lines)} lines)"}

        # 4d. 缩进修正: 若 improved_code 第一行缺缩进, 自动补齐
        normalised_code = self._fix_indent(target_line_raw, improved_code)

        return {"action": "post", "new_line": start_line, "reason": "ok",
                "normalised_code": normalised_code}

    @staticmethod
    def _score_suggestion(s: dict) -> int:
        """对单条建议评分 (0~100), 用于过滤低质量建议."""
        score = 0
        # severity (0~35)
        sev_map = {"high": 30, "medium": 20, "low": 10}
        score += sev_map.get((s.get("severity") or "medium").lower(), 20)
        # label (0~30) — cross-file impact 与 potential bug 同级, 体现 P1 优先级
        label_map = {
            "security": 30, "potential bug": 25, "cross-file impact": 25, "performance": 20,
            "enhancement": 15, "code quality": 12, "style": 5,
        }
        score += label_map.get((s.get("label") or "").lower(), 10)
        # rationale 长度 (0~20)
        rationale = (s.get("rationale") or "")
        if len(rationale) > 200:
            score += 20
        elif len(rationale) > 100:
            score += 15
        elif len(rationale) > 50:
            score += 8
        # 规则引用 (+10) — 仅 {prefix}-RULE 形式加分
        if _RULE_EXEMPT_REGEX.search(rationale):
            score += 10
        # header 质量 (+5)
        header = (s.get("header") or "").strip()
        if len(header) >= 4 and header != "建议改进":
            score += 5
        return min(100, score)

    @staticmethod
    def _fix_indent(target_line: str, improved_code: str) -> str:
        """确保 improved_code 第一行的缩进 == target_line 的缩进.

        模型有时会忘记给 improved 第一行加缩进, 导致 Apply 后格式错乱.
        例如: target=`    q = f"..."`, improved=`q = "..."\n    return ...`
        → 修正为 `    q = "..."\n    return ...`
        """
        lines = improved_code.split("\n")
        if not lines or not target_line:
            return improved_code
        # 提取 target 的前导空白
        target_indent = target_line[: len(target_line) - len(target_line.lstrip())]
        # 第一行当前前导空白
        first = lines[0]
        first_indent_len = len(first) - len(first.lstrip())
        target_indent_len = len(target_indent)
        if first_indent_len < target_indent_len:
            # 补齐缺失的缩进
            pad = target_indent[first_indent_len:]
            lines[0] = pad + first
            return "\n".join(lines)
        return improved_code

    @staticmethod
    def _normalise_suggestion(s: dict[str, Any]) -> dict[str, Any]:
        """校验 + 构造 GitLab "Apply suggestion" inline comment body."""
        if not isinstance(s, dict):
            raise ValueError(f"suggestion must be dict, got {type(s).__name__}")
        file_path = s.get("file")
        if not file_path or not isinstance(file_path, str):
            raise ValueError("missing 'file' (str)")
        start_line = s.get("start_line")
        if not isinstance(start_line, int) or start_line <= 0:
            raise ValueError("missing 'start_line' (int > 0)")
        existing = (s.get("existing_code") or "").strip("\n")
        improved = (s.get("improved_code") or "").strip("\n")
        # 空 improved_code 表示删除整段 existing (GitLab suggestion:-0+(N-1) + 空 body)
        # 典型场景: duplicated_definition / dead_code, agent 不知怎么 "删除 N 行"
        # 要求: existing 必须非空, 否则 raise 防止误删
        if not improved and not existing:
            raise ValueError("missing 'improved_code' (non-empty) and empty 'existing_code'")

        header = (s.get("header") or "建议改进").strip()
        rationale = (s.get("rationale") or "").strip()
        label = (s.get("label") or "enhancement").strip()
        severity = (s.get("severity") or "medium").strip().lower()

        # GitLab suggestion 格式: ```suggestion:-A+B
        # - A = 注释行往上删除的行数 (0 = 不删上方行)
        # - B = 注释行往下 (含注释行自身) 替换的行数
        #   GitLab 语义: +B = B lines AFTER the commented line
        #   总替换 = A + 1 (注释行) + B = A + B + 1
        #   要替换 N 行 existing → B = N - 1
        # 例: 替换 line 59~62 (4行) → suggestion:-0+3
        # 注意: -M 会删除 new_line 上方的 M 行，绝对不能用 existing 行数做 M!
        existing_lines = existing.split("\n") if existing else []
        n_replace = max(0, len(existing_lines) - 1)

        body = (
            f"**[{severity.upper()}]** **{header}** — {label}\n\n"
            f"{rationale}\n\n"
            f"```suggestion:-0+{n_replace}\n{improved}\n```"
            + ImproveCommand.HELP_TEXT_FOOTER
        )
        return {
            "file": file_path,
            "new_line": start_line,
            "improved_code": improved,
            "header": header,
            "rationale": rationale,
            "label": label,
            "severity": severity,
            "body": body,
        }


def _code_first_line_matches(target_line: str, improved_first: str) -> bool:
    """启发式: target / improved 第一行是否同一行（容忍空格/标点差异）.

    规则（任一命中即视为同一行）:
      1. 字符串相等（strip 后）
      2. 都是 def 行且函数名一致
      3. 都是 class 行且类名一致
      4. 都是赋值行且左侧变量名一致（`q = ...` vs `q = ...`）
      5. 都是 return 行且第一个 token 一致
      6. 共享 ≥3 字符标识符（去掉通用 stop token）
    """
    import re

    if not target_line or not improved_first:
        return False
    if target_line == improved_first:
        return True

    # 0. 前缀必须一致（去除前导空白后前 4 字符一致）— 防止 "sys.stderr.write" vs "def log_event"
    # 这种整行重写的情况
    # 注：调用方应在调用前已经判定 "这是多行替换" (improved 行数 > existing 行数),
    # 在那种情况下 improved_first 不需要和 target_line 是同类操作 (e.g. return → with),
    # 所以多行替换应当从外部直接放行, 不进此函数
    t_pref = target_line.lstrip()[:4]
    f_pref = improved_first.lstrip()[:4]
    if t_pref != f_pref:
        return False

    def _lstrip(s: str) -> str:
        return s.lstrip()

    t = _lstrip(target_line)
    f = _lstrip(improved_first)

    # 同 def 行（函数名一致）
    if t.startswith("def ") and f.startswith("def "):
        m_t = re.match(r"def\s+(\w+)", t)
        m_f = re.match(r"def\s+(\w+)", f)
        if m_t and m_f and m_t.group(1) == m_f.group(1):
            return True

    # 同 class 行
    if t.startswith("class ") and f.startswith("class "):
        m_t = re.match(r"class\s+(\w+)", t)
        m_f = re.match(r"class\s+(\w+)", f)
        if m_t and m_f and m_t.group(1) == m_f.group(1):
            return True

    # except 行（bare except → typed except）— 两者都以 except: 开头
    if t.startswith("except") and f.startswith("except"):
        # 都是 except 关键字开头, 即便 f 加了 (X, Y) 参数也算同一行
        return True

    # 赋值 / 调用同名前缀
    m_t = re.match(r"([A-Za-z_]\w*)\s*[=(.]", t)
    m_f = re.match(r"([A-Za-z_]\w*)\s*[=(.]", f)
    if m_t and m_f and m_t.group(1) == m_f.group(1):
        return True

    # return 行（共享 ≥5 字符非 stop 标识符 — 同一行 return ... 但内部计算表达式不同）
    # 停用词只过滤关键字，不过滤数据键名（price/items 是判断"同一行"的关键信号）
    if t.startswith("return ") and f.startswith("return "):
        _stop = {"def", "return", "self", "for", "while", "in", "is", "and", "or", "not",
                 "if", "else", "elif", "import", "from", "class", "try", "except",
                 "finally", "with", "as", "raise", "pass", "lambda", "yield"}
        t_toks = set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{4,}\b", t)) - _stop
        f_toks = set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{4,}\b", f)) - _stop
        return bool(t_toks & f_toks)

    # token overlap（≥3 字符，去 stop — 包括关键字 + Python 内置函数）
    stop = {"def", "return", "self", "None", "True", "False", "and", "or",
            "not", "if", "else", "elif", "for", "while", "in", "is",
            "import", "from", "class", "try", "except", "finally",
            "with", "as", "raise", "pass", "lambda", "yield",
            # Python builtins: 不同行共用它们不等于 "同一行"
            "open", "close", "read", "write", "print", "len", "str", "int",
            "list", "dict", "set", "tuple", "sum", "min", "max", "abs",
            "range", "type", "isinstance", "getattr", "setattr", "delattr",
            "hasattr", "callable", "repr", "format", "hash", "id", "iter",
            "next", "map", "filter", "zip", "enumerate", "sorted", "reversed",
            "any", "all", "bool", "bytes", "bytearray", "complex", "float",
            "frozenset", "object", "property", "staticmethod", "classmethod",
            "super", "input", "eval", "exec", "compile", "globals", "locals",
            "vars", "dir", "chr", "ord", "hex", "oct", "bin", "round",
            "divmod", "pow", "slice", "memoryview", "ascii", "breakpoint",
            "__init__", "__name__", "__main__", "__file__", "__doc__",
            "json", "csv", "sys", "os", "sqlite3",
            # 跨 `os.environ.get(...)` 等通用调用模板
            "environ", "getenv", "get", "set", "items", "keys", "values",
            "append", "extend", "update", "insert", "remove", "delete",
            "split", "join", "strip", "lower", "upper", "replace",
            "encode", "decode", "startswith", "endswith",
            "request", "response", "method", "args", "kwargs",
            # 通用 dict key / 属性名 — 不同对象共用纯属巧合
            "name", "value", "price", "count", "total", "size", "length",
            "type", "status", "level", "code", "message", "msg",
            "data", "result", "error", "info", "warning", "debug",
            "created", "updated", "deleted", "modified", "timestamp",
            "id", "uuid", "key", "secret", "password", "username",
            "host", "port", "addr", "address", "ip", "domain",
            # 常见短变量名 — 跨行共用纯属巧合
            "path", "file", "data", "name", "key", "val", "obj", "res",
            "tmp", "new", "old", "src", "dst", "err", "out", "msg", "cfg",
            "ctx", "args", "kwargs", "item", "rows", "row", "conn", "cur",
            "fp", "fd", "buf", "tag", "ids", "env", "opts", "url", "uri",
            "ret", "result", "status", "count", "total",
            "token", "email", "user"}
    t_tokens = set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b", t))
    f_tokens = set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b", f))
    overlap = (t_tokens & f_tokens) - stop
    if overlap:
        # 至少有一个 shared token 长度 ≥ 5（排除短缩写）
        return any(len(tok) >= 5 for tok in overlap)

    return False
