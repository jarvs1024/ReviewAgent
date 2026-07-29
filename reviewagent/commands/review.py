"""/review 命令端到端.

工作流:
    1. 调 opencode agent `review`
    2. 解析 agent 返回 {summary_md, key_issues[]}
    3. 把 summary_md 作为 MR 顶层普通评论发出
    4. 把每条 key_issue 作为行内 Discussion 评论发出
    5. 全部 publish 后 emit_run_finished(success)

注意:
    key_issues 的 file / start_line 必须与 diff 中实际位置一致，否则 GitLab
    会拒绝行内评论（缺 diff_refs 或行号落在 context 之外）。review agent
    自己负责定位；本模块只搬运。
"""
from __future__ import annotations

from typing import Any

from reviewagent.commands._common import BaseCommand, BaseCommandError
from reviewagent.gitlab.client import GitLabError
from reviewagent.logging_setup import logger
from reviewagent.opencode.client import OpencodeOutputError


# Backward-compat re-exports
ReviewError = BaseCommandError


class ReviewCommand(BaseCommand):
    COMMAND_NAME = "review"
    DEFAULT_AGENT = "reviewer"  # opencode frontmatter name in prompts/review.md

    def _publish(self, agent_result: dict[str, Any]) -> dict[str, Any]:
        summary_md = (agent_result.get("summary_md") or "").strip()
        key_issues = agent_result.get("key_issues") or []
        if not isinstance(key_issues, list):
            raise OpencodeOutputError(
                f"agent output 'key_issues' must be list, got {type(key_issues).__name__}"
            )

        # 0. 预读每个 file 的 source（用于校验 improved_code 与 start_line 对齐）
        file_sources: dict[str, list[str]] = {}
        for issue in key_issues:
            fp = issue.get("file") if isinstance(issue, dict) else None
            if fp and fp not in file_sources:
                file_sources[fp] = self._read_file_lines(fp)

        # 1. 顶层 summary 评论
        top_comment_id: int | None = None
        if summary_md:
            try:
                top_comment_id = self.gitlab.post_mr_comment(
                    self.project_id, self.mr_iid, summary_md
                )
            except GitLabError as e:
                raise BaseCommandError(f"post summary comment failed: {e}") from e

        # 2. 行内评论
        inline_posted: list[str] = []
        inline_skipped: list[dict[str, Any]] = []
        for issue in key_issues:
            try:
                normalised = self._normalise_issue(
                    issue,
                    file_lines=file_sources.get(issue.get("file", ""), []),
                )
            except ValueError as e:
                logger.warning(
                    "review.skip_invalid_issue project={} mr={} issue={} err={}",
                    self.project_id, self.mr_iid, issue, e,
                )
                inline_skipped.append({"issue": issue, "reason": str(e)})
                continue
            note_id = self.gitlab.post_mr_discussion(
                self.project_id,
                self.mr_iid,
                normalised["body"],
                file_path=normalised["file"],
                new_line=normalised["new_line"],
            )
            if note_id:
                inline_posted.append(note_id)
            else:
                inline_skipped.append({
                    "issue": issue,
                    "reason": "gitlab_rejected",
                })

        return {
            "top_comment_id": top_comment_id,
            "key_issues_count": len(key_issues),
            "inline_posted": len(inline_posted),
            "inline_skipped": len(inline_skipped),
            "skipped_reasons": inline_skipped[:3],
        }

    # ---------- helpers ----------
    def _normalise_issue(
        self,
        issue: dict[str, Any],
        file_lines: list[str] | None = None,
    ) -> dict[str, Any]:
        """校验 + 构造 inline comment 的 body.

        Body 布局 (PR-Agent review 风格):
            **[SEVERITY]** **Header**  [importance: N]  — label

            content 描述 (中文)

            ```suggestion:-0
            improved_code
            ```
        """
        if not isinstance(issue, dict):
            raise ValueError(f"issue must be dict, got {type(issue).__name__}")
        file_path = issue.get("file")
        if not file_path or not isinstance(file_path, str):
            raise ValueError("missing 'file' (str)")
        start_line = issue.get("start_line")
        if not isinstance(start_line, int) or start_line <= 0:
            raise ValueError("missing 'start_line' (int > 0)")

        header = (issue.get("header") or "Issue").strip()
        content = (issue.get("content") or "").strip()
        severity = (issue.get("severity") or "").lower()
        importance = issue.get("importance")
        label = (issue.get("label") or "").strip().lower()
        existing = (issue.get("existing_code") or "").strip("\n")
        improved = (issue.get("improved_code") or "").strip("\n")

        # 第一行: severity + header + importance + label
        first_parts: list[str] = []
        if severity:
            first_parts.append(f"**[{severity.upper()}]**")
        first_parts.append(f"**{header}**")
        if isinstance(importance, int) and 1 <= importance <= 10:
            first_parts.append(f"`importance: {importance}`")
        if label:
            first_parts.append(f"— `{label}`")
        head_line = " ".join(first_parts) + "\n\n"

        body = head_line + content

        # 可 Apply 的 suggestion 块（仅当 improved_code 非空且与 start_line 对齐）
        if improved:
            aligned = self._check_suggestion_aligned(file_lines, start_line, improved)
            if aligned:
                body += (
                    "\n\n```suggestion:-0\n"
                    f"{improved}\n"
                    "```"
                )
            else:
                logger.warning(
                    "review.suggestion_misaligned project={} mr={} file={} start_line={} — "
                    "improved_code 第一行与 file 中 start_line 处代码不匹配，降级为纯文字描述",
                    self.project_id, self.mr_iid, file_path, start_line,
                )
        return {
            "file": file_path,
            "new_line": start_line,
            "body": body,
        }

    @staticmethod
    def _check_suggestion_aligned(
        file_lines: list[str] | None,
        start_line: int,
        improved_code: str,
    ) -> bool:
        """校验 improved_code 第一行确实是替换 file 中 start_line 那一行.

        启发式:
          1. improved_code 第一非空行的"主体 token"（去缩进后 ≥3 字符的标识符）
             必须出现在 file[start_line-1] 中（说明替换目标就是这一行）
          2. 或 file[start_line-1] 是 `def foo(...)` 行，improved_code 第一行
             也是 `def foo(...)` 行（说明改进目标就是这个函数）
        """
        if not file_lines or start_line < 1 or start_line > len(file_lines):
            return False
        target = file_lines[start_line - 1]
        imp_lines = [l for l in improved_code.split("\n") if l.strip()]
        if not imp_lines:
            return False
        first = imp_lines[0]
        # 同 def 行（且函数名一致 — 避免 SQL/eval 等多 def 文件的误匹配）
        if target.lstrip().startswith("def ") and first.lstrip().startswith("def "):
            import re as _re
            m_t = _re.match(r"\s*def\s+(\w+)", target)
            m_f = _re.match(r"\s*def\s+(\w+)", first)
            if m_t and m_f and m_t.group(1) == m_f.group(1):
                return True
        # 同 class 行
        if target.lstrip().startswith("class ") and first.lstrip().startswith("class "):
            return True
        # token overlap: 共享 ≥3 字符的标识符
        import re
        target_tokens = set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b", target))
        first_tokens = set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b", first))
        # 排除太通用的 token
        common_stop = {"def", "return", "self", "None", "True", "False", "and", "or",
                       "not", "if", "else", "elif", "for", "while", "in", "is",
                       "import", "from", "class", "try", "except", "finally",
                       "with", "as", "raise", "pass", "lambda", "yield"}
        overlap = (target_tokens & first_tokens) - common_stop
        return bool(overlap)

    def _read_file_lines(self, file_path: str) -> list[str]:
        """从当前 worktree 读 file 源，按行 split；读不到返回 []."""
        try:
            from pathlib import Path
            ws = getattr(self, "ws", None)
            wt = Path(ws.worktree) if ws and getattr(ws, "worktree", None) else None
            if not wt or not wt.is_dir():
                return []
            p = wt / file_path
            if not p.exists():
                return []
            return p.read_text(encoding="utf-8", errors="replace").split("\n")
        except Exception as e:
            logger.warning("review._read_file_lines failed file={} err={}", file_path, e)
            return []
