"""文件规划阶段（纯函数、语言无关）.

`plan()` 把 ``ImproveCommand._call_agent`` 里的文件规划链路按原顺序抽出：

    扩展名过滤 → review_mode 判定 → 测试路径过滤 → 确定性抽样 →
    文件元数据/优先级 → strategy 分配 → 单文件捷径判定 →
    增量复用（blob + 指纹失效 + 跨文件影响）→ 文件数上限 → 批次合并

设计约束（见 spec §7 / 计划 Task 8）：

* **纯函数**：不做 DB / LLM / git 访问。``blob_sha`` / ``read_line_count`` /
  ``stat_bytes`` 由调用方注入（Task 10 的 runner 负责 store 与 worktree 读取），
  ``prior_states`` 是不可变输入，planner 只读不写；
* **语言无关**：测试特征密度与标识符抽取只经
  ``LanguagePolicy.test_feature_density`` / ``LanguagePolicy.extract_symbols``
  两个钩子表达，本模块不含任何语言字面量，也不 import ``review.policies.*``；
* **配置只从入参 ``cfg`` 读**（门面读模块级 ``config`` 后传入），不读测试旋钮、
  不读 ``os.environ``；
* **日志**：迁移 ``_call_agent`` 规划期 info 日志（消息键与字段顺序保持不变），
  其中 ``improve.full_source_budget_downgrade`` 由 :func:`assign_strategy`
  自身发出（它持有 project/mr）；单文件捷径的那一条留在门面包装层。
  指标计数（``reviewagent_improve_*``）、store 读写与 LLM 调用仍属 runner（Task 10）。

行为基线：默认 profile（``ssd-python-autotest``）下，本模块 + 门面委托的
判定结果与 HEAD ``0c5b490`` 的 ``ImproveCommand._call_agent`` 逐字节一致
（由 planner 与 improve 策略两组回归测试双向覆盖）。
"""
from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from reviewagent.git.diff_lines import parse_diff_line_map
from reviewagent.logging_setup import logger
from reviewagent.review.types import FileKind

# Linux MAX_ARG_STRLEN: 单个 argv 参数的字节上限 (32 pages × 4KB)。
# qodercli 的 prompt 以位置参数传递, 编码后超此值即 OSError [Errno 7]。
# 与 ImproveCommand._MAX_ARG_STRLEN 同值（门面保留该常量供旧测试导入）。
_MAX_ARG_STRLEN = 131_072

# Plan.skipped 的分类键：测试/配置路径跳过 与 文件数上限截断（旧代码分开传给
# _merge_chunks，合并成一条会改变 summary 文案）。
SKIP_TEST = "test"
SKIP_CAP = "cap"

# 单文件 full 源码块预算的基准值（与旧实现逐字一致）
_BUDGET_BASE = _MAX_ARG_STRLEN
_BUDGET_FIXED_OVERHEAD = 8_192
_BUDGET_SAFETY = 0.92
_BUDGET_FLOOR = 16_384


