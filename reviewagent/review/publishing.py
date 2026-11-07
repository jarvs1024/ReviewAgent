"""发布与 cohort 归并 — 语言无关的共享实现.

从 `commands/improve.py`（`_handle_cohort_reoccurrence` / `_publish` /
summary 渲染 / guardrail marker 抽取 / 指纹）迁出的活逻辑；门面
(`ImproveCommand`) 只保留薄委托，语言差异经 `PublishCallbacks` 注入
（normalise / validate / detect_apply_risk / summary 构建等）.

架构边界：本模块**不得** import `reviewagent.commands.*` /
`reviewagent.review.policies.*`（语言策略）；`pre_reconcile` 等命令侧依赖
由调用方以回调注入，telemetry store 在函数内惰性导入（保持测试的
`monkeypatch` 语义生效）.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from reviewagent.gitlab.client import GitLabError
from reviewagent.llm import OpencodeOutputError
from reviewagent.logging_setup import logger
from reviewagent.review.validation import (
    suggestion_content_fingerprint,
    suggestion_fingerprint,
)


@dataclass(frozen=True)
class PublishContext:
    """发布所需的最小请求上下文."""

    project_id: int
    mr_iid: int
    gitlab: Any


@dataclass(frozen=True)
class PublishCallbacks:
    """门面注入点：语言策略 / summary 渲染 / overview 刷新 / 预对账."""

    diff_line_map: Callable[[], dict[str, set[int]]]
    head_sha: Callable[[], str | None]
    normalise: Callable[[dict[str, Any]], dict[str, Any]]
    validate: Callable[..., dict[str, Any]]
    detect_apply_risk: Callable[..., tuple[str, list[str]]]
    build_summary_placeholder: Callable[..., str]
    build_summary_v2: Callable[..., str]
    extract_guardrail_markers: Callable[[str], list[str]]
    pre_reconcile: Callable[..., dict[str, Any]]
    publish_overview: Callable[..., Any]
    help_footer: str


def _suggestion_fingerprint(file_path: str, line: int, header: str | None) -> str:
    """Stable fingerprint for suggestion dedup.

    基于 (file:line:header_normalized) 三元组 — 同位置同类问题永远同 fp,
    与 existing_code 行数变化无关 (之前用 sha256(existing_code) 会被 LLM
    给的 1~N 行范围差绕过). lower-case header normalize 让大小写不敏感.

    实现见 review/validation.py:suggestion_fingerprint（薄委托）.
    """
    return suggestion_fingerprint(file_path, line, header)


def _suggestion_content_fingerprint(existing_code: str) -> str:
    """基于 existing_code normalize 后的内容指纹 (16 字符 hex).

    Why 不依赖 line: Apply 一个 suggestion 会让后续行号 +N, 同一行代码
    物理位置变化但内容不变 → 行号 fingerprint 失效, 内容 fingerprint 命中.

    实现见 review/validation.py:suggestion_content_fingerprint（薄委托）.
    """
    return suggestion_content_fingerprint(existing_code)


def _raw_rule_keys(raw: Any) -> list[str]:
    """规范化 agent 输出的 `rule_keys` 字段（列表 → 非空字符串列表）.

    非 list 值一律忽略（返回空列表）：畸形 agent 输出不得退化成
    `[str(value)]` 这类文本兜底键，恢复旧去重的 精确收缩 语义.
    """
    value = raw.get("rule_keys") if isinstance(raw, dict) else None
    if isinstance(value, list):
        return [str(item) for item in value if item]
    return []


def _rule_key_text(normalised: dict[str, Any]) -> str:
    """dedup 用文本：rationale + header（旧去重的扫描范围）."""
    return (normalised.get("rationale") or "") + "\n" + (normalised.get("header") or "")


def _record_rule_key_text(normalised: dict[str, Any]) -> str:
    """record 用文本：只看 rationale（旧 record 正则不扫 header）."""
    return normalised.get("rationale") or ""


def dedup_rule_keys(
    raw: Any, normalised: dict[str, Any], *, rule_set: Any,
) -> str:
    """dedup 用的规则键串（逗号分隔）；空串表示无键.

    优先 agent 显式给出的 `rule_keys`；缺失时按 `rule_set.key_patterns`
    从 rationale/header 抽取（不扩展 promotion 键 — dedup 只做精确收缩）.
    """
    keys = _raw_rule_keys(raw)
    if not keys:
        keys = list(rule_set.key_patterns.extract(_rule_key_text(normalised)))
    return ",".join(keys)


def record_rule_keys(
    raw: Any, normalised: dict[str, Any], *, rule_set: Any,
) -> list[str]:
    """record_suggestion 落库的规则键（保留旧优先级，含 promotion 键）.

    - agent 显式 `rule_keys` 非空 → 以它为基，只追加 promotion 派生键；
    - 为空 → 完全按 `rule_set.normalize_keys(rationale)`.

    Why 只看 rationale：旧 record 正则不扫 header，header 里的键是展示文案，
    不应写进待采纳键（否则与旧行为产生 parity 差异）.
    """
    raw_keys = _raw_rule_keys(raw)
    text = _record_rule_key_text(normalised)
    normalized = rule_set.normalize_keys(text)
    if not raw_keys:
        return normalized
    extracted = set(rule_set.key_patterns.extract(text))
    merged = list(dict.fromkeys(
        [*raw_keys, *[key for key in normalized if key not in extracted]]
    ))
    return merged


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


def make_token_summary(
    prompt_tokens: int, completion_tokens: int, model: str, cost_credits: float = 0.0,
):
    """创建汇总 token 统计的 LLMResult (仅用于 _last_oc_result 替代)."""
    from reviewagent.llm.base import LLMResult

    return LLMResult(
        data={}, prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens, cost_credits=cost_credits, model=model,
    )


def _summary_version(project_id: int, mr_iid: int, *, warn: bool = False) -> int:
    """该 MR 第几次 improve 触发 (0 条时至少为 1)."""
    try:
        from reviewagent.telemetry.store import get_store

        store = get_store()
        runs = store.list_runs(
            project_id=project_id,
            mr_iid=mr_iid,
            command="improve",
            limit=1000,
        )
        return len(runs) or 1
    except Exception as e:  # noqa: BLE001
        if warn:
            logger.warning("improve.summary_version_query failed (non-fatal): {}", e)
        return 1


def build_summary_placeholder(*, project_id: int, mr_iid: int) -> str:
    """生成 summary placeholder (在 inline 循环之前先发, 拿到 note_id 后 edit 为完整内容).

    placeholder 包含版本号 V{N} 让用户在看到第一条 inline 之前就知道这是第几次检视.
    内容会在循环结束后被 edit 为 build_summary_v2 的完整输出.
    """
    version = _summary_version(project_id, mr_iid)
    return f"## 改进总览 V{version}\n\n_加载中…_"


def build_summary_v2(
    *,
    project_id: int,
    mr_iid: int,
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
    version = _summary_version(project_id, mr_iid, warn=True)

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


def extract_guardrail_markers(summary_md: str) -> list[str]:
    """从 _merge_chunks 生成的 summary_md 中抽取文件筛选护栏的提示行.

    V1.1 配套: _publish 重建 summary 时, 把这些行追加到尾部,
    让 GitLab note 显示 C 抽样跳过 / D 文件数截断的提示.
    匹配行首 `> ⏭️ ... 跳过检视` 与 `> ✂️ ... 因超出文件数上限` 开头的一段 (含 IMPROVE_MAX_FILES_TO_REVIEW 配置名).
    """
    if not summary_md:
        return []
    markers: list[str] = []
    # 按 \n\n 切块, 每块开头是 `> ⏭️` 或 `> ✂️` 才保留
    for chunk in re.split(r"\n\n+", summary_md):
        stripped = chunk.strip()
        if not stripped:
            continue
        if stripped.startswith("> ⏭️") or stripped.startswith("> ✂️"):
            markers.append(stripped)
    return markers


def publish(
    ctx: PublishContext,
    merged: dict[str, Any],
    *,
    rule_set: Any,
    profile: Any,
    cfg: Any,
    callbacks: PublishCallbacks,
) -> dict[str, Any]:
    """把 agent_result 应用到 GitLab + 落 telemetry，返回执行摘要.

    `rule_set` 提供规则键抽取 / promotion；`profile` / `cfg` 为后续任务
    预留（当前不使用）.
    """
    project_id, mr_iid, gitlab = ctx.project_id, ctx.mr_iid, ctx.gitlab
    summary_md = (merged.get("summary_md") or "").strip()
    suggestions = merged.get("suggestions") or []
    if not isinstance(suggestions, list):
        raise OpencodeOutputError(
            f"agent output 'suggestions' must be list, got {type(suggestions).__name__}"
        )

    line_map = callbacks.diff_line_map()
    file_sources: dict[str, list[str]] = {}

    # === Head SHA 提前到循环外算一次 (race 修复 Fix A) ===
    # Why: 之前在 dedup check 和 record_suggestion 里各调一次 _get_mr_head_sha (网络),
    #      6 条 suggestion 会触发 12 次 get_mr_diff_refs 调用, 单条 200-800ms 网络往返.
    #      串行叠加后 ~2-9 秒, 这段时间里 _publish 完成 post_mr_discussion 但 SQLite 还没 INSERT,
    #      webhook 上的 /adopt 抢先查到 get_suggestion_by_note_id 返回 None → 误入 no_record 分支
    #      ("✅ 已采纳建议 (无历史记录)").
    # 修复: 进入循环前调用一次 head_sha, 把 head_sha 缓存给所有 iteration 共用.
    # 边界: head_sha 回调内部已经 try/except (网络失败返回 None), 不会阻塞发布流程.
    _publish_head_sha = callbacks.head_sha() or ""

    # === 0a. 顶部"检视汇总"持久评论: 循环前先创建或刷新一次 ===
    # pr_agent 风格: header "## 检视汇总" 自描述锚点, list_mr_notes 本地过滤
    # 找到则 update, 没找到则 post (不存 note_id 到 DB, 避免 schema 迁移).
    # Why 先于 placeholder 创建: GitLab UI 按 created_at 升序展示, 检视汇总要排在
    #      改进总览 V{N} 之上. 失败非致命, 循环结束后还会再调一次.
    try:
        callbacks.publish_overview(
            project_id=project_id, mr_iid=mr_iid,
            inline_posted_count=len(suggestions),
            head_sha=_publish_head_sha,
            gitlab=gitlab,
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
        placeholder_body = callbacks.build_summary_placeholder([], [], len(suggestions))
        top_comment_id = gitlab.post_mr_comment(
            project_id, mr_iid, placeholder_body
        )
    except GitLabError as e:
        logger.warning(
            "improve.post_summary_placeholder_failed (non-fatal) project={} mr={} err={}",
            project_id, mr_iid, e,
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
        _pre_reconcile = callbacks.pre_reconcile(
            project_id=project_id, mr_iid=mr_iid,
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
            normalised = callbacks.normalise(raw)
        except ValueError as e:
            logger.warning(
                "improve.skip_invalid_suggestion project={} mr={} sugg={} err={}",
                project_id, mr_iid, raw, e,
            )
            inline_skipped.append({"suggestion": raw, "reason": str(e)})
            continue

        file_path = normalised["file"]
        start_line = normalised["new_line"]
        improved = normalised["improved_code"]
        existing = (raw.get("existing_code") or "").strip("\n") if isinstance(raw, dict) else ""
        decision = callbacks.validate(
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
                    project_id, mr_iid, file_path, start_line,
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
                # === 兜底: LLM 不输出 rule_keys 字段时, 从 rationale/header
                #     抽 R-XXX / R-OTHER:* / SSD-RULE-* 前缀, 避免 dedup 退化为
                #     纯 (file, line) 兜底而误杀不同规则的建议
                #     (MR 239 other.py L4 medium R-OTHER:magic_number
                #      被 L2 low SSD-RULE-TYPEHINTS 命中 → skip).
                _dedup_rks = dedup_rule_keys(raw, normalised, rule_set=rule_set)
                if _dedup_db.suggestion_exists_at_line(
                    project_id, mr_iid, file_path,
                    decision["new_line"], _sev, head_sha=_head_sha,
                    line_tolerance=2,
                    rule_keys=_dedup_rks or None,
                    existing_code=existing or "",
                ):
                    logger.info(
                        "improve.skip_at_line project={} mr={} file={} line={} severity={} head_sha={}",
                        project_id, mr_iid, file_path,
                        decision["new_line"], _sev,
                        _head_sha[:8] if _head_sha else "",
                    )
                    inline_skipped.append({"suggestion": raw, "reason": "duplicate_at_line"})
                    continue
                # Fingerprint (file:line:header_normalized) — 同位置同类问题永远同 fp.
                # helper 在模块顶部定义, record_suggestion 处复用.
                _dedup_fingerprint = _suggestion_fingerprint(
                    file_path, decision["new_line"], normalised.get("header")
                )
                # Z3+: fingerprint dedup 也传 head_sha, 跟 line dedup 语义保持一致.
                # 已处理状态 (applied/dismissed/resolved) 永远命中;
                # state='open' 必须 head_sha 一致才命中 (force-push 残留 → 放行).
                if _dedup_db.suggestion_exists_by_fingerprint(
                    project_id, mr_iid, _dedup_fingerprint,
                    head_sha=_head_sha,
                ):
                    logger.info(
                        "improve.skip_duplicate project={} mr={} file={} line={} fingerprint={}",
                        project_id, mr_iid, file_path,
                        decision["new_line"], _dedup_fingerprint,
                    )
                    inline_skipped.append({"suggestion": raw, "reason": "duplicate_fingerprint"})
                    continue
            except Exception as _e:
                logger.warning("improve.dedup_check failed (non-fatal): {}", _e)

        if decision["action"] == "post":
            # === apply 风险检测: 引用了 missing 符号时, 在 review 中加 ⚠️ 提示
            # 永远走 post 路径 (保留 Apply 按钮), reviewer 自己决定怎么处理
            _risk_level, _risk_msgs = callbacks.detect_apply_risk(
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
                    project_id, mr_iid, file_path, decision["new_line"],
                    _risk_msgs,
                )
            body_to_post = normalised["body"]
            nc = decision.get("normalised_code") or normalised["improved_code"]
            n_lines = len(nc.split("\n"))
            if nc != normalised["improved_code"]:
                logger.info(
                    "improve.fix_indent project={} mr={} file={} line={}",
                    project_id, mr_iid, file_path, decision["new_line"],
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
                    + callbacks.help_footer
                )
                _warn_block = ""  # 已结构化注入, 避免下面重复

            # === apply 风险: else 分支 (body_to_post 直接用 normalised["body"]) 也需注入 ===
            if _warn_block:
                # 结构化注入: warn_block 紧贴 suggestion 块上方
                idx = body_to_post.find("```suggestion")
                if idx != -1:
                    body_to_post = body_to_post[:idx] + _warn_block + body_to_post[idx:]

            note_id = gitlab.post_mr_discussion(
                project_id,
                mr_iid,
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
                    project_id, mr_iid, file_path, decision["new_line"],
                )
                # 实时刷新顶部"检视汇总": 每发一条 inline 就 update 一次,
                # 让 reviewer 在 GitLab UI 上看到汇总表随检视实时生长.
                # 失败非致命 (网络抖动 / 限速), 循环后还会再刷一次最终版.
                try:
                    callbacks.publish_overview(
                        project_id=project_id, mr_iid=mr_iid,
                        inline_posted_count=len(inline_posted),
                        head_sha=_publish_head_sha,
                        gitlab=gitlab,
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
                    # LLM 不输出 rule_keys 字段 → 从 rationale 文本里抽 R-XXX / R-OTHER / SSD-RULE-* 前缀;
                    # 显式给出时保留旧优先级, 只追加 promotion 派生键 (见 record_rule_keys).
                    rule_keys = record_rule_keys(raw, normalised, rule_set=rule_set)
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
                        f"{file_path}:{_cohort_hdr}".encode()
                    ).hexdigest()[:24]
                    get_store().record_suggestion(
                        project_id=project_id,
                        mr_iid=mr_iid,
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
                            project_id=project_id, mr_iid=mr_iid,
                            cohort_key=cohort_key,
                            new_note_id=note_id,
                            file_path=file_path,
                            target_line=decision["new_line"],
                            target_line_end=(
                                decision["new_line"] + n_lines - 1
                            ) if n_lines > 1 else decision["new_line"],
                            existing=existing,
                            head_sha=head_sha,
                            gitlab=gitlab,
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
                + callbacks.help_footer
            )
            note_id = gitlab.post_mr_discussion(
                project_id,
                mr_iid,
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
                    project_id, mr_iid, file_path, decision["new_line"],
                    decision["reason"],
                )
                try:
                    callbacks.publish_overview(
                        project_id=project_id, mr_iid=mr_iid,
                        inline_posted_count=len(inline_posted),
                        head_sha=_publish_head_sha,
                        gitlab=gitlab,
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
        callbacks.publish_overview(
            project_id=project_id, mr_iid=mr_iid,
            inline_posted_count=len(inline_posted),
            head_sha=_publish_head_sha,
            gitlab=gitlab,
            run_late_detect=True,
        )
    except Exception as _e:
        logger.warning("improve.overview_final_failed (non-fatal): {}", _e)

    # 4. edit placeholder 为完整 summary (含实际 inline_posted 列表).
    #    placeholder 已在循环前创建 (见顶部 step 0), 此处只更新正文.
    summary_md = callbacks.build_summary_v2(inline_posted, inline_skipped, len(suggestions))
    # V1.1: 把 _merge_chunks 生成的 guardrail_markers (⏭️ C 跳过 / ✂️ D 截断)
    #      追加到重建的 summary 后. build_summary_v2 只看 inline_posted/inline_skipped,
    #      不会知道文件筛选护栏丢掉了哪些文件, 需从 merged["summary_md"] 抽 marker 行.
    if summary_md:
        _guardrail_markers = callbacks.extract_guardrail_markers(
            merged.get("summary_md") or ""
        )
        for _marker in _guardrail_markers:
            if _marker and _marker not in summary_md:
                summary_md += f"\n\n{_marker}"
    if top_comment_id and summary_md:
        try:
            gitlab.update_mr_comment(
                project_id, mr_iid, top_comment_id, summary_md
            )
        except GitLabError as e:
            logger.warning(
                "improve.update_summary_failed (non-fatal) project={} mr={} err={}",
                project_id, mr_iid, e,
            )

    return {
        "top_comment_id": top_comment_id,
        "suggestions_count": len(suggestions),
        "inline_posted": len(inline_posted),
        "inline_skipped": len(inline_skipped),
    }


__all__ = [
    "PublishCallbacks",
    "PublishContext",
    "build_summary_placeholder",
    "build_summary_v2",
    "dedup_rule_keys",
    "extract_guardrail_markers",
    "make_token_summary",
    "publish",
    "record_rule_keys",
]
