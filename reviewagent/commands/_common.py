"""共用基类 — reviewagent 多命令共享的 workspace + 错误处理 + telemetry 包装.

所有命令 (describe / improve / 后续) 共同的工作流:
    1. emit_run_started
    2. 拉 MR 元信息
    3. prepare_workspace (git worktree + diff 文件)
    4. 调 opencode agent（agent 已在 ~/.config/opencode/agent/<name>.md 注册）
    5. 解析 agent 输出
    6. 通过 _publish 写回 GitLab + telemetry
    7. cleanup_workspace (try/finally)

各命令只需:
    - agent_name           (str)
    - _build_user_prompt   (返回发给 agent 的 user content)
    - _publish             (把 agent dict 应用到 GitLab + emit telemetry)

agent prompt 模板在 reviewagent/prompts/<name>.md（仓库内）+ 同步到
~/.config/opencode/agent/<name>.md（用 scripts/sync_agents.py 同步）。
"""
from __future__ import annotations

import time
from typing import Any, Callable

from reviewagent.config import config
from reviewagent.git.workspace import (
    Workspace,
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
from reviewagent.repo_context import build_repo_context
from reviewagent.telemetry import events
from reviewagent.telemetry.models import MRRecord, ReviewRun


class BaseCommandError(RuntimeError):
    """所有 ReviewAgent 命令共用基类错误."""
    pass


class BaseCommand:
    """共用命令基类.

    子类通过覆盖 _build_user_prompt / _publish 即可完成一个命令.
    """

    # 子类必须设置:
    COMMAND_NAME: str = ""         # 例 "describe" / "improve"
    DEFAULT_AGENT: str = ""        # opencode agent 名 (同 prompts 名)

    def __init__(
        self,
        *,
        project_id: int,
        mr_iid: int,
        triggered_by: str = "webhook",
        actor_username: str = "",
    ):
        if not self.COMMAND_NAME or not self.DEFAULT_AGENT:
            raise ValueError(f"{type(self).__name__} missing COMMAND_NAME / DEFAULT_AGENT")

        self.project_id = project_id
        self.mr_iid = mr_iid
        self.triggered_by = triggered_by
        self.actor_username = actor_username

        self.gitlab = GitLabClient()
        self.prompt_cfg = loader.load(self.COMMAND_NAME)
        self.model = config.opencode_model  # 已配置 minimax/MiniMax-M2.7
        self.repo_context: str = ""  # AGENTS.md 等仓库规则 (run() 中填充)
        self._last_oc_result = None  # 最后一次 opencode 调用结果 (token 统计用)

    # ---------- 子类可覆盖 ----------
    def _build_user_prompt(self) -> str:
        """user message（diff 通过 file part 附加; 这里只发简短 trigger）."""
        return (
            f"请按你的 system prompt 处理当前 MR 的 diff"
            f"（变更内容见上方附件文件）。"
        )

    def _publish(self, agent_result: dict[str, Any]) -> dict[str, Any]:
        """子类把 agent_result 应用到 GitLab + emit telemetry，返回执行摘要.

        应当 raise GitLabError / BaseCommandError 表示失败.
        """
        raise NotImplementedError

    def _should_skip(self, mr_dict: dict) -> dict[str, Any] | None:
        """子类 hook: 在拉 diff 之前决定是否跳过本次 run.

        返回 None → 正常执行；返回 dict → 立即跳过，dict 作为 result_summary.
        默认实现: 不跳过.
        """
        return None

    def _call_agent(self, ws) -> dict[str, Any]:
        """调 opencode agent，返回解析后的 dict. 子类可覆盖实现并行等策略."""
        oc_result = opencode.run(
            agent=self.DEFAULT_AGENT,
            prompt=self._build_user_prompt(),
            workdir=ws.worktree,
            files=[ws.diff_file],
            timeout=config.rq_worker_timeout,
        )
        self._last_oc_result = oc_result
        return oc_result.data

    # ---------- 主流程 ----------
    def run(self) -> dict[str, Any]:
        """主入口: 返回执行结果摘要（不入库）."""
        run = ReviewRun(
            project_id=self.project_id,
            mr_iid=self.mr_iid,
            command=self.COMMAND_NAME,
            triggered_by=self.triggered_by,
            actor_username=self.actor_username,
        )
        run_id = events.emit_run_started(run)
        t0 = time.monotonic()
        ws: Workspace | None = None
        result_summary: dict[str, Any] = {}
        prompt_tokens = 0
        completion_tokens = 0
        model_used = self.model
        _run_finished = False  # finally 安全网: 确保 run 状态一定被标记

        def _mark_finished(**kw):
            nonlocal _run_finished
            events.emit_run_finished(run_id, **kw)
            _run_finished = True

        try:
            # 1. MR 元信息
            mr_dict = self.gitlab.get_mr(self.project_id, self.mr_iid)
            mr = MRRecord.from_gitlab(mr_dict)
            events.emit_mr_activity(mr)

            # 1.1. 加载仓库规则上下文 (AGENTS.md 等)
            try:
                self.repo_context = build_repo_context(self.gitlab, self.project_id)
            except Exception as e:
                logger.warning("{}.repo_context failed (non-fatal): {}", self.COMMAND_NAME, e)
                self.repo_context = ""

            # 1.5. 执行时状态校验 — MR 可能在排队等待期间已 merged/closed
            mr_state = mr_dict.get("state", "")
            if mr_state and mr_state not in ("opened",):
                logger.info(
                    "{}.skip_state project={} mr={} state={}",
                    self.COMMAND_NAME, self.project_id, self.mr_iid, mr_state,
                )
                duration_ms = int((time.monotonic() - t0) * 1000)
                _mark_finished(
                    run_id, status="skipped", model=model_used,
                    prompt_tokens=0, completion_tokens=0,
                    duration_ms=duration_ms,
                )
                return {"status": "skipped", "reason": f"mr_state={mr_state}"}

            # 1.6. 子类 hook: 一次性 / 幂等守卫（describe 的 "只改一次 title" 等场景）
            skip_summary = self._should_skip(mr_dict)
            if skip_summary is not None:
                reason = skip_summary.get("reason", "subclass_skip")
                logger.info(
                    "{}.skip_custom project={} mr={} reason={}",
                    self.COMMAND_NAME, self.project_id, self.mr_iid, reason,
                )
                duration_ms = int((time.monotonic() - t0) * 1000)
                _mark_finished(
                    run_id, status="skipped", model=model_used,
                    prompt_tokens=0, completion_tokens=0,
                    duration_ms=duration_ms,
                )
                skip_summary.setdefault("status", "skipped")
                skip_summary["duration_ms"] = duration_ms
                return skip_summary

            # 2. 拉 diff
            diff_text = self.gitlab.get_mr_diff(self.project_id, self.mr_iid)
            if not diff_text.strip():
                raise BaseCommandError("MR has no diff (empty or binary-only)")

            # 2.5. MR 大小限制 — diff 过大时跳过并评论告知
            if len(diff_text) > config.max_diff_chars:
                logger.info(
                    "{}.skip_large_diff project={} mr={} bytes={} limit={}",
                    self.COMMAND_NAME, self.project_id, self.mr_iid,
                    len(diff_text), config.max_diff_chars,
                )
                try:
                    self.gitlab.post_mr_comment(
                        self.project_id, self.mr_iid,
                        f"> ⚠️ **{self.COMMAND_NAME}** 跳过：MR diff 过大"
                        f"（{len(diff_text)} 字符 > 上限 {config.max_diff_chars}），"
                        f"无法进行有效自动检视。",
                    )
                except GitLabError:
                    pass  # best-effort comment
                duration_ms = int((time.monotonic() - t0) * 1000)
                _mark_finished(
                    run_id, status="skipped", model=model_used,
                    prompt_tokens=0, completion_tokens=0,
                    duration_ms=duration_ms,
                )
                return {
                    "status": "skipped", "reason": "diff_too_large",
                    "diff_chars": len(diff_text), "limit": config.max_diff_chars,
                }

            # 3. 准备 workspace
            git_url = self.gitlab.get_project_git_url(self.project_id)
            ws = prepare_workspace(
                project_id=self.project_id,
                mr_iid=self.mr_iid,
                source_sha=mr_dict["sha"],
                diff_text=diff_text,
                git_url=git_url,
                tag=self.COMMAND_NAME,
            )
            self.ws = ws  # 让 _publish 等子类方法能拿到 worktree 路径

            # 3.7. 执行前二次校验 — MR 可能在排队期间已 merged/closed
            fresh_mr = self.gitlab.get_mr(self.project_id, self.mr_iid)
            fresh_state = fresh_mr.get("state", "")
            if fresh_state and fresh_state not in ("opened",):
                logger.info(
                    "{}.skip_state_late project={} mr={} state={}",
                    self.COMMAND_NAME, self.project_id, self.mr_iid, fresh_state,
                )
                duration_ms = int((time.monotonic() - t0) * 1000)
                _mark_finished(
                    run_id, status="skipped", model=model_used,
                    prompt_tokens=0, completion_tokens=0,
                    duration_ms=duration_ms,
                )
                return {"status": "skipped", "reason": f"mr_state_late={fresh_state}"}

            # 4. 调 opencode agent（子类可覆盖 _call_agent 实现并行等策略）
            agent_result = self._call_agent(ws)
            prompt_tokens = self._last_oc_result.prompt_tokens if self._last_oc_result else 0
            completion_tokens = self._last_oc_result.completion_tokens if self._last_oc_result else 0
            model_used = (self._last_oc_result.model if self._last_oc_result else "") or self.model

            if isinstance(agent_result, dict):
                _preview = str(agent_result)
                if len(_preview) > 4000:
                    _preview = _preview[:4000] + "...(truncated)"
                logger.info(
                    "{}.agent_raw project={} mr={} keys={} preview={}",
                    self.COMMAND_NAME, self.project_id, self.mr_iid,
                    list(agent_result.keys()), _preview,
                )
            else:
                logger.info(
                    "{}.agent_raw project={} mr={} type={} preview={!r}",
                    self.COMMAND_NAME, self.project_id, self.mr_iid,
                    type(agent_result).__name__, str(agent_result)[:300],
                )

            # 5. 校验（必须 dict）
            if not isinstance(agent_result, dict):
                raise OpencodeOutputError(
                    f"agent output not dict: {type(agent_result).__name__}"
                )

            # 6. 子类落库逻辑
            result_summary = self._publish(agent_result) or {}

            # 7. 标记成功
            duration_ms = int((time.monotonic() - t0) * 1000)
            _mark_finished(
                run_id, status="success", model=model_used,
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                duration_ms=duration_ms,
            )
            result_summary.setdefault("status", "success")
            result_summary["duration_ms"] = duration_ms
            logger.info(
                "{}.ok project={} mr={} summary={}",
                self.COMMAND_NAME, self.project_id, self.mr_iid, result_summary,
            )
            return result_summary

        except (OpencodeTimeoutError, OpencodeOutputError, OpencodeError) as e:
            duration_ms = int((time.monotonic() - t0) * 1000)
            _mark_finished(
                run_id, status="failed", error=f"opencode: {e}",
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                duration_ms=duration_ms,
            )
            raise BaseCommandError(f"opencode error: {e}") from e
        except (WorkspaceError, GitLabError) as e:
            duration_ms = int((time.monotonic() - t0) * 1000)
            _mark_finished(
                run_id, status="failed", error=f"infra: {e}",
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                duration_ms=duration_ms,
            )
            raise BaseCommandError(f"infra error: {e}") from e
        except Exception as e:
            duration_ms = int((time.monotonic() - t0) * 1000)
            _mark_finished(
                run_id, status="failed", error=f"unexpected: {e}",
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                duration_ms=duration_ms,
            )
            raise
        finally:
            # 安全网: 进程被 kill / OOM / 未捕获异常 等场景下,
            # 确保 review_run 不会永远卡在 "running"
            if not _run_finished:
                duration_ms = int((time.monotonic() - t0) * 1000)
                try:
                    events.emit_run_finished(
                        run_id, status="failed",
                        error="process terminated unexpectedly",
                        duration_ms=duration_ms,
                    )
                except Exception:
                    pass
            if ws is not None:
                try:
                    cleanup_workspace(ws)
                except Exception as e:
                    logger.warning(
                        "{}.cleanup failed (non-fatal): {}",
                        self.COMMAND_NAME, e,
                    )


# Compatibility alias used by describe.py (legacy code path).
CommandError = BaseCommandError