@dataclass(frozen=True)
class Plan:
    """一次文件规划的完整结果（runner 只读）.

    Attributes:
        files: 规划后的全部文件（已过滤，按优先级降序）——旧 ``files``。
        files_to_review: 需送 LLM 检视的文件（已排除复用、已做文件数上限），
            旧 ``files_to_review``；全复用或全部被截断时可能为空。
        reused: 上轮结果可直接复用的文件（旧 ``files_reuse``）。
        strategies: ``{file: full|partial|patch}``（覆盖 ``files`` 全部文件）。
        batches: 批次分组（每组 = 1 次 LLM 调用）；由 :func:`group_files_for_batching`
            或逐文件单组生成。
        skipped: ``{"test": (...), "cap": (...)}`` — 测试/配置路径跳过 +
            文件数上限截断（旧 ``test_skipped`` / ``cap_truncated``）。
        prior_by_file: ``{file: prior_state}`` — ``prior_states`` 在本次规划文件上的
            只读快照（runner 用它取 head_sha 等，不做 DB 访问）。
        file_priorities: ``{file: score}``（旧 ``_file_priorities``），覆盖 ``files``。
        new_files: 本次规划文件中的新增文件（旧 ``new_files`` 交集）。
        cross_reuse: 因跨文件影响被强制重检的原复用候选（旧 ``files_to_review``
            里那部分，仅用于日志/回归断言）。
        single_file: 旧 ``if len(files) <= 1 and not skipped_files`` 判定；
            调用方仍需叠加 full 源码预算检查后决定是否走基类捷径。
        single_file_over_budget: ``single_file`` 且唯一文件的 full 源码块
            超预算（E2BIG 防线）；非单文件时恒为 False。
        overflow: 本次规划文件中的 overflow 集合（强制 patch）。
        review_mode: 本次生效的检视模式（``business`` / ``test``）。
    """

    files: tuple[str, ...] = ()
    files_to_review: tuple[str, ...] = ()
    reused: tuple[str, ...] = ()
    strategies: dict[str, str] = field(default_factory=dict)
    batches: tuple[tuple[str, ...], ...] = ()
    skipped: dict[str, tuple[str, ...]] = field(default_factory=dict)
    prior_by_file: dict[str, dict[str, Any]] = field(default_factory=dict)
    file_priorities: dict[str, float] = field(default_factory=dict)
    new_files: frozenset[str] = frozenset()
    cross_reuse: tuple[str, ...] = ()
    single_file: bool = False
    single_file_over_budget: bool = False
    overflow: frozenset[str] = frozenset()
    review_mode: str = "business"

    @property
    def test_skipped(self) -> tuple[str, ...]:
        """测试/配置路径被跳过的文件（旧 ``test_skipped``）."""
        return tuple(self.skipped.get(SKIP_TEST, ()))

    @property
    def cap_truncated(self) -> tuple[str, ...]:
        """文件数上限截断的低优先级文件（旧 ``cap_truncated``）."""
        return tuple(self.skipped.get(SKIP_CAP, ()))

    @property
    def all_skipped(self) -> tuple[str, ...]:
        """两类跳过合并（顺序：测试跳过在前，上限截断在后）."""
        return self.test_skipped + self.cap_truncated


# ---------------------------------------------------------------------------
# 纯 helper（与旧 ImproveCommand 静态方法逐字同源）
# ---------------------------------------------------------------------------


def _matches_any_path(file_path: str, patterns: Sequence[str]) -> bool:
    """路径命中 patterns：子串命中，或文件名以 pattern（去掉尾 ``/``）开头.

    与旧实现 ``pat in fp or Path(fp).name.startswith(pat.rstrip("/"))`` 等价。
    """
    name = Path(file_path).name
    return any(pat in file_path or name.startswith(pat.rstrip("/")) for pat in patterns)


def _is_test_file(file_path: str, policy: Any, skip_test_patterns: Sequence[str]) -> bool:
    """review_mode auto：优先用策略的 `classify_file`（读 profile.test_paths），
    策略未实现时退回 `improve_skip_test_paths` 的路径匹配."""
    classify = getattr(policy, "classify_file", None)
    if callable(classify):
        try:
            return classify(file_path) in (FileKind.TEST, FileKind.FIXTURE)
        except Exception:  # noqa: BLE001 — 策略异常不得中断规划
            pass
    return _matches_any_path(file_path, skip_test_patterns)


def sample_case_files(
    all_files: list[str], patterns: tuple[str, ...], max_n: int,
) -> tuple[list[str], list[str]]:
    """确定性抽样: 命中 patterns 的文件超过 max_n 时, 按路径 md5 排序取前 max_n.

    Returns: (kept_all_files, unsampled_list)
        - kept_all_files: 抽样后剩余的 all_files (顺序不变, 仅移除未抽中者)
        - unsampled_list: 被抽样剔除的文件 (已排序)
    """
    case_files = [
        fp for fp in all_files
        if _matches_any_path(fp, patterns)
    ]
    if len(case_files) <= max_n:
        return list(all_files), []
    sampled = sorted(case_files, key=lambda p: hashlib.md5(p.encode()).hexdigest())[:max_n]
    sampled_set = set(sampled)
    unsampled = [fp for fp in case_files if fp not in sampled_set]
    kept = [fp for fp in all_files if fp not in set(unsampled)]
    return kept, sorted(unsampled)


