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
from pathlib import Path
from typing import Any

from reviewagent.commands._common import BaseCommand, BaseCommandError, publish_overview
from reviewagent.config import config
from reviewagent.git.diff_lines import (
    format_line_map_for_prompt,
    parse_diff_line_map,
)
from reviewagent.llm import get_client
from reviewagent.logging_setup import logger
from reviewagent.review.policies.python import (
    HELP_TEXT_FOOTER as _HELP_TEXT_FOOTER,
)
from reviewagent.review.policies.python import (
    code_first_line_matches,
    collect_defined_names,
    detect_apply_risk,
    extract_identifiers,
    extract_remedied_symbols,
    find_line_by_existing_code,
    fix_indent,
    normalise_suggestion,
    safe_parse_defs,
    score_suggestion,
    test_feature_density,
)
from reviewagent.review.publishing import (
    PublishCallbacks,
    PublishContext,
    _handle_cohort_reoccurrence,  # noqa: F401  # backward-compat re-export
    _suggestion_content_fingerprint,  # noqa: F401  # backward-compat re-export
    _suggestion_fingerprint,  # noqa: F401  # backward-compat re-export
    make_token_summary,
)
from reviewagent.review.publishing import (
    extract_guardrail_markers as _extract_guardrail_markers_impl,
)
from reviewagent.review.publishing import (
    publish as _publish_impl,
)

# Backward-compat re-exports
ImproveError = BaseCommandError

# 豁免正则: 仅匹配 {prefix}-RULE-* 形式的规则 (如 SSD-RULE-XXX)
# 用于评分/严重度过滤的豁免判断 — 只有仓库自定义规则才豁免, R-OTHER 等通用规则不豁免.
# 前缀在**调用时**从 config.rule_key_prefix 解析（正则按前缀缓存在 review.policy），
# 不再 import-time 绑定单一前缀 — 测试 monkeypatch 本模块 config 时立即生效.

# Linux MAX_ARG_STRLEN: 单个 argv 参数的字节上限 (32 pages × 4KB)。
# qodercli 的 prompt 以位置参数传递, 编码后超此值即 OSError [Errno 7]
# Argument list too long (MR 1136 E2BIG 根因 — 与总 ARG_MAX 2MB 无关,
# 单参数就限 128KB)。full 策略的源码块预算以此为天花板。
_MAX_ARG_STRLEN = 131_072


