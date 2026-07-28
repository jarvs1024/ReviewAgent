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
    DEFAULT_AGENT = "review"

    def _publish(self, agent_result: dict[str, Any]) -> dict[str, Any]:
        summary_md = (agent_result.get("summary_md") or "").strip()
        key_issues = agent_result.get("key_issues") or []
        if not isinstance(key_issues, list):
            raise OpencodeOutputError(
                f"agent output 'key_issues' must be list, got {type(key_issues).__name__}"
            )

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
                normalised = self._normalise_issue(issue)
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
    @staticmethod
    def _normalise_issue(issue: dict[str, Any]) -> dict[str, Any]:
        """校验 + 构造 inline comment 的 body."""
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
        body_lines: list[str] = []
        if severity:
            body_lines.append(f"**[{severity.upper()}]** ")
        body_lines.append(f"**{header}**\n\n{content}")
        return {
            "file": file_path,
            "new_line": start_line,
            "body": "".join(body_lines),
        }
