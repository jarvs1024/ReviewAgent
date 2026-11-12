"""建议「可 Apply 性」判定 — replace / delete / text_only 三种语义.

Why (真实业务回归, p31 / MR !2 / note_1095 / note_1096):
    模型输出里 `improved_code` 为空有两种**完全不同的**含义, 但字段本身无法区分:

    1. 「删掉这段代码」            → 删代码**就是**建议的内容, 应当保留 Apply 按钮;
    2. 「我给不出可直接 Apply 的改法」→ 只是一条文字说明, **绝不能**出现 Apply 按钮.

    早期实现把两者一律当成 (1): 空 improved_code + 非空 existing_code → 发布成
    空的 ```suggestion:-0+0``` 块 → GitLab 渲染出 **Apply suggestion** 按钮.
    reviewer 一点就把那行**有效代码**删掉了 —— 而模型 (2) 根本没打算删它, 只是
    无法给出可 Apply 的改法 (note_1096: caller 参数不匹配, 缺 blk_id 无法确定).

判定优先级:
    1. 模型显式声明 `applicability` 字段 (replace / delete / text_only) —— 契约优先;
    2. `improved_code` 非空 → replace;
    3. `improved_code` 为空 → 文本意图判定:
       text_only 关键词 → delete 关键词 → 兜底 text_only.
       兜底选 text_only 是刻意的**安全优先**: 误发删除型 Apply 的代价 (删掉有效代码)
       远大于漏发删除型 Apply 的代价 (reviewer 手工删一行).
"""
from __future__ import annotations

import re
from typing import Any, Mapping

REPLACE = "replace"
DELETE = "delete"
TEXT_ONLY = "text_only"

_APPLICABILITY_VALUES = (REPLACE, DELETE, TEXT_ONLY)

# 「给不出可 Apply 的代码」意图关键词 —— 命中即降级为纯文字说明 (无 Apply 按钮).
# 前 6 条来自旧 publishing.py 的 force_general_not_in_diff 关键词表 (MR1121),
# 保留以兼容既有行为; 其余为 note_1096 实测暴露出来的说法.
_TEXT_ONLY_KEYWORDS: tuple[str, ...] = (
    # --- 既有 (MR1121) ---
    "不在 valid new lines",
    "不在本次 diff",
    "无法提供可 apply",
    "降级为普通评论",
    "not in valid new lines",
    "not in diff",
    # --- 中文补充 ---
    "不提供可直接 apply",
    "不提供可 apply",
    "无法提供可直接 apply",
    "无法提供可 apply",
    "不可直接 apply",
    "无法 apply",
    "无法应用",
    "不可应用",
    "没有可直接 apply",
    "无可 apply",
    "无可直接 apply",
    "无法直接应用",
    "只做文字",
    "仅文字",
    "纯文字",
    "文字说明",
    # --- 英文补充 ---
    "cannot be applied",
    "can not be applied",
    "no applicable code",
    "not directly applicable",
    "text only",
    "text_only",
)

# 「建议删除现有代码」意图 —— 命中则保留 Apply 按钮 (删除本身就是建议内容).
# 用正则而不是固定短语: 真实 rationale 的说法太散 ("删除 dead code" /
# "移除该 import" / "删掉这行" / "remove the import"), 枚举短语必然漏.
_DELETE_INTENT_RE = re.compile(
    r"(删除|移除|删掉|去掉|干掉|remove|delete)",
    re.IGNORECASE,
)

# 删除意图的否定形式 —— 命中则**不**按删除处理 (避免把「不应删除」误判成删除建议).
_DELETE_NEGATION_RE = re.compile(
    r"(不应删除|不要删除|不建议删除|无需删除|不能删除|不可删除|勿删除|禁止删除"
    r"|避免删除|防止删除|不应移除|不要移除|不建议移除|无需移除"
    r"|not\s+remove|do\s+not\s+remove|don'?t\s+remove|should\s+not\s+remove"
    r"|shouldn'?t\s+remove)",
    re.IGNORECASE,
)


def _norm(text: Any) -> str:
    return str(text or "").lower()


def has_text_only_intent(text: Any) -> bool:
    """文本是否表达了「我给不出可直接 Apply 的代码」."""
    low = _norm(text)
    return any(kw in low for kw in _TEXT_ONLY_KEYWORDS)


def has_delete_intent(text: Any) -> bool:
    """文本是否表达了「应当删除这段代码」 (否定形式不算)."""
    low = _norm(text)
    if _DELETE_NEGATION_RE.search(low):
        return False
    return bool(_DELETE_INTENT_RE.search(low))


def resolve_applicability(
    raw: Mapping[str, Any] | None,
    *,
    improved: str = "",
    existing: str = "",
    header: str = "",
    rationale: str = "",
) -> str:
    """判定一条 suggestion 的可 Apply 语义.

    返回 `REPLACE` / `DELETE` / `TEXT_ONLY` 之一.
    """
    raw = raw if isinstance(raw, Mapping) else {}
    improved_s = (improved or "").strip()
    existing_s = (existing or "").strip()

    explicit = _norm(raw.get("applicability") or raw.get("suggestion_kind"))
    if explicit in _APPLICABILITY_VALUES:
        if explicit == TEXT_ONLY:
            return TEXT_ONLY
        if improved_s:
            # 有代码 → 就是替换 (delete 声明与实际代码冲突时以代码为准).
            return REPLACE
        if explicit == DELETE and existing_s:
            return DELETE
        return TEXT_ONLY

    if improved_s:
        return REPLACE

    intent_text = f"{header}\n{rationale}"
    if has_text_only_intent(intent_text):
        return TEXT_ONLY
    if has_delete_intent(intent_text):
        return DELETE
    return TEXT_ONLY