class ImproveCommand(BaseCommand):
    COMMAND_NAME = "improve"
    DEFAULT_AGENT = "improve"

    # 每条 suggestion 末尾追加 /adopt /dismiss 帮助文本 (与 pr-agent 一致)
    # 末段提醒用户用 MR 评论 + 命令标记处理, 避免直接点「解决主题」或 ✓ 图标关闭 thread — 这两条都会被
    # GitLab 归类为「已关闭(未分类)」, /adopt /dismiss 检测不到, telemetry 缺失处理数据.
    # 正文由 Python 策略提供（suggestion body 构造在 review/policies/python.py）.
    HELP_TEXT_FOOTER = _HELP_TEXT_FOOTER
    # ---------- 并行分块调用 ----------
    def _lazy_deps(self):
        from reviewagent.review.dynamic import load_dynamic_rules
        from reviewagent.review.policies.registry import get as get_policy
        from reviewagent.scenario.profile import active_or_load
        from reviewagent.scenario.rules.resolver import assemble_base

        profile = getattr(self, "_profile", None) or active_or_load()
        # 动态规则加载即校验模板；配置错误在命令启动期 fail-fast（预期行为）。
        self._dynamic_rules = getattr(self, "_dynamic_rules", None) or load_dynamic_rules(profile)
        rule_set = getattr(self, "_rule_set", None) or assemble_base(profile)
        policy = getattr(self, "_policy", None) or get_policy(
            profile.language, profile=profile, cfg=globals()["config"], read_lines=self._read_file_lines,
        )
        self._profile, self._rule_set, self._policy = profile, rule_set, policy
        return profile, rule_set, policy

    def _call_agent(self, ws, *, overflow_files: list[str] | None = None) -> dict[str, Any]:
        """覆盖基类: 按文件分块 + 并行调 opencode + 合并结果（编排在 review/runner.py）."""
        from reviewagent.review.runner import Callbacks, RunContext, run

        profile, rule_set, policy = self._lazy_deps()
        ctx = RunContext(
            project_id=self.project_id, mr_iid=self.mr_iid, ws=ws,
            repo_context=getattr(self, "repo_context", "") or "",
            cfg=globals()["config"], profile=profile, rule_set=rule_set,
            dynamic_rules=self._dynamic_rules, policy=policy,
            callbacks=Callbacks(
                split_diff=self._split_diff_by_file,
                diff_line_map=self._diff_line_map,
                read_lines=self._read_file_lines,
                read_line_count=self._read_file_line_count,
                stat_bytes=self._stat_file_bytes,
                blob_sha=self._file_blob_sha,
                head_sha=self._get_mr_head_sha,
                new_files=self._new_files_from_diff,
                collect_cross_refs=self._collect_cross_file_refs_for_mr,
                build_chunk_prompt=self._build_chunk_prompt,
                build_batch_prompt=self._build_batch_prompt,
                call_chunk=self._call_chunk,
                build_user_prompt=self._build_user_prompt,
                super_call_agent=lambda ws_: super(ImproveCommand, self)._call_agent(ws_),
                set_token_summary=lambda p, c, m, cost: setattr(
                    self, "_last_oc_result", type(self)._make_token_summary(p, c, m, cost),
                ),
                budget_fn=self._full_source_budget_bytes,
            ),
            overflow_files=overflow_files,
        )
        result = run(ctx)
        self._review_mode = ctx.review_mode
        return result

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
        """从 diff 的 `+` 行提取改动的标识符 (def/class/常量/import 名字).

        实现见 review/policies/python.py:extract_identifiers（门面薄委托）.
        """
        return extract_identifiers(diff)

    def _dynamic_rule_params(self) -> dict[str, Any]:
        """当前启用 `cross_file_refs` 规则的参数（缺省回退 legacy 常量）."""
        from reviewagent.review.dynamic.providers.cross_file_refs import DEFAULT_PARAMS

        params = dict(DEFAULT_PARAMS)
        ruleset = getattr(self, "_dynamic_rules", None)
        if ruleset is not None:
            for rule in ruleset.rules:
                if rule.enabled and rule.provider == "cross_file_refs":
                    params.update(rule.params or {})
                    break
        return params

    def _find_cross_file_refs(
        self, file_path: str, diff: str, worktree_path, max_refs: int | None = None,
    ) -> list[dict]:
        """在 worktree 找 diff 改动的标识符在其他文件的 caller 引用.

        实现见 review/dynamic/providers/cross_file_refs.py:find_refs（门面薄委托）；
        params 从当前动态规则集解析（缺省回退 legacy 6）——显式传入的
        `max_refs` 优先，否则不覆盖规则参数。
        """
        from reviewagent.review.dynamic.providers.cross_file_refs import find_refs

        params = self._dynamic_rule_params()
        if max_refs is not None:
            params["max_refs_per_file"] = max_refs
        return find_refs(file_path, diff, worktree_path, params=params)

    def _collect_cross_file_refs_for_mr(
        self,
        files: list[str],
        diff_by_file: dict[str, str],
        worktree_path,
        max_refs_per_file: int | None = None,
    ) -> dict[str, list[dict]]:
        """对整个 MR 一次性 rg 找所有 diff 文件的标识符在仓库的 caller 引用.

        Returns: {file_path: [{file, line, content, ident}, ...]}
        实现见 review/dynamic/providers/cross_file_refs.py:collect_refs（门面薄委托）；
        params 从当前动态规则集解析（缺省回退 legacy 6）——显式传入的
        `max_refs_per_file` 优先，否则不覆盖规则参数。
        """
        from reviewagent.review.dynamic.providers.cross_file_refs import collect_refs

        params = self._dynamic_rule_params()
        if max_refs_per_file is not None:
            params["max_refs_per_file"] = max_refs_per_file
        return collect_refs(files, diff_by_file, worktree_path, params=params)

    def _render_cross_file_section(self, refs: list[dict]) -> str:
        """把 cross-file 引用渲染成 chunk prompt 段.

        文案在 ``prompts/dynamic/cross_file_refs_*.md``，实现见
        review/context.py:render_cross_file_section（门面薄委托）.
        模板路径优先取当前动态规则集里首个启用的声明，缺省用 legacy 模板。
        """
        from reviewagent.review.context import render_cross_file_section
        from reviewagent.scenario.profile import active_or_load

        empty_template: str | None = None
        items_template: str | None = None
        ruleset = getattr(self, "_dynamic_rules", None)
        if ruleset is not None:
            for rule in ruleset.rules:
                if not rule.enabled:
                    continue
                if empty_template is None and rule.empty_template:
                    empty_template = rule.empty_template
                if items_template is None and rule.items_template:
                    items_template = rule.items_template
                if empty_template and items_template:
                    break

        return render_cross_file_section(
            refs,
            profile=active_or_load(),
            empty_template=empty_template,
            items_template=items_template,
        )

    def _build_chunk_prompt(
        self, file_path: str, file_diff: str, valid_lines: set[int], ws,
        cross_file_refs: list[dict] | None = None,
        *, strategy: str = "full",
        prior_issues: list[dict] | None = None,
        cross_file_enabled: bool = True,
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

        from reviewagent.scenario.profile import active_or_load
        from reviewagent.security import hardening_prefix, normalize_untrusted, security_flag

        profile = active_or_load()
        if security_flag(profile, "normalize_untrusted", default=True):
            # diff 属不可信数据：控制字符会让行号/围栏解析错位，先清理再裁剪。
            file_diff = normalize_untrusted(file_diff)

        # V10: 大 diff 截断 (clip) — 防单文件巨型 diff 撑爆 prompt (截断处有显式标记行)
        _clip_n = config.improve_clip_diff_lines
        if _clip_n > 0:
            file_diff, _was_clipped = self._clip_diff(file_diff, _clip_n)
            if _was_clipped:
                logger.info(
                    "improve.diff_clipped project={} mr={} file={} max_lines={}",
                    self.project_id, self.mr_iid, file_path, _clip_n,
                )

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
        # 规则文案在 prompts/rules_repo.md / rules_general.md（assembly 装配）.
        from reviewagent.review.assembly import (
            ChunkPromptParts,
            build_chunk_prompt,
            build_rules_block,
        )

        rules_block = build_rules_block(
            self.repo_context, self._general_rules_text(), profile=profile
        )

        # 跨文件 caller 引用: 优先用 _call_agent 预计算的全局结果, fallback 到 per-file 调用
        if cross_file_enabled:
            if cross_file_refs is None:
                cross_file_refs = self._find_cross_file_refs(file_path, file_diff, wt)
            cross_file_section = self._render_cross_file_section(cross_file_refs)
        else:
            # 动态规则禁用：整段不存在（连空模板都不渲染）。
            cross_file_section = ""

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

        return hardening_prefix(profile) + build_chunk_prompt(
            ChunkPromptParts(
                cross_file_section=cross_file_section,
                rules_block=rules_block,
                prior_section=prior_section,
                file_path=file_path,
                file_diff=file_diff,
                source_block=source_block,
                valid_lines=vl_str,
            ),
            profile=profile,
        )

    @staticmethod
    def _merge_chunks(results: list[dict[str, Any]], *, skipped_files: list[str] | None = None, file_priorities: dict[str, float] | None = None, new_files: set[str] | None = None, cap_truncated: list[str] | None = None) -> dict[str, Any]:
        """合并多个 chunk 的结果（实现见 review/validation.py:merge_chunks，门面薄委托）."""
        from reviewagent.review.validation import merge_chunks

        return merge_chunks(
            results, cfg=config, score_fn=ImproveCommand._score_suggestion,
            skipped_files=skipped_files, file_priorities=file_priorities,
            new_files=new_files, cap_truncated=cap_truncated,
        )

    # ---------- helpers ----------
    @staticmethod
    def _make_token_summary(prompt_tokens: int, completion_tokens: int, model: str, cost_credits: float = 0.0):
        """创建汇总 token 统计的 LLMResult (仅用于 _last_oc_result 替代).

        实现见 review/publishing.py:make_token_summary（门面薄委托）.
        """
        return make_token_summary(prompt_tokens, completion_tokens, model, cost_credits)

    def _get_mr_head_sha(self) -> str | None:
        """获取当前 MR 的 head_sha (用于 record_suggestion 时记录发布 SHA)."""
        try:
            refs = self.gitlab.get_mr_diff_refs(self.project_id, self.mr_iid)
            return refs.get("head_sha") or refs.get("start_sha")
        except Exception as e:
            logger.warning("improve.get_mr_head_sha failed: {}", e)
            return None

    def _worktree_candidate(self, root: Path, file_path: str) -> Path | None:
        """把 worktree 相对路径解析为真实路径（worktree 约束，spec §7.6 第 2 条）.

        默认（``security.worktree_containment=true``）走
        ``review.paths.resolve_within``：绝对路径 / ``..`` 分量 / 符号链接逃逸
        记 ``security.path_rejected`` WARNING 并返回 None；文件缺失或解析失败
        （ELOOP 等 OSError）静默返回 None（调用侧沿用既有降级返回值）。

        关闭约束时保留旧语义（``root / file_path``，绝对路径按 pathlib 规则直取）。
        """
        from reviewagent.review.paths import resolve_within
        from reviewagent.security import security_flag

        if not security_flag(
            getattr(self, "_profile", None), "worktree_containment", default=True
        ):
            return Path(root) / file_path
        try:
            return resolve_within(Path(root), file_path)
        except ValueError:
            logger.warning(
                "security.path_rejected path={} reason={}", file_path, "outside_worktree"
            )
            return None
        except OSError:
            return None

    def _file_blob_sha(self, file_path: str, ws) -> str | None:
        """V9: 文件内容指纹 (sha1 of raw bytes at worktree head).

        相同 blob = 文件字节相同 = 上轮 LLM(无状态)的建议必然仍成立 → 可安全复用.
        不依赖 git 历史 (workspace 是 shallow fetch, 旧 head 可能已裁剪).
        """
        import hashlib
        try:
            p = self._worktree_candidate(Path(ws.worktree), file_path)
            if p is None or not p.is_file():
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

        实现见 review/planner.py:sample_case_files（门面薄委托）.
        """
        from reviewagent.review.planner import sample_case_files

        return sample_case_files(all_files, patterns, max_n)

    @staticmethod
    def _apply_file_count_cap(
        files_to_review: list[str], new_files: set[str], keyword_paths: tuple[str, ...],
        max_files: int,
    ) -> tuple[list[str], list[str]]:
        """文件数硬上限: 必保(新增+关键路径)外的低优先级 tail 截断.

        files_to_review 须已按优先级降序.
        Returns: (kept, truncated)

        实现见 review/planner.py:apply_file_count_cap（门面薄委托）.
        """
        from reviewagent.review.planner import apply_file_count_cap

        return apply_file_count_cap(files_to_review, new_files, keyword_paths, max_files)

    def _read_file_line_count(self, file_path: str, ws) -> int:
        """读 worktree 文件行数, 用于 priority 排序 + strategy 分配.
        失败 (含路径越界 / 文件缺失) 返回 -1 (排序时按最低优先级)."""
        if not ws:
            return -1
        path = self._worktree_candidate(Path(ws.worktree), file_path)
        if path is None:
            return -1
        try:
            if not path.is_file():
                return -1
            with path.open("r", encoding="utf-8", errors="replace") as f:
                return sum(1 for _ in f)
        except (OSError, UnicodeDecodeError):
            return -1

    # ---------- V10 批次合并 + 大 diff 截断 (2026-08-18) ----------

    @staticmethod
    def _clip_diff(file_diff: str, max_lines: int) -> tuple[str, bool]:
        """截断 diff 到 max_lines 行 (防单文件巨型 diff 撑爆 prompt).

        截断处加显式标记行, 让 LLM 知道后文已省略 (不幻觉行号).
        Returns: (clipped_diff, was_clipped)
        """
        if max_lines <= 0:
            return file_diff, False
        lines = file_diff.splitlines()
        if len(lines) <= max_lines:
            return file_diff, False
        kept = lines[:max_lines]
        kept.append(f"... (diff truncated: {len(lines) - max_lines} more lines omitted)")
        return "\n".join(kept) + "\n", True

    @staticmethod
    def _group_files_for_batching(
        files: list[str],
        diff_by_file: dict[str, str],
        strategy: dict[str, str],
        *,
        max_diff_lines: int,
        max_files: int,
        max_total_lines: int,
    ) -> list[list[str]]:
        """V10: 小 diff 文件合并批次分组.

        规则 (借鉴 pr-agent 多 chunk 打包 + FFD 装箱):
        - full 策略文件 → 独享 chunk (高优先级, 不降检视质量)
        - partial/patch 策略且 0 < diff 行数 <= max_diff_lines → 参与合并
          (按 diff 行数降序 FFD 装箱, 批次满 max_files 或累计 max_total_lines 即关)
        - diff 行数超限 / diff 缺失的 partial/patch 文件 → 独立 chunk

        全局 priority 序不动 (full 配额 / suggestion 预算依赖它);
        FFD 只在装箱内部排序, 让大 diff 先占批次空间.

        Returns: 文件分组列表 ([[f1], [f2,f3,f4], ...]) — 每组 = 1 次 LLM 调用
        """
        def _diff_lines(fp: str) -> int:
            return len(diff_by_file.get(fp, "").splitlines())

        singles: list[list[str]] = []
        mergeable: list[str] = []
        for fp in files:
            if strategy.get(fp) == "full":
                singles.append([fp])
                continue
            dl = _diff_lines(fp)
            if 0 < dl <= max_diff_lines:
                mergeable.append(fp)
            else:
                singles.append([fp])

        if len(mergeable) <= 1:
            return singles + [[fp] for fp in mergeable]

        # FFD: 大 diff 先装箱 (减少批次浪费)
        mergeable.sort(key=_diff_lines, reverse=True)
        batches: list[list[str]] = []
        cur: list[str] = []
        cur_lines = 0
        for fp in mergeable:
            dl = _diff_lines(fp)
            if cur and (len(cur) >= max_files or cur_lines + dl > max_total_lines):
                batches.append(cur)
                cur, cur_lines = [], 0
            cur.append(fp)
            cur_lines += dl
        if cur:
            batches.append(cur)

        return singles + batches

    def _build_batch_prompt(
        self,
        group: list[str],
        diff_by_file: dict[str, str],
        line_map: dict[str, set[int]],
        ws,
        cross_file_refs_by_file: dict[str, list[dict]],
        strategy: dict[str, str],
        prior_issues_by_file: dict[str, list[dict]],
        *,
        cross_file_enabled: bool = True,
    ) -> str:
        """V10: 多文件合并批次 prompt — rules 只发一次 + N 个文件 section.

        组内保留各自 strategy 的 source block (partial 仍 partial, patch 仍 patch);
        suggestions 必须带 file 字段, 解析侧会过滤 file 不在组内的建议 (防归属混淆).
        """
        from reviewagent.review.assembly import (
            BatchPromptParts,
            build_batch_prompt,
            build_rules_block,
        )
        from reviewagent.scenario.profile import active_or_load
        from reviewagent.security import hardening_prefix, normalize_untrusted, security_flag

        profile = active_or_load()
        normalize = security_flag(profile, "normalize_untrusted", default=True)
        rules_block = build_rules_block(
            self.repo_context, self._general_rules_text(), profile=profile
        )

        n = len(group)
        sections: list[str] = []
        for idx, fp in enumerate(group, 1):
            file_diff = diff_by_file.get(fp, "")
            if normalize:
                file_diff = normalize_untrusted(file_diff)
            valid_lines = line_map.get(fp, set())
            strat = strategy.get(fp, "patch")
            if strat == "full":
                source_block = self._build_full_source_block(fp)  # 理论不达 (full 不入批)
            elif strat == "partial":
                source_block = self._build_partial_context_block(fp, valid_lines, ws)
            else:
                source_block = self._build_patch_only_block(fp, valid_lines)

            vl_str = f"{fp}: {sorted(valid_lines)}"
            cross_section = (
                self._render_cross_file_section(cross_file_refs_by_file.get(fp, []))
                if cross_file_enabled else ""
            )

            prior_section = ""
            pis = prior_issues_by_file.get(fp)
            if pis:
                _anchors = []
                for i, pi in enumerate(pis, 1):
                    sev = (pi.get("severity") or "medium").lower()
                    hdr = (pi.get("header") or pi.get("rationale") or "").strip()
                    lbl = (pi.get("label") or "").strip()
                    _anchors.append(f"{i}. [{sev}] {hdr}" + (f" (label={lbl})" if lbl else ""))
                prior_section = (
                    "#### 已知问题锚点（上一轮 open；行号可能偏移）\n\n"
                    + "\n".join(_anchors) + "\n\n"
                )

            sections.append(
                f"### 文件 {idx}/{n}: `{fp}`\n\n"
                f"{cross_section}"
                f"{prior_section}"
                f"#### diff\n```diff\n{file_diff}\n```\n\n"
                f"{source_block}\n\n"
                f"#### VALID NEW LINES（start_line 只能从此取）\n\n{vl_str}\n"
            )

        file_list = "\n".join(f"- `{fp}`" for fp in group)
        return hardening_prefix(profile) + build_batch_prompt(
            BatchPromptParts(
                rules_block=rules_block,
                file_count=n,
                file_list=file_list,
                sections="\n\n---\n\n".join(sections),
            ),
            profile=profile,
        )

    def _test_feature_density(self, file_path: str, file_size: int) -> float:
        """V7 测试特征密度 (0~1): 绝对命中数 + 密度组合.

        高分 → 高风险测试文件 (含 fixture/mock/parametrize/assert/setup-teardown).
        用绝对数 + 密度组合, 避免大文件被稀释 (2000行100assert ≠ 50行10assert).

        实现见 review/policies/python.py:test_feature_density（门面薄委托）；
        关键字在调用时从 config 解析.
        """
        return test_feature_density(
            file_path,
            file_size,
            read_lines=self._read_file_lines,
            keywords=config.improve_test_feature_keywords,
        )

    @staticmethod
    def _rule_block_text() -> str:
        """当前激活规则块正文（full 源码预算的固定开销估算用）.

        由 T5 resolver 的 `verbatim_body` 提供（与旧的
        `load_block("_general_rules_block")` 逐字节一致）；取不到时返回 ``""``，
        按 0 计 — 预算是安全网, 不是硬依赖（与旧实现 ``except: pass`` 等价）.
        """
        try:
            from reviewagent.scenario.rules.resolver import active_rule_set

            return active_rule_set().verbatim_body
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _general_rules_text() -> str:
        """chunk / batch prompt 内联的通用规则正文（不做静默降级）.

        优先取 T5 resolver 的 `verbatim_body`（与旧
        `load_block("_general_rules_block")` 逐字节一致）；resolver 异常时回退
        旧 loader — 旧 loader 再失败就照常抛错（与重构前一致，不能静默输出
        空规则块）. 与 `_rule_block_text()` 的区别：后者为预算估算吞掉异常.
        """
        try:
            from reviewagent.scenario.rules.resolver import active_rule_set

            return active_rule_set().verbatim_body
        except Exception:  # noqa: BLE001
            from reviewagent.prompts.loader import load_block as _load_block

            return _load_block("_general_rules_block")

    def _prompt_policy(self):
        """当前 profile 的语言策略（prompt/source 装配用；调用时解析，不缓存）."""
        from reviewagent.review.policies.registry import get
        from reviewagent.scenario.profile import active_or_load

        profile = active_or_load()
        return get(
            profile.language,
            profile=profile,
            cfg=globals()["config"],
            read_lines=self._read_file_lines,
        )

    def _full_source_budget_bytes(self, diff_lines: int) -> int:
        """full 源码块允许的最大字节数 (含行号前缀) — E2BIG 防线.

        prompt 作为 qodercli 的单个位置参数传递, 受内核 MAX_ARG_STRLEN=128KB
        约束 (MR 1136: [Errno 7] Argument list too long)。full 策略把整个文件
        源码塞进 prompt, 是唯一可能撑爆该参数的变量, 分配策略阶段即按
        ``上限 − 固定开销(repo_context/规则块/杂项) − 本文件 diff 估算`` 预算拦截。

        config.improve_full_source_max_bytes: 0=自适应(默认), >0=固定值, <0=关闭。
        实现见 review/planner.py:full_source_budget_bytes（门面薄委托,
        规则块正文经 `_rule_block_text()` 惰性获取, 失败按 0 字节计）。
        """
        from reviewagent.review.planner import full_source_budget_bytes

        cfg = globals()["config"]
        rule_bytes = len(self._rule_block_text().encode())
        return full_source_budget_bytes(
            diff_lines, cfg=cfg, rule_bytes=rule_bytes,
            repo_context=getattr(self, "repo_context", "") or "",
        )

    def _stat_file_bytes(self, file_path: str) -> int:
        """从 worktree 读文件字节数 (E2BIG 预算用); 读不到返回 -1 (不拦截, 维持旧行为)."""
        ws = getattr(self, "ws", None)
        if not ws:
            return -1
        path = self._worktree_candidate(Path(ws.worktree), file_path)
        if path is None:
            return -1
        try:
            if path.is_file():
                return path.stat().st_size
        except OSError:
            pass
        return -1

    def _single_file_over_full_budget(
        self, files: list[str], file_meta: dict[str, tuple[int, int]],
    ) -> bool:
        """E2BIG 防线 (单文件早退路径): 唯一文件的 full 源码块超预算 → True.

        单文件 MR 走 ``_call_agent`` 顶部的基类捷径 (``super()._call_agent``),
        其 ``_build_user_prompt`` 无条件内嵌全部源码且不受 strategy 约束。
        超预算时调用方不早退, 落到 strategy 路径 (partial/patch, prompt 尺寸受控)
        — 与多文件 MR 走的是同一条已验证路径。

        判定见 review/planner.py:single_file_over_full_budget；降级日志留在本层
        （planner 侧无 project/mr 语境）。
        """
        from reviewagent.review.planner import single_file_over_full_budget

        over = single_file_over_full_budget(
            files, file_meta,
            stat_bytes=self._stat_file_bytes,
            budget_fn=self._full_source_budget_bytes,
        )
        if over:
            diff_lines, file_lines = file_meta.get(files[0], (0, 0))
            file_bytes = self._stat_file_bytes(files[0])
            logger.info(
                "improve.full_source_budget_downgrade project={} mr={} file={} "
                "file_bytes={} est_source_bytes={} budget={} -> strategy path (single-file)",
                self.project_id, self.mr_iid, files[0], file_bytes,
                file_bytes + file_lines * 7, self._full_source_budget_bytes(diff_lines),
            )
        return over

    def _assign_strategy(
        self, sorted_files: list[str], file_meta: dict[str, tuple[int, int]],
        overflow_set: set[str],
    ) -> dict[str, str]:
        """V7 strategy 分配 (full / partial / patch) — 与 priority 解耦.

        full    = 完整源码 (≤ full_files 配额, 优先高 priority 文件)
        partial = diff 行 ±N 行 context (中等文件 / 关键路径降级)
        patch   = diff + ±patch_context_lines 行最小 context (overflow / 大文件)

        实现见 review/planner.py:assign_strategy（门面薄委托, 晚绑定 config;
        stat/预算经实例方法透传, 保持 monkeypatch 有效）。
        """
        from reviewagent.review.planner import assign_strategy as _assign

        cfg = globals()["config"]
        return _assign(
            sorted_files, file_meta, overflow_set, cfg=cfg,
            stat_bytes=self._stat_file_bytes,
            budget_fn=self._full_source_budget_bytes,
            project_id=getattr(self, "project_id", None),
            mr_iid=getattr(self, "mr_iid", None),
        )

    def _build_full_source_block(self, file_path: str) -> str:
        """full 模式: 读 worktree 文件, 拼完整源码 (≤ 5000 行, 超出截断).

        实现见 review/context.py:build_source_block（门面薄委托）.
        """
        return self._build_source_block(file_path, set(), "full")

    def _build_partial_context_block(self, file_path: str, valid_lines: set[int], ws) -> str:
        """partial 模式 (V6): 仅取 diff 行 ±N 行 context, 多段合并.
        适用: 超出 IMPROVE_FULL_FILES 但仍需 LLM 检视的中等文件.
        性能: 比 full 模式省 token, 比 patch 模式多 ±N 行 context (跨行识别).

        实现见 review/context.py:build_source_block（门面薄委托）；
        ``ws`` 仅为兼容旧签名保留（行读取走 self._read_file_lines）.
        """
        return self._build_source_block(file_path, valid_lines, "partial")

    def _build_patch_only_block(self, file_path: str, valid_lines: set[int]) -> str:
        """patch-only 模式 (V7): diff 行 ±N 行最小 context.

        V6 纯行号基本检不出跨行问题; V7 加 ±patch_context_lines 行 source,
        让 LLM 能看到局部结构 (函数签名/缩进/上下文), 从"摆设"变"能用".
        适用: MAX_DIFF_CHARS overflow 文件 + 低优先级大文件.

        实现见 review/context.py:build_source_block（门面薄委托）.
        """
        return self._build_source_block(file_path, valid_lines, "patch")

    def _build_source_block(
        self, file_path: str, valid_lines: set[int], strategy: str,
    ) -> str:
        """source 块装配门面: 读行 -> FileContext -> context.build_source_block."""
        from reviewagent.review.context import build_source_block
        from reviewagent.review.types import FileContext

        lines = self._read_file_lines(file_path)
        ctx = FileContext(
            file_path=file_path,
            lines=tuple(lines),
            valid_lines=frozenset(valid_lines),
            start_line=1,
        )
        return build_source_block(
            ctx,
            strategy,
            cfg=globals()["config"],
            policy=self._prompt_policy(),
        )

    def _read_file_lines(self, file_path: str) -> list[str]:
        """从 worktree 读 file 源，按行 split；读不到（含路径越界）返回 []."""
        if not self.ws:
            return []
        path = self._worktree_candidate(Path(self.ws.worktree), file_path)
        if path is None:
            return []
        try:
            if path.is_file():
                return path.read_text(encoding="utf-8").splitlines(keepends=False)
        except OSError:
            return []
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

        实现见 review/policies/python.py:find_line_by_existing_code（门面薄委托）.
        """
        return find_line_by_existing_code(
            file_lines,
            existing_code,
            hint_line=hint_line,
            max_window=max_window,
        )

    def _build_user_prompt(self) -> str:
        """把 diff 的 valid new_line 集合 + 完整文件源码喂给 agent —
        严格约束它的 start_line 取值，让模型能精确数出文件行号。"""
        from reviewagent.scenario.profile import active_or_load
        from reviewagent.security import hardening_prefix, normalize_untrusted, security_flag

        profile = getattr(self, "_profile", None) or active_or_load()
        normalize = security_flag(profile, "normalize_untrusted", default=True)
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
            return hardening_prefix(profile) + (
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
            if normalize:
                lines = [normalize_untrusted(ln) for ln in lines]
            numbered = "\n".join(f"{i+1:4d}| {ln}" for i, ln in enumerate(lines))
            file_blocks.append(
                f"### 完整源码：`{fp}`（共 {len(lines)} 行；行号在左侧）\n```\n{numbered}\n```"
            )
        files_text = "\n\n".join(file_blocks) if file_blocks else "(no files)"

        return hardening_prefix(profile) + (
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

        实现见 review/publishing.py:build_summary_placeholder（门面薄委托）.
        """
        from reviewagent.review.publishing import build_summary_placeholder

        return build_summary_placeholder(
            project_id=self.project_id, mr_iid=self.mr_iid,
        )

    @staticmethod
    def _collect_defined_names(
        tree: Any,
        ast_module: Any,
    ) -> set[str]:
        """从 AST 中收集所有「定义」的符号名.

        实现见 review/policies/python.py:collect_defined_names（门面薄委托）.
        """
        return collect_defined_names(tree, ast_module)

    def _safe_parse_defs(
        self,
        src: str,
        _ast_module,
    ) -> tuple[set[str], SyntaxError | None]:
        """解析 src 收集 defs, 带 partial fallback.

        实现见 review/policies/python.py:safe_parse_defs（门面薄委托）.
        """
        return safe_parse_defs(src, _ast_module)

    def _detect_apply_risk(
        self,
        *,
        file_path: str,
        improved_code: str,
        file_sources: dict[str, list[str]],
        suggestion_text: str = "",
    ) -> tuple[str, list[str]]:
        """识别 improved_code 中引用了 file_sources 未定义的符号.

        实现见 review/policies/python.py:detect_apply_risk（门面薄委托）.
        """
        return detect_apply_risk(
            file_path=file_path,
            improved_code=improved_code,
            file_sources=file_sources,
            suggestion_text=suggestion_text,
        )

    @staticmethod
    def _extract_remedied_symbols(text: str) -> set[str]:
        """从建议正文里提取"已声明补救"的符号.

        实现见 review/policies/python.py:extract_remedied_symbols（门面薄委托）.
        """
        return extract_remedied_symbols(text)

    def _build_summary_v2(
        self,
        inline_posted: list[dict[str, Any]],
        inline_skipped: list[dict[str, Any]],
        total_agent_suggestions: int,
    ) -> str:
        """生成 V{N} 格式的顶层 summary, 只展示本次循环内新发布的 suggestions.

        实现见 review/publishing.py:build_summary_v2（门面薄委托）；输出逐字节一致.
        """
        from reviewagent.review.publishing import build_summary_v2

        return build_summary_v2(
            project_id=self.project_id,
            mr_iid=self.mr_iid,
            inline_posted=inline_posted,
            inline_skipped=inline_skipped,
            total_agent_suggestions=total_agent_suggestions,
        )

    def _pre_reconcile_silent(self, **kwargs: Any) -> dict[str, Any]:
        """发布前预对账（惰性导入, 保持 monkeypatch 语义）."""
        from reviewagent.commands.suggestion_actions import (
            _scan_and_mark_resolved_silent,
        )

        return _scan_and_mark_resolved_silent(**kwargs)

    def _publish(self, agent_result: dict[str, Any]) -> dict[str, Any]:
        """把 agent_result 应用到 GitLab + 落 telemetry，返回执行摘要.

        实现见 review/publishing.py:publish（门面薄委托，回调注入）.
        """
        _profile, rule_set, _policy = self._lazy_deps()
        ctx = PublishContext(
            project_id=self.project_id, mr_iid=self.mr_iid, gitlab=self.gitlab,
        )
        callbacks = PublishCallbacks(
            diff_line_map=self._diff_line_map,
            head_sha=self._get_mr_head_sha,
            normalise=self._normalise_suggestion,
            validate=self._validate_suggestion,
            detect_apply_risk=self._detect_apply_risk,
            build_summary_placeholder=self._build_summary_placeholder,
            build_summary_v2=self._build_summary_v2,
            extract_guardrail_markers=self._extract_guardrail_markers,
            pre_reconcile=self._pre_reconcile_silent,
            publish_overview=publish_overview,
            help_footer=self.HELP_TEXT_FOOTER,
        )
        return _publish_impl(
            ctx, agent_result, rule_set=rule_set, profile=_profile,
            cfg=globals()["config"], callbacks=callbacks,
        )

    @staticmethod
    def _extract_guardrail_markers(summary_md: str) -> list[str]:
        """从 _merge_chunks 生成的 summary_md 中抽取文件筛选护栏的提示行.

        V1.1 配套: _publish 重建 summary 时, 把这些行追加到尾部,
        让 GitLab note 显示 C 抽样跳过 / D 文件数截断的提示.

        实现见 review/publishing.py:extract_guardrail_markers（门面薄委托）.
        """
        return _extract_guardrail_markers_impl(summary_md)

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

        校验顺序见 review/validation.py:validate_fields（门面注入 find_line /
        read_lines / fix_indent / first_line_matches，实现只此一份）.
        `file_sources` 惰性填充语义保持不变：缺文件时才经 `_read_file_lines` 读取.
        """
        from reviewagent.review.validation import validate_fields

        return validate_fields(
            file_path=file_path,
            start_line=start_line,
            improved_code=improved_code,
            existing_code=existing_code,
            line_map=line_map,
            file_sources=file_sources,
            find_line=self._find_line_by_existing_code,
            read_lines=self._read_file_lines,
            fix_indent=self._fix_indent,
            first_line_matches=_code_first_line_matches,
            project_id=self.project_id,
            mr_iid=self.mr_iid,
        )

    @staticmethod
    def _score_suggestion(s: dict) -> int:
        """对单条建议评分 (0~100), 用于过滤低质量建议.

        实现见 review/policies/python.py:score_suggestion（门面薄委托）；
        规则前缀在调用时从 config 解析.
        """
        return score_suggestion(s, rule_prefix=config.rule_key_prefix)

    @staticmethod
    def _fix_indent(target_line: str, improved_code: str) -> str:
        """确保 improved_code 第一行的缩进 == target_line 的缩进.

        实现见 review/policies/python.py:fix_indent（门面薄委托）.
        """
        return fix_indent(target_line, improved_code)

    @staticmethod
    def _normalise_suggestion(s: dict[str, Any]) -> dict[str, Any]:
        """校验 + 构造 GitLab "Apply suggestion" inline comment body.

        实现见 review/policies/python.py:normalise_suggestion（门面薄委托）.
        """
        return normalise_suggestion(s)


def _code_first_line_matches(target_line: str, improved_first: str) -> bool:
    """启发式: target / improved 第一行是否同一行（容忍空格/标点差异）.

    实现见 review/policies/python.py:code_first_line_matches（门面薄委托）.
    """
    return code_first_line_matches(target_line, improved_first)
