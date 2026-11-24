"""被忽略意见 (dismissed) 的分类 + 指纹 —— 跨轮次复用的核心口径.
放在 telemetry 层是因为**写入端** (commands.suggestion_actions)
与**读取端** (review 门面 / prompt 装配) 必须用完全相同的算法,
否则同一个问题在两边的指纹对不上, 历史反馈就永远匹配不上.
设计要点 (2026-11-24):
  · 双层指纹:
      L1 cohort_key       —— 精确: 「同一个问题」(文件+归一化标题+代码内容指纹)
      L2 pattern_fingerprint —— 特征: 「同一类问题」(规则键+标题词根+目录模式)
  · dismissal_kind 分类 —— 只有「技术判断类」的忽略才值得复用;
    「范围外 / 已自行处理」与 AI 的判断能力无关, 复用只会污染 prompt.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

# ── dismissal_kind ──────────────────────────────────────────────
#
# Why 分类: 不是所有 dismiss 都值得让 AI 学.
#   false_positive —— AI 判错了 (误报/不是 bug/不成立)      → 高价值, 该学
#   preference     —— 工程师个人偏好 (风格/规范)            → 可复用 (低优先)
#   out_of_scope   —— 本轮范围外 / 不是这次的问题            → 不复用 (污染)
#   already_fixed  —— 用户自己改了, 不是 AI 的错            → 不复用
#   unknown        —— 理由为空或看不懂                       → 不复用 (保守)
#
# ⚠️ 关键词表是**中文优先 + 英文兜底** —— 匹配用「包含」,
#    顺序有意义 (先判更具体的), 所以用有序列表而不是 set.
_KIND_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "out_of_scope",
        (
            "范围外", "不在本次", "本轮不", "超范围", "不相关", "无关",
            "暂不需要", "以后再说", "后续再", "本次不", "非本次",
            "out of scope", "not in scope", "not relevant", "later",
        ),
    ),
    (
        "already_fixed",
        (
            "已修复", "已改", "已解决", "我自己改", "手动改", "已经处理",
            "already fixed", "already fixed", "fixed by me",
        ),
    ),
    (
        "false_positive",
        (
            "误报", "误判", "不是 bug", "不是bug", "不成立", "不对", "错误",
            "没必要", "不需要改", "无需修改", "本来就这样", "设计如此",
            "有意为之", "false positive", "not a bug", "invalid",
            "wrong", "unnecessary", "by design", "intentional",
        ),
    ),
    (
        "preference",
        (
            "风格", "规范", "约定", "团队习惯", "个人偏好", "不喜欢",
            "建议不用", "不用改", "别改", "保持现状",
            "style", "convention", "preference", "keep as is", "nit",
        ),
    ),
)
# 可复用 (会注入 prompt) 的 kind —— 对应配置 DISMISSAL_REUSE_KINDS
REUSABLE_KINDS: frozenset[str] = frozenset({"false_positive", "preference"})

def classify_dismissal(reason: str | None) -> str:
    """把用户填的忽略原因分类成 dismissal_kind.
    Returns: 'false_positive' | 'preference' | 'out_of_scope' |
             'already_fixed' | 'unknown'
    """
    text = (reason or "").strip().lower()
    if not text:
        return "unknown"
    for kind, keywords in _KIND_RULES:
        for kw in keywords:
            if kw in text:
                return kind
    return "unknown"

def is_reusable_kind(kind: str, allowed: frozenset[str] | None = None) -> bool:
    """该 kind 是否值得跨轮次复用."""
    allowed = allowed if allowed is not None else REUSABLE_KINDS
    return kind in allowed

# ── 指纹 ────────────────────────────────────────────────────────
# 标题里的"噪声"词 —— 去掉后剩下的才是语义词根.
#   (数字、变量名占位、常见套话)
_NOISE_WORDS = frozenset({
    "a", "an", "the", "this", "that", "these", "those",
    "is", "are", "be", "should", "must", "can", "may", "will", "would",
    "to", "of", "in", "on", "at", "for", "and", "or", "not", "no",
    "code", "file", "function", "method", "class", "line", "行", "代码", "文件",
    "please", "suggest", "suggestion", "建议", "问题", "修复",
})
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[一-鿿]+")

def title_signature(header: str | None, limit: int = 6) -> str:
    """标题 → 语义词根签名 (用于 L2 特征指纹).
    做法: 取词 → 去掉噪声词/超短词 → 排序去重 → 截断.
    排序是为了让「同样的词、不同语序」得到同样的签名.
    """
    text = (header or "").strip()
    if not text:
        return ""
    words: list[str] = []
    for w in _WORD_RE.findall(text.lower()):
        if w in _NOISE_WORDS:
            continue
        if len(w) < 2 and not _is_cjk(w):
            continue
        words.append(w)
    uniq = sorted(set(words))
    return "|".join(uniq[:limit])

def _is_cjk(w: str) -> bool:
    return len(w) >= 1 and "一" <= w[0] <= "鿿"

def file_path_pattern(file_path: str | None, depth: int = 3) -> str:
    """文件路径 → 目录模式 (保留前 N 段目录, 丢文件名).
    例: 'testcase/test_tcg/Locking/Lock_Semantics/test_x.py'
      → 'testcase/test_tcg/Locking/**'
    这样同一目录族的问题会落进同一个 pattern.
    """
    path = (file_path or "").strip().replace("\\", "/")
    if not path:
        return ""
    parts = [p for p in path.split("/") if p]
    if len(parts) <= depth:
        return "/".join(parts)
    return "/".join(parts[:depth]) + "/**"

def pattern_fingerprint(
    *,
    file_path: str | None,
    rule_key: str | None,
    header: str | None,
    ruleset_fingerprint: str | None = None,
) -> str:
    """L2 特征指纹 —— 标识「同一类问题」(24 字符 hex).
    构成: 目录模式 + 规则键 + 标题词根签名 (+ 规则集指纹).
    ⚠️ 规则集指纹**可选**参与: 规则集变了历史反馈可能不适用,
       但它会让指纹每逢规则更新就全变 —— 所以只在配置要求时加入.
    """
    parts = [
        file_path_pattern(file_path),
        (rule_key or "").strip(),
        title_signature(header),
    ]
    if ruleset_fingerprint:
        parts.append(ruleset_fingerprint[:12])
    raw = "\x1f".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

def extract_rule_key(rule_keys: str | None) -> str:
    """从 rule_keys 字段取**单个**代表键 (最强信号).
    rule_keys 可能是逗号分隔的多键; 取第一个 —— 同一建议可能命中多条规则,
    但用于指纹只需要一个稳定的代表.
    """
    if not rule_keys:
        return ""
    for part in re.split(r"[,\s;|]+", rule_keys):
        p = part.strip()
        if p:
            return p
    return ""

def summarize_dismissal(
    *,
    reason: str | None,
    kind: str,
    rule_key: str | None,
    header: str | None,
    file_pattern: str | None,
    occurrence_count: int,
    max_reason_len: int = 120,
) -> str:
    """给 prompt 用的一行摘要 (人类可读)."""
    label = {
        "false_positive": "误报",
        "preference": "偏好/风格",
        "out_of_scope": "范围外",
        "already_fixed": "已自行处理",
    }.get(kind, kind)
    r = (reason or "").strip().replace("\n", " ")
    if len(r) > max_reason_len:
        r = r[:max_reason_len] + "…"
    bits = [label]
    if rule_key:
        bits.append(rule_key)
    if file_pattern:
        bits.append(file_pattern)
    head = " · ".join(bits)
    if occurrence_count > 1:
        head += f" · {occurrence_count} 次"
    return f"{head} — {r}" if r else head

def dismissal_entry_from_suggestion(
    sug: dict[str, Any] | None,
    *,
    reason: str | None,
    kind: str | None = None,
) -> dict[str, Any] | None:
    """从 suggestion 行 + dismiss 原因构造一条 dismissed_patterns 记录.
    Returns None 表示**不该记录** (kind 不可复用 / 数据不足).
    """
    if not sug:
        return None
    kind = kind or classify_dismissal(reason)
    if not is_reusable_kind(kind):
        return None
    rule_key = extract_rule_key(sug.get("rule_keys"))
    header = sug.get("header") or ""
    file_path = sug.get("file_path") or ""
    if not rule_key and not header:
        # 既没规则键也没标题 → 没有可复用的特征, 不记
        return None
    return {
        "cohort_key": sug.get("cohort_key") or "",
        "pattern_fingerprint": pattern_fingerprint(
            file_path=file_path,
            rule_key=rule_key,
            header=header,
        ),
        "rule_key": rule_key,
        "file_path_pattern": file_path_pattern(file_path),
        "title_signature": title_signature(header),
        "severity": sug.get("severity") or "",
        "dismissed_reason": (reason or "").strip() or None,
        "dismissal_kind": kind,
        "original_title": header,
        "original_detail": (sug.get("detail") or sug.get("body") or "")[:500] or None,
        "suggestion_note_id": sug.get("note_id") or "",
    }
