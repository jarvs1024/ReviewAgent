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

from pathlib import Path
from typing import Any

from reviewagent.commands._common import BaseCommand, BaseCommandError
from reviewagent.git.diff_lines import (
    find_nearest_valid_line,
    format_line_map_for_prompt,
    parse_diff_line_map,
)
from reviewagent.gitlab.client import GitLabError
from reviewagent.logging_setup import logger
from reviewagent.opencode.client import OpencodeOutputError


# Backward-compat re-exports
ImproveError = BaseCommandError




class ImproveCommand(BaseCommand):
    COMMAND_NAME = "improve"
    DEFAULT_AGENT = "improve"

    # ---------- helpers ----------
    def _diff_line_map(self) -> dict[str, set[int]]:
        """读 self.ws.diff_file 解析每个文件的 valid new_line 集合."""
        if not self.ws or not self.ws.diff_file:
            return {}
        try:
            diff_text = Path(self.ws.diff_file).read_text(encoding="utf-8")
        except OSError:
            return {}
        return parse_diff_line_map(diff_text)

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
        target_first = target_lines[0].strip()
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
        if not line_map:
            return (
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

    def _publish(self, agent_result: dict[str, Any]) -> dict[str, Any]:
        summary_md = (agent_result.get("summary_md") or "").strip()
        suggestions = agent_result.get("suggestions") or []
        if not isinstance(suggestions, list):
            raise OpencodeOutputError(
                f"agent output 'suggestions' must be list, got {type(suggestions).__name__}"
            )

        line_map = self._diff_line_map()
        file_sources: dict[str, list[str]] = {}

        # 1. 顶层 summary
        top_comment_id: int | None = None
        if summary_md:
            try:
                top_comment_id = self.gitlab.post_mr_comment(
                    self.project_id, self.mr_iid, summary_md
                )
            except GitLabError as e:
                raise BaseCommandError(f"post summary comment failed: {e}") from e

        # 2. 每条 suggestion：先校验 new_line + improved_code 对齐
        inline_posted: list[str] = []
        inline_skipped: list[dict[str, Any]] = []
        general_added: list[str] = []
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
            if decision["action"] == "post":
                body_to_post = normalised["body"]
                nc = decision.get("normalised_code")
                if nc and nc != normalised["improved_code"]:
                    logger.info(
                        "improve.fix_indent project={} mr={} file={} line={}",
                        self.project_id, self.mr_iid, file_path, decision["new_line"],
                    )
                    n_lines = len(nc.split("\n"))
                    sev = normalised.get("severity", "medium").upper()
                    body_to_post = (
                        f"**[{sev}]** **{normalised['header']}** — {normalised['label']}\n\n"
                        f"{normalised['rationale']}\n\n"
                        f"```suggestion:-0+{n_lines}\n{nc}\n```"
                    )
                note_id = self.gitlab.post_mr_discussion(
                    self.project_id,
                    self.mr_iid,
                    body_to_post,
                    file_path=file_path,
                    new_line=decision["new_line"],
                )
                if note_id:
                    inline_posted.append(note_id)
                    logger.info(
                        "improve.post_inline project={} mr={} file={} line={}",
                        self.project_id, self.mr_iid, file_path, decision["new_line"],
                    )
                else:
                    inline_skipped.append({"suggestion": raw, "reason": "gitlab_rejected"})
            elif decision["action"] == "general":
                # 降级为 general comment（同一 file 聚合到一条）
                general_added.append(
                    f"- **{normalised['header']}** ({file_path}:{start_line}): "
                    f"{normalised['rationale']}"
                )
                inline_skipped.append({
                    "suggestion": raw,
                    "reason": decision["reason"],
                })
                logger.warning(
                    "improve.degrade_general project={} mr={} file={} line={} reason={}",
                    self.project_id, self.mr_iid, file_path, start_line,
                    decision["reason"],
                )
            else:
                # dropped
                inline_skipped.append({"suggestion": raw, "reason": decision["reason"]})

        # 把降级的条目合并到 summary 末尾（或发一条独立 follow-up）
        if general_added:
            try:
                self.gitlab.post_mr_comment(
                    self.project_id, self.mr_iid,
                    "## 改进补充（无法 Apply 的建议）\n\n"
                    + "\n".join(general_added)
                    + "\n\n_以下建议因位置/内容不在 diff 范围内，未生成可 Apply 代码块；请人工核对。_",
                )
            except GitLabError as e:
                logger.warning("improve.post_general_comment failed: {}", e)

        return {
            "top_comment_id": top_comment_id,
            "suggestions_count": len(suggestions),
            "inline_posted": len(inline_posted),
            "inline_skipped": len(inline_skipped),
            "degraded_to_general": len(general_added),
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
        """校验 + snap — 返回 {"action": post|general|drop, "new_line": int, "reason": str}.

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
            return {"action": "general", "new_line": start_line,
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
            # 信任模型: existing_code 已通过 snap 校验 + improved 行数 > existing 行数
            # 说明模型在把单行展开成多行(如 with... + return...)
            # 但仍要校验: improved 第一行缩进 == target_line 缩进, 否则自动补齐
            normalised_code = self._fix_indent(target_line_raw, improved_code)
            logger.info(
                "improve.multiline_replace project={} mr={} file={} line={} existing_lines={} improved_lines={}",
                self.project_id, self.mr_iid, file_path, start_line,
                len(existing_lines), len(improved_lines),
            )
            # 更新 normalised_code 到 normalisation 结果 (在 publish 阶段生效)
            return {"action": "post", "new_line": start_line, "reason": "multi_line_replacement",
                    "normalised_code": normalised_code}

        # 4b. 正常的对齐检查 (1→1 或 N→N 等行数替换)
        if not _code_first_line_matches(target_line, imp_first):
            return {"action": "general", "new_line": start_line,
                    "reason": f"improved_code first line doesn't match file:{start_line} ({target_line!r} vs {imp_first!r})"}

        # 4c. 缩进修正: 若 improved_code 第一行缺缩进, 自动补齐
        normalised_code = self._fix_indent(target_line_raw, improved_code)

        return {"action": "post", "new_line": start_line, "reason": "ok",
                "normalised_code": normalised_code}

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
        if not improved:
            raise ValueError("missing 'improved_code' (non-empty)")

        header = (s.get("header") or "建议改进").strip()
        rationale = (s.get("rationale") or "").strip()
        label = (s.get("label") or "enhancement").strip()
        severity = (s.get("severity") or "medium").strip().lower()

        # GitLab suggestion 格式: ```suggestion:-A+B
        # - A (默认 0, 负数表示从 new_line 往下删除 A 行)
        # - B (正数) = 替换 B 行(包含 new_line 那行), 建议块内容填这里
        # pr-agent 用法: range = existing_lines_end - existing_lines_start + 1
        # 我们没有 end_line, 但有 existing_code 行数 → range = existing 行数(最少 1)
        existing_lines = existing.split("\n") if existing else []
        range_n = max(1, len(existing_lines))

        body = (
            f"**[{severity.upper()}]** **{header}** — {label}\n\n"
            f"{rationale}\n\n"
            f"```suggestion:-0+{range_n}\n{improved}\n```"
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
