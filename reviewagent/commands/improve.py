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

    def _build_user_prompt(self) -> str:
        """把 diff 的 valid new_line 集合喂给 agent — 严格约束它的 start_line 取值."""
        line_map = self._diff_line_map()
        if not line_map:
            return (
                "请按你的 system prompt 处理当前 MR 的 diff"
                "（变更内容见上方附件文件）。"
            )
        formatted = format_line_map_for_prompt(line_map)
        return (
            "请按你的 system prompt 处理当前 MR 的 diff\n"
            "（变更内容见上方附件文件）。\n\n"
            "## diff 有效新增行（VALID NEW LINES）\n\n"
            "下面列出本次 diff 里每个文件所有以 `+` 开头的**新文件行号**。"
            "**你的 `start_line` 必须且只能从此集合中取**；"
            "若你怀疑某 issue 的目标行不在此集合里（context 行 / 删除行 / "
            "跨文件推断），请放弃 `improved_code`，改为在 `summary_md` 里文字描述，"
            "不要强行填一个错位的 suggestion。\n\n"
            f"{formatted}\n"
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
            decision = self._validate_suggestion(
                file_path=file_path,
                start_line=start_line,
                improved_code=improved,
                line_map=line_map,
                file_sources=file_sources,
            )
            if decision["action"] == "post":
                note_id = self.gitlab.post_mr_discussion(
                    self.project_id,
                    self.mr_iid,
                    normalised["body"],
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
        line_map: dict[str, set[int]],
        file_sources: dict[str, list[str]],
    ) -> dict[str, Any]:
        """校验 + snap — 返回 {"action": post|general|drop, "new_line": int, "reason": str}.

        校验顺序:
          1. new_line 是否在 diff valid 集合内；不在 → snap 到最近 valid
          2. snap 后 improved_code 第一行是否与 file[start_line-1] 匹配
          3. 任一不匹配 → degrade 为 general comment
        """
        valid = line_map.get(file_path)
        # 文件不在 diff 中（agent 乱猜）→ drop
        if valid is None:
            return {"action": "drop", "new_line": start_line,
                    "reason": f"file '{file_path}' not in diff"}

        # 1. snap 到最近 valid
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

        # 2. improved_code 第一行 vs file[start_line-1] 对齐检查
        if file_path not in file_sources:
            file_sources[file_path] = self._read_file_lines(file_path)
        file_lines = file_sources[file_path]
        if not file_lines or start_line - 1 >= len(file_lines):
            return {"action": "general", "new_line": start_line,
                    "reason": "file content unavailable for alignment check"}

        target_line = file_lines[start_line - 1].strip()
        imp_first = (improved_code.splitlines()[0] if improved_code else "").strip()
        if not _code_first_line_matches(target_line, imp_first):
            return {"action": "general", "new_line": start_line,
                    "reason": f"improved_code first line doesn't match file:{start_line} ({target_line!r} vs {imp_first!r})"}

        return {"action": "post", "new_line": start_line, "reason": "ok"}

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

        body = (
            f"**[{severity.upper()}]** **{header}** — {label}\n\n"
            f"{rationale}\n\n"
            f"```suggestion:-0\n{improved}\n```"
        )
        return {
            "file": file_path,
            "new_line": start_line,
            "improved_code": improved,
            "header": header,
            "rationale": rationale,
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