def apply_file_count_cap(
    files_to_review: list[str], new_files: Collection[str], keyword_paths: Sequence[str],
    max_files: int,
) -> tuple[list[str], list[str]]:
    """文件数硬上限：任何情况下 kept 都不超过 max_files.

    必保集合（新增 + 关键路径）只保证"优先保留"，不保证越过上限：必保本身
    超过 max_files 时按既有优先级顺序截断尾部，剩余全部进 truncated。

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
    if len(must) >= max_files:
        return must[:max_files], must[max_files:] + optional
    budget = max_files - len(must)
    kept_optional = optional[:budget]
    truncated = optional[budget:]
    return must + kept_optional, truncated


def full_source_budget_bytes(
    diff_lines: int, *, cfg: Any, rule_bytes: int, repo_context: str = "",
) -> int:
    """full 源码块允许的最大字节数 (含行号前缀) — E2BIG 防线.

    prompt 作为 CLI 的单个位置参数传递, 受内核 MAX_ARG_STRLEN=128KB 约束
    (MR 1136: [Errno 7] Argument list too long)。full 策略把整个文件源码塞进
    prompt, 是唯一可能撑爆该参数的变量, 分配策略阶段即按
    ``上限 − 固定开销(规则块/repo_context/杂项) − 本文件 diff 估算`` 预算拦截。

    Args:
        diff_lines: 本文件 diff 行数（用于估算 diff 在 prompt 中的字节开销）.
        cfg: 生效配置（``improve_full_source_max_bytes`` / ``improve_clip_diff_lines``）.
        rule_bytes: 规则块正文字节数（RuleSet ``verbatim_body``，
            取不到时按 0 计）.
        repo_context: 仓库自定义规则文本（旧 ``self.repo_context``）.

    ``cfg.improve_full_source_max_bytes``: 0=自适应(默认), >0=固定值, <0=关闭。
    """
    if cfg.improve_full_source_max_bytes > 0:
        return cfg.improve_full_source_max_bytes
    if cfg.improve_full_source_max_bytes < 0:
        return _MAX_ARG_STRLEN  # 关闭防线 (恢复旧行为)
    fixed = _BUDGET_FIXED_OVERHEAD  # 段标题 / VALID NEW LINES / 已发建议列表 / 输出指令等杂项
    fixed += rule_bytes
    fixed += len((repo_context or "").encode())
    # diff 行均宽估算 (含上下文行/前缀); clip=0 (不截断) 时按实际行数
    clip = cfg.improve_clip_diff_lines
    est_diff = (diff_lines if clip <= 0 else min(max(diff_lines, 0), clip)) * 64
    # 目标上限留 8% 安全余量 (估算是粗的)
    return max(_BUDGET_FLOOR, int(_BUDGET_BASE * _BUDGET_SAFETY) - fixed - est_diff)


def single_file_over_full_budget(
    files: list[str],
    file_meta: dict[str, tuple[int, int]],
    *,
    stat_bytes: Callable[[str], int],
    budget_fn: Callable[[int], int],
) -> bool:
    """E2BIG 防线 (单文件早退路径): 唯一文件的 full 源码块超预算 → True.

    单文件 MR 走 ``_call_agent`` 顶部的基类捷径 (``super()._call_agent``),
    其 ``_build_user_prompt`` 无条件内嵌全部源码且不受 strategy 约束。
    超预算时调用方不早退, 落到 strategy 路径 (partial/patch, prompt 尺寸受控)
    — 与多文件 MR 走的是同一条已验证路径。

    本函数**不打日志**（降级日志由门面包装层发出，它持有 project/mr 字段）。
    """
    if len(files) != 1:
        return False
    fp = files[0]
    diff_lines, file_lines = file_meta.get(fp, (0, 0))
    file_bytes = stat_bytes(fp)
    if file_bytes < 0:
        return False  # 读不到文件大小 → 不拦截, 维持旧行为
    budget = budget_fn(diff_lines)
    return file_bytes + file_lines * 7 > budget


def assign_strategy(
    sorted_files: list[str],
    file_meta: dict[str, tuple[int, int]],
    overflow_set: Collection[str],
    *,
    cfg: Any,
    stat_bytes: Callable[[str], int],
    budget_fn: Callable[[int], int],
    project_id: Any = None,
    mr_iid: Any = None,
) -> dict[str, str]:
    """V7 strategy 分配 (full / partial / patch) — 与 priority 解耦.

    full    = 完整源码 (≤ full_files 配额, 优先高 priority 文件)
    partial = diff 行 ±N 行 context (中等文件 / 关键路径降级)
    patch   = diff + ±patch_context_lines 行最小 context (overflow / 大文件)

    ``project_id`` / ``mr_iid`` 只用于 ``improve.full_source_budget_downgrade``
    日志字段（默认 None，纯测试可省略）。
    """
    strategy: dict[str, str] = {}
    full_quota = cfg.improve_full_files
    for fp in sorted_files:
        if fp in overflow_set:
            strategy[fp] = "patch"  # MAX_DIFF_CHARS overflow → 强制 patch
            continue
        if full_quota > 0 and sum(1 for s in strategy.values() if s == "full") < full_quota:
            # E2BIG 防线: full 源码块 (文件字节 + 行号前缀) 须在预算内,
            # 超预算不给 full (落到下方 partial/patch, 不占 full 配额)
            diff_lines, file_lines = file_meta[fp]
            file_bytes = stat_bytes(fp)
            budget = budget_fn(diff_lines)
            if file_bytes < 0 or file_bytes + file_lines * 7 <= budget:
                strategy[fp] = "full"
                continue
            logger.info(
                "improve.full_source_budget_downgrade project={} mr={} file={} "
                "file_bytes={} est_source_bytes={} budget={} -> partial/patch",
                project_id, mr_iid, fp, file_bytes,
                file_bytes + file_lines * 7, budget,
            )
        file_size = file_meta[fp][1]
        if file_size > 0 and file_size < 2000:
            strategy[fp] = "partial"
        elif any(kw.lstrip("/") in fp or kw in fp.lstrip("/") for kw in cfg.improve_keyword_paths):
            strategy[fp] = "partial"
        else:
            strategy[fp] = "patch"
    return strategy


def group_files_for_batching(
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


def priority_score(
    file_path: str,
    *,
    diff_size: int,
    file_size: int,
    cfg: Any,
    test_density: float = 0.0,
    is_new: bool = False,
    review_mode: str = "test",
) -> float:
    """V7 文件优先级打分（与旧 ``_priority`` 逐字同源）.

    信号：改动大小（log1p 不封顶）+ 关键路径 + 改动密度 + 测试特征密度 + 新增文件。
    `review_mode="business"` 时测试特征密度不参与打分（避免业务仓库被测试权重误导）。
    """
    # 改动大小: log1p 单调递增不封顶, 大改动永远比小改动高分
    score = math.log1p(diff_size) * cfg.improve_priority_weight_diff
    # 关键路径加分 (降到 25, 不再碾压 diff_size)
    fp_stripped = file_path.lstrip("/")
    if any(kw.lstrip("/") in file_path or kw in fp_stripped for kw in cfg.improve_keyword_paths):
        score += cfg.improve_priority_weight_keyword
    # 改动密度: 改动占比 > 30% → 高风险 (整文件重写级别)
    if file_size > 0 and (diff_size / file_size) > 0.3:
        score += cfg.improve_priority_weight_density
    # 测试特征密度: 只在 test 模式计权 (business 模式即使配置了权重也不生效)
    if review_mode == "test" and cfg.improve_priority_weight_test_feature > 0:
        score += test_density * cfg.improve_priority_weight_test_feature
    # A: 新增文件优先级加分 (自然吃满 IMPROVE_FULL_FILES 配额 → full 源码)
    if cfg.improve_new_file_full and is_new:
        score += cfg.improve_priority_weight_keyword
    return score


# ---------------------------------------------------------------------------
# 指纹失效判定
# ---------------------------------------------------------------------------


def _invalidated_reason(
    prior_state: Mapping[str, Any],
    *,
    profile_fingerprint: str | None,
    ruleset_fingerprint: str | None,
) -> str | None:
    """返回 ``profile`` / ``ruleset`` / None — 已存指纹与入参不一致的原因.

    入参为 None（Task 15 之前）或上轮未存该字段 → 视为一致（只按 blob 复用）。
    profile 优先于 ruleset：两者都变时只报 ``profile``（一次日志）。
    """
    if profile_fingerprint is not None:
        prior_profile = prior_state.get("profile_fingerprint")
        if prior_profile is not None and prior_profile != profile_fingerprint:
            return "profile"
    if ruleset_fingerprint is not None:
        prior_ruleset = prior_state.get("ruleset_fingerprint")
        if prior_ruleset is not None and prior_ruleset != ruleset_fingerprint:
            return "ruleset"
    return None


# ---------------------------------------------------------------------------
# 规划主入口
# ---------------------------------------------------------------------------


def plan(
    files: Sequence[str],
    diff_by_file: Mapping[str, str],
    *,
    profile: Any,
    cfg: Any,
    policy: Any,
    read_line_count: Callable[[str], int],
    stat_bytes: Callable[[str], int],
    prior_states: Mapping[str, Mapping[str, Any]] | None = None,
    head_sha: str | None = "",
    blob_sha: Callable[[str], str | None] | None = None,
    rule_block_bytes: int,
    profile_fingerprint: str | None = None,
    ruleset_fingerprint: str | None = None,
    new_files: Collection[str] | None = None,
    overflow_set: Collection[str] | None = None,
    line_map: Mapping[str, Collection[int]] | None = None,
    budget_fn: Callable[[int], int] | None = None,
    repo_context: str = "",
    project_id: Any = None,
    mr_iid: Any = None,
) -> Plan:
    """按旧 ``_call_agent`` 顺序做一次文件规划（纯函数）.

    Args:
        files: diff 涉及的全部文件（内部去重 + 升序，等价于旧 ``sorted(line_map)``）.
        diff_by_file: ``{file: 该文件 unified diff 文本}``.
        profile: 生效场景包（规划只用 ``cfg`` 里的生效值，其余由后续任务消费）.
        cfg: 生效配置（``reviewagent.config.Config`` 快照或代理）.
        policy: ``LanguagePolicy`` 实现（只用 ``test_feature_density`` /
            ``extract_symbols`` 两个钩子）.
        read_line_count: ``file -> worktree 行数``（读不到返回 -1）.
        stat_bytes: ``file -> worktree 字节数``（读不到返回 -1）.
        prior_states: 上轮每文件状态快照（``get_file_review_states`` 形态），
            **不可变输入**，planner 不做 DB 访问.
        head_sha: MR 当前 head；空/None 时全量重检（不复用）.
        blob_sha: ``file -> 当前 blob sha``；None → 一律视为未知（不做复用）.
        rule_block_bytes: 规则块正文字节数（full 源码预算用；**必填**——
            省略默认值会让预算比旧实现宽出规则块大小，可能翻转 full↔partial）.
        profile_fingerprint / ruleset_fingerprint: 指纹失效判定入参；
            None（Task 15 之前）保持只按 blob 复用.
        new_files: 新增文件集合（priority 加分 / 文件数上限必保）.
        overflow_set: MAX_DIFF_CHARS 截断文件集合（强制 patch）.
        line_map: ``{file: 有效新增行号集合}``；None → 由 ``diff_by_file`` 解析.
        budget_fn: full 源码预算函数（默认按 ``cfg`` / ``rule_block_bytes`` /
            ``repo_context`` 计算；runner 可注入以复用门面预算）.
        repo_context: 仓库自定义规则文本（预算估算用）.
        project_id / mr_iid: 只用于日志字段（默认 None，纯测试可省略）.
    """
    prior: Mapping[str, Mapping[str, Any]] = prior_states or {}
    news = frozenset(new_files or ())
    overflow = frozenset(overflow_set or ())
    get_blob: Callable[[str], str | None] = blob_sha or (lambda _fp: None)
    get_budget: Callable[[int], int] = budget_fn or (
        lambda n: full_source_budget_bytes(
            n, cfg=cfg, rule_bytes=rule_block_bytes, repo_context=repo_context,
        )
    )

    skipped: dict[str, list[str]] = {SKIP_TEST: [], SKIP_CAP: []}
    priorities: dict[str, float] = {}
    review_mode = "business"  # 默认值, 后续 auto 判断可能覆盖

    def _finalise(
        planned: Sequence[str],
        review: Sequence[str],
        reuse: Sequence[str],
        strategies: Mapping[str, str],
        batches: Sequence[Sequence[str]],
        *,
        single_file: bool = False,
        single_file_over_budget: bool = False,
        cross_reuse: Sequence[str] = (),
    ) -> Plan:
        planned_set = set(planned)
        return Plan(
            files=tuple(planned),
            files_to_review=tuple(review),
            reused=tuple(reuse),
            strategies=dict(strategies),
            batches=tuple(tuple(g) for g in batches),
            skipped={k: tuple(v) for k, v in skipped.items() if v},
            prior_by_file={fp: dict(prior[fp]) for fp in planned if fp in prior},
            file_priorities={fp: priorities[fp] for fp in planned},
            new_files=frozenset(news & planned_set),
            cross_reuse=tuple(cross_reuse),
            single_file=single_file,
            single_file_over_budget=single_file_over_budget,
            overflow=frozenset(overflow & planned_set),
            review_mode=review_mode,
        )

    # 1) 文件扩展名过滤: 跳过 .md/.doc/.png 等非代码文件
    all_files = sorted(set(files))
    excluded_ext = tuple(cfg.review_exclude_extensions)
    if excluded_ext:
        code_files = [
            f for f in all_files
            if not any(f.lower().endswith(ext) for ext in excluded_ext)
        ]
        if len(code_files) < len(all_files):
            logger.info(
                "improve.file_filter project={} mr={} skipped={}",
                project_id, mr_iid, sorted(set(all_files) - set(code_files)),
            )
        all_files = code_files

    # 2) 早退: 无代码文件
    if not all_files:
        return _finalise((), (), (), {}, ())

    # 3) V7 检视模式判断: auto 按 diff 测试文件占比自动切 business/test
    review_mode = cfg.improve_review_mode
    skip_test_patterns = tuple(cfg.improve_skip_test_paths)
    if review_mode == "auto":
        test_count = sum(
            1 for fp in all_files
            if _is_test_file(fp, policy, skip_test_patterns)
        )
        review_mode = "test" if test_count > len(all_files) * 0.5 else "business"
        logger.info(
            "improve.review_mode_auto project={} mr={} test_files={} total={} -> {}",
            project_id, mr_iid, test_count, len(all_files), review_mode,
        )

    # 4) V6 测试路径过滤 (路径级别, 不与扩展名过滤冲突)
    # V7: test 模式下 IMPROVE_SKIP_TEST_PATHS 仍生效 (测试代码即业务代码,
    # 但 conftest 等共享 fixture 文件仍跳过)
    test_skipped = skipped[SKIP_TEST]
    if skip_test_patterns:
        filtered: list[str] = []
        for fp in all_files:
            if _matches_any_path(fp, skip_test_patterns):
                test_skipped.append(fp)
            else:
                filtered.append(fp)
        if test_skipped:
            logger.info(
                "improve.test_path_filter project={} mr={} skipped={}",
                project_id, mr_iid, sorted(test_skipped),
            )
        all_files = filtered

    # 5) C: 自动 case / 测试文件抽样检视 (IMPROVE_TEST_SAMPLE_MAX>0 启用)
    sample_paths = tuple(cfg.improve_test_sample_paths)
    sample_max = cfg.improve_test_sample_max
    if sample_paths and sample_max > 0:
        all_files, unsampled = sample_case_files(all_files, sample_paths, sample_max)
        if unsampled:
            test_skipped.extend(unsampled)
            logger.info(
                "improve.test_file_sampling project={} mr={} total_case={} sampled={} skipped={}",
                project_id, mr_iid, len(unsampled) + sample_max, sample_max, len(unsampled),
            )

    # 6) 早退: 过滤后为空（skipped 数据交给 runner 拼同文案的 summary）
    if not all_files:
        return _finalise((), (), (), {}, ())

    # 7) V7 文件优先级打分 (按 score 倒序排序)
    # 信号: 改动大小(log1p不封顶) + 关键路径 + 改动密度 + 测试特征密度
    diff_map: Mapping[str, Collection[int]] = (
        line_map if line_map is not None
        else parse_diff_line_map("\n".join(diff_by_file.get(fp, "") for fp in all_files))
    )
    file_meta: dict[str, tuple[int, int]] = {}
    for fp in all_files:
        diff_size = len(diff_map.get(fp, ()))
        file_size = read_line_count(fp)
        file_meta[fp] = (diff_size, file_size)

    # 测试特征密度仅 test 模式计算并计权（business 模式不读文件内容）
    test_density: dict[str, float] = {}
    if review_mode == "test" and cfg.improve_priority_weight_test_feature > 0:
        for fp in all_files:
            test_density[fp] = policy.test_feature_density(fp, file_meta[fp][1])

    for fp in all_files:
        priorities[fp] = priority_score(
            fp,
            diff_size=file_meta[fp][0],
            file_size=file_meta[fp][1],
            cfg=cfg,
            test_density=test_density.get(fp, 0.0),
            is_new=fp in news,
            review_mode=review_mode,
        )

    sorted_files = sorted(all_files, key=lambda fp: priorities[fp], reverse=True)

    # 8) V7 strategy 分配 (full / partial / patch)
    strategy = assign_strategy(
        sorted_files, file_meta, overflow,
        cfg=cfg, stat_bytes=stat_bytes, budget_fn=get_budget,
        project_id=project_id, mr_iid=mr_iid,
    )

    # 记录 strategy 分布 (log; 指标计数留在 runner)
    strat_counts = {"full": 0, "partial": 0, "patch": 0}
    for s in strategy.values():
        strat_counts[s] = strat_counts.get(s, 0) + 1
    logger.info(
        "improve.strategy project={} mr={} total={} full={} partial={} patch={} test_skip={}",
        project_id, mr_iid, len(sorted_files),
        strat_counts["full"], strat_counts["partial"], strat_counts["patch"],
        len(test_skipped),
    )

    # 9) E2BIG 单文件捷径标记 (唯一文件的 full 源码块预算在同一 file_meta/budget 上判定)
    single_file = len(sorted_files) <= 1 and not test_skipped
    single_file_over_budget = single_file and single_file_over_full_budget(
        sorted_files, file_meta, stat_bytes=stat_bytes, budget_fn=get_budget,
    )

    # 10) V9 增量复用: 按文件内容指纹 (blob sha) 复用, 取代整 MR head_sha.
    # blob sha 只在文件内容真变时才变 → 只重检变了的文件, 其余复用.
    files_to_review: list[str] = []
    files_reuse: list[str] = []
    cross_reuse: list[str] = []
    if head_sha and prior:
        cur_blobs = {fp: get_blob(fp) for fp in sorted_files}
        # 内容变了的文件的 diff (跨文件影响检测: unchanged 文件标识符出现在
        # changed diff → 强制重检, 防 "B 没变但被 A 牵动" 漏检)
        changed_diffs = {
            fp: diff_by_file.get(fp, "") for fp in sorted_files
            if cur_blobs.get(fp) != (prior.get(fp) or {}).get("blob_sha")
        }
        changed_text = "\n".join(changed_diffs.values())

        for fp in sorted_files:
            prev = prior.get(fp) or {}
            prev_blob = prev.get("blob_sha")
            if prev_blob and cur_blobs.get(fp) == prev_blob:
                reason = _invalidated_reason(
                    prev,
                    profile_fingerprint=profile_fingerprint,
                    ruleset_fingerprint=ruleset_fingerprint,
                )
                if reason:
                    logger.info(
                        "review.state_invalidated project={} mr={} file={} reason={}",
                        project_id, mr_iid, fp, reason,
                    )
                    files_to_review.append(fp)  # 指纹失效 → 重检
                    continue
                # 内容未变 → 跨文件影响检测
                idents = set(policy.extract_symbols(diff_by_file.get(fp, "")))
                cross_impact = any(
                    ident in changed_text for ident in idents if len(ident) >= 3
                ) if changed_text else False
                if cross_impact:
                    cross_reuse.append(fp)
                    files_to_review.append(fp)  # 强制重检
                else:
                    files_reuse.append(fp)  # 复用上轮
            else:
                files_to_review.append(fp)  # 内容变了 / 首次 / blob 未知 → 重检

        if files_reuse:
            logger.info(
                "improve.incremental project={} mr={} total={} review={} reuse={}",
                project_id, mr_iid,
                len(sorted_files), len(files_to_review), len(files_reuse),
            )
    else:
        files_to_review = list(sorted_files)  # 拿不到 head_sha / 首次 → 全检

    # 11) D: 文件数硬上限 (IMPROVE_MAX_FILES_TO_REVIEW) — 只截断送 LLM 的集合,
    # 复用文件不计数 (必须在复用判定之后, 否则复用文件会占配额而漏检)
    cap_truncated = skipped[SKIP_CAP]
    max_files = cfg.improve_max_files_to_review
    if max_files > 0 and len(files_to_review) > max_files:
        files_to_review, truncated = apply_file_count_cap(
            files_to_review, news, tuple(cfg.improve_keyword_paths), max_files,
        )
        if truncated:
            cap_truncated.extend(truncated)
            logger.info(
                "improve.file_count_cap project={} mr={} max={} kept={} truncated={}",
                project_id, mr_iid, max_files, len(files_to_review), len(truncated),
            )

    # 12) V10: 小 diff 文件合并批次 (full 策略文件独享 chunk, 减少 LLM 调用次数)
    batches: list[list[str]] = [[fp] for fp in files_to_review]
    if cfg.improve_batch_enabled and len(files_to_review) > 1:
        batches = group_files_for_batching(
            list(files_to_review), dict(diff_by_file), strategy,
            max_diff_lines=cfg.improve_batch_max_diff_lines,
            max_files=cfg.improve_batch_max_files,
            max_total_lines=cfg.improve_batch_max_total_lines,
        )
        _batched = sum(1 for g in batches if len(g) > 1)
        _merged_files = sum(len(g) for g in batches if len(g) > 1)
        if _batched:
            logger.info(
                "improve.batch project={} mr={} files={} llm_calls={} batched_groups={} merged_files={}",
                project_id, mr_iid, len(files_to_review),
                len(batches), _batched, _merged_files,
            )

    return _finalise(
        sorted_files, files_to_review, files_reuse, strategy, batches,
        single_file=single_file,
        single_file_over_budget=single_file_over_budget,
        cross_reuse=cross_reuse,
    )


__all__ = [
    "Plan",
    "SKIP_CAP",
    "SKIP_TEST",
    "apply_file_count_cap",
    "assign_strategy",
    "full_source_budget_bytes",
    "group_files_for_batching",
    "plan",
    "priority_score",
    "sample_case_files",
    "single_file_over_full_budget",
]
