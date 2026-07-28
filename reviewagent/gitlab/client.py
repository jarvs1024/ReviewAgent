"""GitLab 客户端 — 薄封装 python-gitlab.

原则: 只做搬运（拉 diff / 发评论 / 写 description），不做任何代码理解或解析.
所有「理解代码」的工作交给 opencode agent.
"""
from __future__ import annotations

from typing import Any

import gitlab

from reviewagent.config import config
from reviewagent.logging_setup import logger


class GitLabError(RuntimeError):
    pass


class GitLabClient:
    def __init__(self):
        # python-gitlab 实例
        self._gl = gitlab.Gitlab(
            url=config.gitlab_url,
            private_token=config.gitlab_pat,
            timeout=30,
        )

    # ---------- MR 元信息 ----------
    def get_mr(self, project_id: int, mr_iid: int) -> dict[str, Any]:
        """拉取 MR 完整元信息."""
        try:
            project = self._gl.projects.get(project_id)
            mr = project.mergerequests.get(mr_iid)
        except gitlab.exceptions.GitlabError as e:
            raise GitLabError(f"get_mr failed: {e}") from e
        return mr.asdict() if hasattr(mr, "asdict") else dict(mr)

    # ---------- 项目 ----------
    def get_project_git_url(self, project_id: int) -> str:
        """返回带 token 的 git URL（给 git clone --bare 用）.

        同时支持 http:// 与 https:// GitLab 实例.
        """
        try:
            project = self._gl.projects.get(project_id)
        except gitlab.exceptions.GitlabError as e:
            raise GitLabError(f"get_project_git_url failed: {e}") from e

        path = getattr(project, "path_with_namespace", None) or str(project_id)
        base = self._gl.url.rstrip("/")
        scheme = "https" if base.startswith("https://") else "http"
        host = base[len(f"{scheme}://"):]
        return f"{scheme}://oauth2:{self._gl.private_token}@{host}/{path}.git"

    # ---------- MR diff ----------
    def get_mr_diff(self, project_id: int, mr_iid: int) -> str:
        """拉取 MR 的 unified diff（Python 端不解析，原文返回给 agent）.

        优先用 /changes 端点（单次返回元信息 + diffs），简化逻辑.
        """
        try:
            project = self._gl.projects.get(project_id)
            changes = project.mergerequests.get(mr_iid, lazy=True).changes(get_all=True)
        except gitlab.exceptions.GitlabError as e:
            raise GitLabError(f"get_mr_diff failed: {e}") from e

        diffs = changes.get("changes", [])
        # 拼接所有文件的 unified diff（agent 会读懂）
        parts: list[str] = []
        for d in diffs:
            old = d.get("old_path", "")
            new = d.get("new_path", "")
            header = f"diff --git a/{old} b/{new}\n"
            if d.get("new_file"):
                header += "new file mode 100644\n"
            elif d.get("deleted_file"):
                header += "deleted file mode 100644\n"
            elif d.get("renamed_file"):
                header += f"rename from {old}\nrename to {new}\n"
            diff_body = d.get("diff", "")
            parts.append(header + diff_body + "\n")

        full_diff = "".join(parts)
        logger.info("gitlab.get_mr_diff project={} mr={} files={} bytes={}",
                    project_id, mr_iid, len(diffs), len(full_diff))
        return full_diff

    # ---------- 评论 ----------
    def post_mr_comment(self, project_id: int, mr_iid: int, body: str) -> int:
        """发 MR 普通评论（不是行内 DiffNote）；返回 note_id."""
        try:
            project = self._gl.projects.get(project_id)
            mr = project.mergerequests.get(mr_iid)
            note = mr.notes.create({"body": body})
        except gitlab.exceptions.GitlabError as e:
            raise GitLabError(f"post_mr_comment failed: {e}") from e
        logger.info("gitlab.post_comment project={} mr={} note_id={}",
                    project_id, mr_iid, note.id)
        return note.id

    # ---------- 行内评论 (Discussion) ----------
    def get_mr_diff_refs(self, project_id: int, mr_iid: int) -> dict[str, str]:
        """拉 MR 的 diff_refs（base_sha / start_sha / head_sha），用于发布行内评论.

        Inline discussion 的 position 必须带这三个 SHA，缺一不可.
        """
        try:
            project = self._gl.projects.get(project_id)
            mr = project.mergerequests.get(mr_iid)
            refs = mr.diff_refs
        except gitlab.exceptions.GitlabError as e:
            raise GitLabError(f"get_mr_diff_refs failed: {e}") from e
        if not refs:
            raise GitLabError(
                f"empty diff_refs for project={project_id} mr={mr_iid}"
            )
        logger.info("gitlab.get_mr_diff_refs project={} mr={} base={} start={} head={}",
                    project_id, mr_iid, refs.get('base_sha'),
                    refs.get('start_sha'), refs.get('head_sha'))
        return refs

    def post_mr_discussion(
        self,
        project_id: int,
        mr_iid: int,
        body: str,
        *,
        file_path: str,
        new_line: int | None = None,
        old_line: int | None = None,
        side: str = "new",
    ) -> str | None:
        """发 MR 行内评论 (DiscussionNote in python-gitlab).

        Args:
            file_path: 相对仓库根的文件路径
            new_line: 新文件行号（+1 起；指向 diff 中新增/修改行）
            old_line: 旧文件行号（删除行时用；一般在 - 行时设）
            side: 'new' / 'old' / 'both'

        Returns:
            note id (str) on success, None on fallback failure.
        """
        try:
            refs = self.get_mr_diff_refs(project_id, mr_iid)
        except GitLabError as e:
            logger.warning("post_mr_discussion.refs_failed: {}", e)
            return None

        position: dict[str, Any] = {
            "position_type": "text",
            "new_path": file_path,
            "old_path": file_path,
            "base_sha": refs.get("base_sha"),
            "start_sha": refs.get("start_sha"),
            "head_sha": refs.get("head_sha"),
        }
        if new_line is not None:
            position["new_line"] = new_line
        if old_line is not None:
            position["old_line"] = old_line

        try:
            project = self._gl.projects.get(project_id)
            mr = project.mergerequests.get(mr_iid)
            discussion = mr.discussions.create(
                {"body": body, "position": position}
            )
            note_id = (
                discussion.id
                if hasattr(discussion, "id")
                else discussion.attributes.get("id")
            )
            logger.info(
                "gitlab.post_discussion project={} mr={} file={} new={} old={} note={}",
                project_id, mr_iid, file_path, new_line, old_line, note_id,
            )
            return str(note_id) if note_id is not None else None
        except gitlab.exceptions.GitlabError as e:
            logger.warning(
                "gitlab.post_discussion failed project={} mr={} file={} line={}: {}",
                project_id, mr_iid, file_path, new_line, e,
            )
            return None

    # ---------- description ----------
    def update_mr_description(self, project_id: int, mr_iid: int, description: str) -> None:
        """覆盖 MR description（不修改 title，由调用方决定）."""
        try:
            project = self._gl.projects.get(project_id)
            mr = project.mergerequests.get(mr_iid)
            mr.description = description
            mr.save()
        except gitlab.exceptions.GitlabError as e:
            raise GitLabError(f"update_mr_description failed: {e}") from e
        logger.info("gitlab.update_description project={} mr={} bytes={}",
                    project_id, mr_iid, len(description))

    def update_mr_title(self, project_id: int, mr_iid: int, title: str) -> None:
        """修改 MR title（用于 /describe 优化标题）."""
        try:
            project = self._gl.projects.get(project_id)
            mr = project.mergerequests.get(mr_iid)
            mr.title = title
            mr.save()
        except gitlab.exceptions.GitlabError as e:
            raise GitLabError(f"update_mr_title failed: {e}") from e
        logger.info("gitlab.update_title project={} mr={} title={!r}",
                    project_id, mr_iid, title[:50])

    # ---------- MR 列表（周报用） ----------
    def list_project_mrs(
        self,
        project_id: int,
        *,
        state: str = "merged",
        updated_after: str | None = None,
        updated_before: str | None = None,
        per_page: int = 100,
    ) -> list[dict[str, Any]]:
        """列出项目 MR 列表（周报聚合用）.

        Args:
            state: 'opened' | 'closed' | 'merged' | 'all'
            updated_after: ISO 8601 时间字符串（包含）
            updated_before: ISO 8601 时间字符串（不包含）
        """
        params: dict[str, Any] = {"state": state, "per_page": per_page}
        if updated_after:
            params["updated_after"] = updated_after
        if updated_before:
            params["updated_before"] = updated_before

        try:
            project = self._gl.projects.get(project_id)
            mrs = project.mergerequests.list(**params)
        except gitlab.exceptions.GitlabError as e:
            raise GitLabError(f"list_project_mrs failed: {e}") from e

        return [m.asdict() if hasattr(m, "asdict") else dict(m) for m in mrs]

    # ---------- 健康检查 ----------
    def ping(self) -> bool:
        try:
            self._gl.auth()
            return True
        except gitlab.exceptions.GitlabError as e:
            logger.warning("gitlab.ping failed: {}", e)
            return False


# 全局单例
client = GitLabClient()