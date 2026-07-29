"""Diff line map — 从 unified diff 解析每个文件的有效 new_line 集合.

GitLab inline discussion 必须挂在 diff 中的有效行（新增 `+` 行），
否则 GitLab API 会报 "line must be part of the MR diff".

本模块提供:
    parse_diff_line_map(diff_text) -> dict[file_path, set[int]]
        返回每个文件在 diff 中新增 (`+`) 的新文件行号集合.

    find_nearest_valid_line(file_path, target_line, valid_map) -> int | None
        把任意行号 snap 到该文件最接近的有效新增行（向上优先）.

算法:
    逐行扫描 unified diff:
        @@ -old,size +new,size @@ header → 重置 new_line_cursor 到 +new
        `+` 行 → cursor 在 valid set 里
        `-` 行 → cursor 不动（删除不占新文件行号）
        ` ` context 行 → cursor 增 1（在新文件里也有这一行，但不算"变更行"）
        其它（diff --git / index / --- / +++） → 忽略
"""
from __future__ import annotations

import re
from typing import Iterable

# @@ -1,3 +1,5 @@ 或 @@ -1 +1 @@
_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def parse_diff_line_map(diff_text: str) -> dict[str, set[int]]:
    """解析 diff 为 {file_path: set(valid_new_line_numbers)}."""
    result: dict[str, set[int]] = {}
    current_file: str | None = None
    new_cursor: int = 0
    in_hunk = False

    for raw in diff_text.splitlines():
        line = raw.rstrip("\n")
        if line.startswith("diff --git "):
            # diff --git a/<path> b/<path>
            parts = line.split()
            if len(parts) >= 4:
                current_file = parts[-1].removeprefix("b/")
            in_hunk = False
            continue
        if line.startswith("+++ "):
            # +++ b/<path>（新文件路径，更权威）
            path = line.removeprefix("+++ b/").split("\t", 1)[0].strip()
            if path and path != "/dev/null":
                current_file = path
            continue
        if line.startswith("--- "):
            # --- a/<path>（旧文件路径，只取当前行是为了 ctx 不算错）
            continue
        m = _HUNK_HEADER_RE.match(line)
        if m:
            new_cursor = int(m.group(1))
            in_hunk = True
            if current_file:
                result.setdefault(current_file, set())
            continue
        if not in_hunk or current_file is None:
            continue
        if line.startswith("+"):
            result.setdefault(current_file, set()).add(new_cursor)
            new_cursor += 1
        elif line.startswith("-"):
            # 删除行: cursor 不动
            pass
        elif line.startswith(" "):
            # context 行: 在新文件里占一行，但不是变更行（不算 valid target）
            new_cursor += 1
        # 其它（\ No newline at end of file 等）忽略

    return result


def find_nearest_valid_line(
    file_path: str,
    target_line: int,
    valid_map: dict[str, set[int]],
) -> int | None:
    """snap 到该文件最接近 target_line 的 valid 行（向上优先）."""
    valid = valid_map.get(file_path)
    if not valid:
        return None
    if target_line in valid:
        return target_line
    # 向上优先（snap 到 ≤ target 的最大 valid；若有则用；否则 snap 到 ≥ target 的最小 valid）
    le = [n for n in valid if n <= target_line]
    ge = [n for n in valid if n >= target_line]
    if le:
        return max(le)
    if ge:
        return min(ge)
    return None


def format_line_map_for_prompt(line_map: dict[str, set[int]]) -> str:
    """格式化给 agent 的 user prompt.

    例:
        services/foo.py: 13, 14, 15, 27, 28
        services/bar.py: 5, 12
    """
    if not line_map:
        return "(no diff hunks)"
    rows: list[str] = []
    for fp in sorted(line_map):
        lines = sorted(line_map[fp])
        rows.append(f"- `{fp}`: {', '.join(str(n) for n in lines)}")
    return "\n".join(rows)


def summarise(lines: Iterable[int]) -> str:
    """把 sorted 行号压成 range 形式，便于人读.

    例: [1,2,3,5,7,8,9] → "1-3, 5, 7-9"
    """
    nums = sorted(set(lines))
    if not nums:
        return ""
    out: list[str] = []
    i = 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        if i == j:
            out.append(str(nums[i]))
        elif j == i + 1:
            out.append(f"{nums[i]}, {nums[j]}")
        else:
            out.append(f"{nums[i]}-{nums[j]}")
        i = j + 1
    return ", ".join(out)


def annotate_diff_with_line_numbers(diff_text: str) -> str:
    """把每个 `+` 行前面加上 new_line 行号 — pr-agent 风格.

    输入: 标准 unified diff
    输出: 在每个 hunk 的 `+` / ` ` 行前加 `<new_line>	` 前缀（`-` 行保持不变）

    例:
        @@ -0,0 +1,4 @@
        +def foo():
        +    return 1
    →
        @@ -0,0 +1,4 @@
        1	+def foo():
        2	+    return 1
    """
    import re

    lines = diff_text.splitlines(keepends=False)
    out: list[str] = []
    hunk_re = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
    new_cursor = 0
    for line in lines:
        m = hunk_re.match(line)
        if m:
            new_cursor = int(m.group(1))
            out.append(line)
            continue
        if line.startswith("+"):
            out.append(f"{new_cursor}\t{line}")
            new_cursor += 1
        elif line.startswith("-"):
            out.append(line)
            # 删除行 cursor 不动
        elif line.startswith(" "):
            out.append(f"{new_cursor}\t{line}")
            new_cursor += 1
        else:
            out.append(line)
    return "\n".join(out) + "\n"
