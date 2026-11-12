"""cohort 身份键 — 跨轮次归并「同一个问题」的共享实现.

放在 telemetry 层是因为写入端 (review.publishing) 与存量数据回填端
(telemetry.store migration) 必须用**完全相同**的算法, 否则同一 MR 里会出现
两套 cohort 口径.
"""
from __future__ import annotations

import hashlib
import re


def content_fingerprint(existing_code: str) -> str:
    """基于 existing_code normalize 后的内容指纹 (16 字符 hex).

    normalize: strip + 折叠多空白为单空格. 缩进差异视为同代码.
    """
    code_norm = re.sub(r"\s+", " ", (existing_code or "").strip())
    return hashlib.sha256(code_norm.encode()).hexdigest()[:16]


def cohort_key(
    file_path: str,
    header: str | None,
    existing_code: str,
    line: int | None = None,
) -> str:
    """cohort 键 —— 标识「同一个问题」, 供跨轮次归并 (supersede / 统计) 使用.

    粒度 = 文件 + 归一化标题 + **代码内容指纹**.

    Why 不是 (file + header):
        那个粒度太粗 —— 同一文件里 3 处**不同位置**、标题文字恰好相同的
        不同问题 (例: 三处日志都叫「日志补上下文」) 会落进同一个 cohort,
        `list_latest_by_cohort` 只保留最新一条 →
          - 图1 / 周报的「总建议数」少算 (GitLab 上看得见的建议没进统计);
          - 采纳率分母偏小 → **采纳率虚高**;
          - 被并掉的那几条在 DB 里仍是 state=open, 在 GitLab 上也仍可见,
            于是「图1 待处理 9 条 / GitLab 上 12 条」—— 用户视角的自相矛盾.
        E2E 实测: 同文件 3 条同标题建议 → 图1 显示总建议数 10, 实际 12.

    Why 用内容指纹而不是行号:
        行号会随用户改动漂移 (原设计的出发点), 而「同一处代码」才是稳定特征;
        内容指纹 (existing_code) 恰好满足: 代码没被改 → 同一问题仍能归并;
        代码被改 → 不归并 (图1 多算一条, 与 GitLab 可见数一致, 方向安全).
        例外: existing_code 为空 (文件级问题) 时内容指纹恒定, 只能用行号兜底,
        否则同文件同标题的多个无锚点问题会重新并成一个.

    Why 不用 rule_keys:
        LLM 可能不输出该字段 (→ 空 → key 不稳定).
    """
    hdr = re.sub(r"\s+", " ", (header or "").strip().lower())
    content_fp = content_fingerprint(existing_code or "")
    has_content = bool((existing_code or "").strip())
    line_part = "" if has_content or not line else f":line={int(line)}"
    return hashlib.sha256(
        f"{file_path}:{hdr}:{content_fp}{line_part}".encode()
    ).hexdigest()[:24]
