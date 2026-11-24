"""历史「被忽略意见」的检索与 prompt 注入 (2026-11-24).
设计原则 (为什么这样注入):
  · **只提示, 不压制** —— 阶段 1 只往 prompt 里加「历史反馈」段落,
    让 AI 自己判断要不要再提同类问题; 跨 MR 的**精确压制**是独立开关,
    默认关闭 (误杀风险高: 曾经的误报这次可能真的成立).
  · **说人话** —— 注入的是「工程师当时说了什么」, 不是抽象的规则编号.
    AI 看到具体原因才学得到; 只给 rule_key 它无法迁移判断.
  · **给逃生口** —— 明确告诉 AI「如果你仍认为必须提, 请说明与历史反馈的差异」,
    否则它会变得过度保守 (宁可漏报也不报), 那比误报更糟.
  · **控制预算** —— 最多 N 条 (config.dismissal_reuse_max_items),
    每条一行, 避免 prompt 膨胀挤掉真正的代码上下文.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from reviewagent.logging_setup import logger
from reviewagent.telemetry.dismissal import (
    pattern_fingerprint,
    summarize_dismissal,
)


def _allowed_kinds(config) -> frozenset[str]:
    raw = (getattr(config, "dismissal_reuse_kinds", "") or "").strip()
    if not raw:
        return frozenset()
    return frozenset(p.strip() for p in raw.split(",") if p.strip())

def _lookback_since(config) -> str | None:
    """时间窗口下界 (ISO). None = 不限时间.

    ⚠️ `dismissal_reuse_lookback_days = 0` 的语义是「只看今天」,
       **不是**「不限」—— 不限会让历史无限累积, 违背「最近 N 天」的直觉.
    """
    days = int(getattr(config, "dismissal_reuse_lookback_days", 0) or 0)
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()

def collect_dismissal_context(
    *,
    config,
    project_id: int,
    file_paths: list[str] | None = None,
    rule_keys: list[str] | None = None,
    cohort_keys: list[str] | None = None,
    store=None,
) -> list[dict[str, Any]]:
    """检索与本次检视相关的历史「被忽略意见」.
    检索维度:
      · L2 特征指纹 —— 用「本次要检的文件路径」反推 pattern
        (目录模式 + 规则键), 命中说明「这类问题在这片目录被忽略过」
      · 规则键 —— 本次命中的规则曾被反复 dismiss
      · L1 精确指纹 —— 同一处代码被 dismiss 过
    Returns: 命中的 pattern 行 (已按 occurrence_count 排序, 已截断).
    """
    if not getattr(config, "dismissal_reuse_enabled", False):
        return []
    kinds = _allowed_kinds(config)
    if not kinds:
        return []
    if store is None:
        from reviewagent.telemetry.store import get_store
        store = get_store()
    # 用待检文件反推 L2 指纹候选 (目录模式 × 已知规则键)
    fps: list[str] = []
    for fp in file_paths or []:
        for rk in rule_keys or []:
            fps.append(pattern_fingerprint(
                file_path=fp, rule_key=rk, header=""))
    # 只有标题词根才能完全对上, 但我们不知道本次的标题 → 用规则键 + 目录模式
    # 命中的是「同目录 + 同规则」这一维, 够用 (精确维靠 cohort_key 覆盖).
    fps = list(dict.fromkeys(fps))
    try:
        rows = store.list_dismissed_patterns(
            project_id=project_id,
            pattern_fingerprints=fps or None,
            rule_keys=rule_keys or None,
            cohort_keys=cohort_keys or None,
            kinds=tuple(kinds),
            since=_lookback_since(config),
            min_occurrences=int(getattr(config, "dismissal_reuse_min_occurrences", 1) or 1),
            limit=int(getattr(config, "dismissal_reuse_max_items", 8) or 8),
        )
    except Exception as e:  # noqa: BLE001 - 检索失败不能挡住检视
        logger.warning("dismissal_context.collect failed (non-fatal): {}", e)
        return []
    return rows

def render_dismissal_context(rows: list[dict[str, Any]]) -> str:
    """把命中的历史忽略渲染成 prompt 段落 (空串 = 不注入).
    ⚠️ 措辞很重要: 不能让 AI 觉得"用户在否定我" —— 那会过度保守.
       所以强调「仅供参考 / 若仍成立请说明差异」.
    """
    if not rows:
        return ""
    lines = []
    for row in rows:
        lines.append(
            "- " + summarize_dismissal(
                reason=row.get("dismissed_reason"),
                kind=row.get("dismissal_kind") or "unknown",
                rule_key=row.get("rule_key"),
                header=row.get("original_title"),
                file_pattern=row.get("file_path_pattern"),
                occurrence_count=int(row.get("occurrence_count") or 1),
            )
        )
    if not lines:
        return ""
    return (
        "## 历史反馈（同类问题曾被工程师忽略）\n\n"
        "以下问题在本项目历史检视中已被工程师忽略，请**避免重复提出同类问题**：\n"
        + "\n".join(lines)
        + "\n\n"
        "（以上仅为历史反馈，不代表当前代码仍有问题。"
        "如果你认为当前仍需提出，请说明与上述历史反馈的差异。）\n\n"
        "---\n\n"
    )

def collect_and_render(
    *,
    config,
    project_id: int,
    file_paths: list[str] | None = None,
    rule_keys: list[str] | None = None,
    cohort_keys: list[str] | None = None,
    store=None,
) -> str:
    """检索 + 渲染 (一步到位). 开关关闭 / 无命中 → 空串."""
    rows = collect_dismissal_context(
        config=config,
        project_id=project_id,
        file_paths=file_paths,
        rule_keys=rule_keys,
        cohort_keys=cohort_keys,
        store=store,
    )
    if not rows:
        return ""
    section = render_dismissal_context(rows)
    if section:
        logger.info(
            "dismissal_context.injected project={} items={}",
            project_id, len(rows),
        )
    return section
