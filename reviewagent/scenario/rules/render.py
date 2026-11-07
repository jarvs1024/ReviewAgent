"""规则块渲染（spec §6.6）：verbatim 正文原样返回，structured 渲染规则表."""
from __future__ import annotations

from collections.abc import Sequence

from reviewagent.scenario.profile import Profile
from reviewagent.scenario.rules.resolver import RuleSet


def render_rule_block(
    rule_set: RuleSet,
    *,
    profile: Profile,
    file_paths: Sequence[str] = (),
    budget: int | None = None,
) -> str:
    """渲染注入 prompt 的规则块.

    * `verbatim`：逐字节返回包正文；`budget`（字节）超限直接失败，避免
      静默截断改变 LLM 看到的规则；
    * `structured`：按 `rule_set.rules` 顺序输出
      `| 规则键 | 严重度 | 摘要 |` 表。

    `file_paths` 预留给 T6 的适用性过滤（按 `applies_to.paths` 裁剪规则）。
    """
    if rule_set.render == "verbatim":
        size = len(rule_set.verbatim_body.encode())
        if budget is not None and size > budget:
            raise ValueError(
                f"verbatim rule block exceeds budget for profile {profile.name}: "
                f"{size} > {budget}"
            )
        return rule_set.verbatim_body
    lines = ["| 规则键 | 严重度 | 摘要 |", "|---|---|---|"]
    lines += [f"| {rule.key} | {rule.severity} | {rule.title} |" for rule in rule_set.rules]
    rendered = "\n".join(lines)
    if budget is not None:
        size = len(rendered.encode())
        if size > budget:
            raise ValueError(
                f"structured rule block exceeds budget for profile {profile.name}: "
                f"{size} > {budget}"
            )
    return rendered
