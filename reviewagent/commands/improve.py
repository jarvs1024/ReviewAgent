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

from typing import Any

from reviewagent.commands._common import BaseCommand, BaseCommandError
from reviewagent.gitlab.client import GitLabError
from reviewagent.logging_setup import logger
from reviewagent.opencode.client import OpencodeOutputError


# Backward-compat re-exports
ImproveError = BaseCommandError


class ImproveCommand(BaseCommand):
    COMMAND_NAME = "improve"
    DEFAULT_AGENT = "improve"

    def _publish(self, agent_result: dict[str, Any]) -> dict[str, Any]:
        summary_md = (agent_result.get("summary_md") or "").strip()
        suggestions = agent_result.get("suggestions") or []
        if not isinstance(suggestions, list):
            raise OpencodeOutputError(
                f"agent output 'suggestions' must be list, got {type(suggestions).__name__}"
            )

        # 1. 顶层 summary
        top_comment_id: int | None = None
        if summary_md:
            try:
                top_comment_id = self.gitlab.post_mr_comment(
                    self.project_id, self.mr_iid, summary_md
                )
            except GitLabError as e:
                raise BaseCommandError(f"post summary comment failed: {e}") from e

        # 2. 每条 suggestion 作为行内 comment（含 markdown ````suggestion:-N``` 块）
        inline_posted: list[str] = []
        inline_skipped: list[dict[str, Any]] = []
        for s in suggestions:
            try:
                normalised = self._normalise_suggestion(s)
            except ValueError as e:
                logger.warning(
                    "improve.skip_invalid_suggestion project={} mr={} sugg={} err={}",
                    self.project_id, self.mr_iid, s, e,
                )
                inline_skipped.append({"suggestion": s, "reason": str(e)})
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
                inline_skipped.append({"suggestion": s, "reason": "gitlab_rejected"})

        return {
            "top_comment_id": top_comment_id,
            "suggestions_count": len(suggestions),
            "inline_posted": len(inline_posted),
            "inline_skipped": len(inline_skipped),
        }

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
            "body": body,
        }
