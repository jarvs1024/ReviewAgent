"""/describe 命令端到端.

工作流:
    1. 拉 MR 元信息 → emit_mr_activity
    2. 拉 MR diff
    3. prepare_workspace (git worktree + diff 文件)
    4. 调 opencode agent pr-describer → 解析 JSON
    5. 更新 GitLab MR title + description
    6. emit_description_generated + finish_run
    7. cleanup_workspace

错误处理: 任何步骤失败都会:
    - emit_run_finished(status=failed, error=...)
    - re-raise（让 RQ 标记任务失败）
    - 但 workspace 清理会通过 try/finally 保证
"""
from __future__ import annotations

import time
from typing import Any

from reviewagent.config import config
from reviewagent.git.workspace import (
    WorkspaceError,
    cleanup_workspace,
    prepare_workspace,
)
from reviewagent.gitlab.client import GitLabError, GitLabClient
from reviewagent.logging_setup import logger
from reviewagent.opencode.client import (
    OpencodeError,
    OpencodeOutputError,
    OpencodeTimeoutError,
    client as opencode,
)
from reviewagent.prompts import loader
from reviewagent.telemetry import events
from reviewagent.telemetry.models import MRRecord, ReviewRun


class DescribeError(RuntimeError):
    pass


class DescribeCommand:
    def __init__(
        self,
        *,
        project_id: int,
        mr_iid: int,
        triggered_by: str = "webhook",
        actor_username: str = "",
    ):
        self.project_id = project_id
        self.mr_iid = mr_iid
        self.triggered_by = triggered_by
        self.actor_username = actor_username

        self.gitlab = GitLabClient()
        self.prompt_cfg = loader.load("describe")

    def run(self) -> dict[str, Any]:
        """主入口: 返回执行结果摘要（不入库）."""
        run = ReviewRun(
            project_id=self.project_id,
            mr_iid=self.mr_iid,
            command="describe",
            triggered_by=self.triggered_by,
            actor_username=self.actor_username,
        )
        run_id = events.emit_run_started(run)
        t0 = time.monotonic()
        ws = None
        model: str | None = None

        try:
            # 1. MR 元信息
            mr_dict = self.gitlab.get_mr(self.project_id, self.mr_iid)
            mr = MRRecord.from_gitlab(mr_dict)
            events.emit_mr_activity(mr)

            # 2. 拉 diff
            diff_text = self.gitlab.get_mr_diff(self.project_id, self.mr_iid)
            if not diff_text.strip():
                raise DescribeError("MR has no diff (empty or binary-only)")

            # 3. 准备 workspace
            git_url = self.gitlab.get_project_git_url(self.project_id)
            ws = prepare_workspace(
                project_id=self.project_id,
                mr_iid=self.mr_iid,
                source_sha=mr_dict["sha"],
                diff_text=diff_text,
                git_url=git_url,
            )

            # 4. 调 opencode agent
            result = opencode.run(
                agent=self.prompt_cfg["name"],
                prompt=self._build_user_prompt(),
                workdir=ws.worktree,
                files=[ws.diff_file],
                timeout=config.rq_worker_timeout,
            )

            # 5. 校验输出 schema
            self._validate(result)

            # 5.5 规范化 description（强制 "## Description" 标题格式）
            new_desc = self._normalize_description(result.get("description_md", "").strip())

            # 6. 写回 GitLab
            new_title = result.get("title", "").strip() or mr.title
            self.gitlab.update_mr_title(self.project_id, self.mr_iid, new_title)
            if new_desc:
                self.gitlab.update_mr_description(self.project_id, self.mr_iid, new_desc)

            # 7. 标记
            events.emit_description_generated(self.project_id, self.mr_iid)
            duration_ms = int((time.monotonic() - t0) * 1000)
            events.emit_run_finished(
                run_id, status="success", model=model,
                prompt_tokens=0, completion_tokens=0,
                duration_ms=duration_ms,
            )
            logger.info("describe.ok project={} mr={} bytes={}",
                        self.project_id, self.mr_iid, len(new_desc))
            return {
                "status": "success",
                "title": new_title,
                "description_bytes": len(new_desc),
                "duration_ms": duration_ms,
            }

        except (OpencodeTimeoutError, OpencodeOutputError, OpencodeError) as e:
            duration_ms = int((time.monotonic() - t0) * 1000)
            events.emit_run_finished(
                run_id, status="failed", error=str(e),
                prompt_tokens=0, completion_tokens=0,
                duration_ms=duration_ms,
            )
            raise DescribeError(f"opencode error: {e}") from e
        except (WorkspaceError, GitLabError) as e:
            duration_ms = int((time.monotonic() - t0) * 1000)
            events.emit_run_finished(
                run_id, status="failed", error=str(e),
                prompt_tokens=0, completion_tokens=0,
                duration_ms=duration_ms,
            )
            raise DescribeError(f"infra error: {e}") from e
        except Exception as e:
            duration_ms = int((time.monotonic() - t0) * 1000)
            events.emit_run_finished(
                run_id, status="failed", error=f"unexpected: {e}",
                prompt_tokens=0, completion_tokens=0,
                duration_ms=duration_ms,
            )
            raise
        finally:
            if ws is not None:
                try:
                    cleanup_workspace(ws)
                except Exception as e:
                    logger.warning("describe.cleanup failed (non-fatal): {}", e)

    @staticmethod
    def _build_user_prompt() -> str:
        """构造发给 opencode agent 的 user message.

        agent 的 system prompt 已部署到 opencode config
        (~/.config/opencode/agent/<name>.md)，这里只发 diff 文件引用 + 简短 trigger。
        """
        return "请按你的 system prompt 描述当前 MR（diff 内容见上方附件文件）。"

    @staticmethod
    def _validate(result: dict[str, Any]) -> None:
        """校验 agent 返回值符合 schema."""
        if not isinstance(result, dict):
            raise OpencodeOutputError(f"agent output not dict: {type(result).__name__}")
        if "title" not in result or not isinstance(result["title"], str):
            raise OpencodeOutputError("agent output missing 'title' (str)")
        if "description_md" not in result or not isinstance(result["description_md"], str):
            raise OpencodeOutputError("agent output missing 'description_md' (str)")

    @staticmethod
    def _normalize_description(raw: str) -> str:
        """规范化 description — 强制首行 "## Description" (两个 #，不加粗).

        模型 (例如 MiniMax-M2.7) 经常输出 `### **Description**`；
        这里去掉 bold、转 ### → ##，但保留 bullet 内容 / 末尾分隔线。
        """
        import re
        if not raw:
            return raw
        lines = raw.split("\n")
        for i in range(min(3, len(lines))):
            stripped = lines[i].strip()
            # 匹配: ### **Description** / ## Description / # Description 等所有变体
            if re.fullmatch(r"#+\s*\**\s*Description\s*\**", stripped):
                lines[i] = "## Description"
                break
        return "\n".join(lines)
