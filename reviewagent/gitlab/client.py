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
        # project 对象缓存 — 同一 command 执行中多次复用
        self._project_cache: dict[int, Any] = {}

    def _get_project(self, project_id: int):
        """获取 project 对象（带缓存，避免重复 API 调用）."""
        if project_id not in self._project_cache:
            try:
                self._project_cache[project_id] = self._gl.projects.get(project_id)
            except gitlab.exceptions.GitlabError as e:
                raise GitLabError(f"get_project failed: {e}") from e
        return self._project_cache[project_id]

    # ---------- MR 元信息 ----------
    def get_mr(self, project_id: int, mr_iid: int) -> dict[str, Any]:
        """拉取 MR 完整元信息."""
        try:
            project = self._get_project(project_id)
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
            project = self._get_project(project_id)
        except gitlab.exceptions.GitlabError as e:
            raise GitLabError(f"get_project_git_url failed: {e}") from e

        path = getattr(project, "path_with_namespace", None) or str(project_id)
        base = self._gl.url.rstrip("/")
        scheme = "https" if base.startswith("https://") else "http"
        host = base[len(f"{scheme}://"):]
        return f"{scheme}://oauth2:{self._gl.private_token}@{host}/{path}.git"

    def get_mr_web_url(self, project_id: int, mr_iid: int) -> str | None:
        """返回 GitLab MR 的 web URL (浏览器可访问的链接).

        失败时返回 None (调网络异常/项目无权限等), 不抛错 — caller 可选忽略.
        """
        try:
            project = self._get_project(project_id)
        except GitLabError as e:
            logger.debug("gitlab.get_mr_web_url get_project failed: {}", e)
            return None
        path = getattr(project, "path_with_namespace", None) or str(project_id)
        base = self._gl.url.rstrip("/")
        return f"{base}/{path}/-/merge_requests/{mr_iid}"

    # ---------- MR diff ----------
    def get_mr_changes(
        self, project_id: int, mr_iid: int
    ) -> list[dict[str, Any]]:
        """拉取 MR 的变更文件列表（结构化数据，含 old/new path / additions / deletions / diff）.

        优先用 /changes 端点（单次返回元信息 + diffs），简化逻辑.

        默认过滤掉 deleted_file=True（这些 diff 全是 - 行, 对 improve / describe
        都没意义, 还会污染 LLM context 触发 false positive).
        """
        try:
            project = self._get_project(project_id)
            changes = project.mergerequests.get(mr_iid, lazy=True).changes(get_all=True)
        except gitlab.exceptions.GitlabError as e:
            raise GitLabError(f"get_mr_changes failed: {e}") from e

        diffs = changes.get("changes", [])
        all_count = len(diffs)
        filtered = [d for d in diffs if not (isinstance(d, dict) and d.get("deleted_file"))]
        skipped = all_count - len(filtered)
        if skipped:
            logger.info(
                "gitlab.get_mr_changes filtered deleted files project={} mr={} "
                "all={} kept={} skipped={}",
                project_id, mr_iid, all_count, len(filtered), skipped,
            )
        else:
            logger.info("gitlab.get_mr_changes project={} mr={} files={}",
                        project_id, mr_iid, len(filtered))
        return [dict(d) if not isinstance(d, dict) else d for d in filtered]

    def get_mr_diff(self, project_id: int, mr_iid: int) -> str:
        """拉取 MR 的 unified diff（Python 端不解析，原文返回给 agent）."""
        diffs = self.get_mr_changes(project_id, mr_iid)
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
            project = self._get_project(project_id)
            mr = project.mergerequests.get(mr_iid)
            note = mr.notes.create({"body": body})
        except gitlab.exceptions.GitlabError as e:
            raise GitLabError(f"post_mr_comment failed: {e}") from e
        logger.info("gitlab.post_comment project={} mr={} note_id={}",
                    project_id, mr_iid, note.id)
        return note.id

    def update_mr_comment(self, project_id: int, mr_iid: int, note_id: int, body: str) -> bool:
        """修改 MR 评论内容（PUT /notes/:id）。返回是否成功."""
        try:
            project = self._get_project(project_id)
            mr = project.mergerequests.get(mr_iid)
            note = mr.notes.get(note_id)
            note.body = body
            note.save()
        except gitlab.exceptions.GitlabError as e:
            raise GitLabError(f"update_mr_comment failed: {e}") from e
        logger.info("gitlab.update_comment project={} mr={} note_id={}",
                    project_id, mr_iid, note_id)
        return True

    def list_mr_notes(self, project_id: int, mr_iid: int) -> list[dict[str, Any]]:
        """拉 MR 全部普通评论 (notes, 非 DiffNote). 一次性 API 调用, 本地过滤 header.

        Why: 持久评论模式 (pr_agent 风格) 需要按 header 找到上一轮评论,
        然后 update 而不是 post 新评论. 每次只调一次 API, 内部过滤.

        Returns: [{"id": int|str, "body": str}, ...] (按 created_at 升序).
        Raises: GitLabError.
        """
        try:
            project = self._get_project(project_id)
            mr = project.mergerequests.get(mr_iid)
            notes = mr.notes.list(get_all=True)
        except gitlab.exceptions.GitlabError as e:
            raise GitLabError(f"list_mr_notes failed: {e}") from e
        result = [{"id": n.id, "body": n.body} for n in notes]
        logger.info(
            "gitlab.list_notes project={} mr={} count={}",
            project_id, mr_iid, len(result),
        )
        return result

    # ---------- 行内评论 (Discussion) ----------
    def get_mr_diff_refs(self, project_id: int, mr_iid: int) -> dict[str, str]:
        """拉 MR 的 diff_refs（base_sha / start_sha / head_sha），用于发布行内评论.

        Inline discussion 的 position 必须带这三个 SHA，缺一不可.
        """
        try:
            project = self._get_project(project_id)
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
            project = self._get_project(project_id)
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

    # ---------- /adopt & /dismiss 配套方法 ----------
    def resolve_discussion(self, project_id: int, mr_iid: int, discussion_id: str) -> bool:
        """Resolve 一个 discussion (用于 /adopt 和 /dismiss).

        discussion_id 是 GitLab 返回的字符串 ID (mr.discussions.get(id)).
        """
        try:
            project = self._get_project(project_id)
            mr = project.mergerequests.get(mr_iid)
            discussion = mr.discussions.get(discussion_id)
            discussion.resolved = True
            discussion.save()
            logger.info(
                "gitlab.resolve_discussion project={} mr={} discussion={}",
                project_id, mr_iid, discussion_id,
            )
            return True
        except gitlab.exceptions.GitlabError as e:
            logger.warning(
                "gitlab.resolve_discussion failed project={} mr={} discussion={}: {}",
                project_id, mr_iid, discussion_id, e,
            )
            return False

    def reply_to_discussion(
        self,
        project_id: int,
        mr_iid: int,
        discussion_id: str,
        body: str,
    ) -> int | None:
        """在 discussion 下发回复评论 (用于 /adopt 验证失败时的友好提示)."""
        try:
            project = self._get_project(project_id)
            mr = project.mergerequests.get(mr_iid)
            discussion = mr.discussions.get(discussion_id)
            reply = discussion.notes.create({"body": body})
            note_id = reply.id if hasattr(reply, "id") else reply.attributes.get("id")
            logger.info(
                "gitlab.reply_discussion project={} mr={} discussion={} note={}",
                project_id, mr_iid, discussion_id, note_id,
            )
            return int(note_id) if note_id is not None else None
        except gitlab.exceptions.GitlabError as e:
            logger.warning(
                "gitlab.reply_discussion failed project={} mr={} discussion={}: {}",
                project_id, mr_iid, discussion_id, e,
            )
            return None

    def get_discussion_notes(
        self, project_id: int, mr_iid: int, discussion_id: str
    ) -> list[dict[str, Any]]:
        """获取一个 discussion 下所有 notes (用于 /adopt 找原 suggestion)."""
        try:
            project = self._get_project(project_id)
            mr = project.mergerequests.get(mr_iid)
            discussion = mr.discussions.get(discussion_id)
            notes = discussion.attributes.get("notes", []) or []
            return [dict(n) if not isinstance(n, dict) else n for n in notes]
        except gitlab.exceptions.GitlabError as e:
            logger.warning(
                "gitlab.get_discussion_notes failed project={} mr={} discussion={}: {}",
                project_id, mr_iid, discussion_id, e,
            )
            return []

    def is_discussion_resolved(
        self, project_id: int, mr_iid: int, discussion_id: str
    ) -> bool | None:
        """返回 discussion 的 resolved 状态；API 失败时返回 None."""
        try:
            project = self._get_project(project_id)
            mr = project.mergerequests.get(mr_iid)
            discussion = mr.discussions.get(discussion_id)
            notes = discussion.attributes.get("notes", []) or []
            resolvable = [note for note in notes if note.get("resolvable")]
            if not resolvable:
                return False
            return all(bool(note.get("resolved")) for note in resolvable)
        except gitlab.exceptions.GitlabError as e:
            logger.warning(
                "gitlab.is_discussion_resolved failed project={} mr={} discussion={}: {}",
                project_id, mr_iid, discussion_id, e,
            )
            return None

    def list_repository_tree(
        self, project_id: int, path: str, ref: str, *, recursive: bool = True
    ) -> list[dict[str, Any]]:
        """列出某 ref 下指定目录的文件树.

        Args:
            recursive: True 则递归列出所有子目录文件 (默认 True)
        """
        try:
            project = self._get_project(project_id)
            return project.repository_tree(path=path, ref=ref, all=True, recursive=recursive)
        except gitlab.exceptions.GitlabError as e:
            logger.warning(
                "gitlab.list_repository_tree failed project={} path={} ref={}: {}",
                project_id, path, ref, e,
            )
            return []

    def get_file_at_sha(
        self, project_id: int, file_path: str, ref: str
    ) -> str | None:
        """取某 ref 下的文件 raw 内容 (用于 /adopt 验证目标行是否被修改)."""
        from urllib.parse import quote
        try:
            project = self._get_project(project_id)
            f = project.files.get(file_path=file_path, ref=ref)
            # python-gitlab 返回 .content 是 base64
            import base64
            return base64.b64decode(f.content).decode("utf-8", errors="replace")
        except gitlab.exceptions.GitlabError as e:
            logger.warning(
                "gitlab.get_file_at_sha failed project={} file={} ref={}: {}",
                project_id, file_path, ref, e,
            )
            return None
        except Exception as e:
            logger.warning(
                "gitlab.get_file_at_sha decode failed project={} file={} ref={}: {}",
                project_id, file_path, ref, e,
            )
            return None

    # ---------- description ----------
    def update_mr_description(self, project_id: int, mr_iid: int, description: str) -> None:
        """覆盖 MR description（不修改 title，由调用方决定）."""
        try:
            project = self._get_project(project_id)
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
            project = self._get_project(project_id)
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
        source_branch: str | None = None,
        target_branch: str | None = None,
        updated_after: str | None = None,
        updated_before: str | None = None,
        per_page: int = 100,
    ) -> list[dict[str, Any]]:
        """列出项目 MR 列表.

        Args:
            state: 'opened' | 'closed' | 'merged' | 'all'
            source_branch: 按 source branch 过滤（push hook 用）
            target_branch: 按 target branch 过滤（周报用, 只看合并到 main 的）
            updated_after: ISO 8601 时间字符串（包含）
            updated_before: ISO 8601 时间字符串（不包含）
        """
        params: dict[str, Any] = {"state": state, "per_page": per_page}
        if source_branch:
            params["source_branch"] = source_branch
        if target_branch:
            params["target_branch"] = target_branch
        if updated_after:
            params["updated_after"] = updated_after
        if updated_before:
            params["updated_before"] = updated_before

        try:
            project = self._get_project(project_id)
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
