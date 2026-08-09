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
from reviewagent.llm import (
    OpencodeError,
    OpencodeOutputError,
    OpencodeTimeoutError,
    QoderCLIError,
    QoderCLIOutputError,
    QoderCLITimeoutError,
    get_client,
)
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
        self.model = (
            config.qodercli_model
            if config.llm_provider == "qodercli"
            else config.opencode_model
        )
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
        client = get_client()
        oc_result = client.run(
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

        def _mark_finished(_run_id, **kw):
            """统一收口 emit_run_finished 调用,带 finished flag 防止 finally 重复触发.

            Args:
                _run_id: 当前 review_run 的 id (运行时一直是同一个 run_id, 这里保留
                    作为位置参数以保持调用点 `_mark_finished(run_id, ...)` 不变).
                **kw: 其余字段 (status, error, prompt_tokens, ...) 透传给 events.
            """
            nonlocal _run_finished
            events.emit_run_finished(_run_id, **kw)
            _run_finished = True
        # 在 try 块前初始化 provider_name, except 块 + finally 都能访问
        provider_name = getattr(get_client(), "provider_name", "unknown")

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

        # LLM 适配层异常族 — opencode + qodercli 共 6 个具体类;
        # 没有公共基类(避免动 reviewagent.opencode.client 的类层级),这里显式列出.
        except (
            OpencodeTimeoutError, OpencodeOutputError, OpencodeError,
            QoderCLITimeoutError, QoderCLIOutputError, QoderCLIError,
        ) as e:
            duration_ms = int((time.monotonic() - t0) * 1000)
            _mark_finished(
                run_id, status="failed", error=f"{provider_name}: {e}",
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                duration_ms=duration_ms,
            )
            raise BaseCommandError(f"{provider_name} error: {e}") from e
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




# ---------- 检视汇总 (顶部 MR 持久评论) 共享刷新 ----------
#
# Why: 之前 improve.py 把"生成 + 找/创/更新"同一评论的逻辑写成方法,
#      /adopt /dismiss / UI Apply 不走 improve pipeline, 顶部汇总就停留在
#      上一次 improve run 的快照, 用户看到的是 stale 数据 (MR 247 实测偏差).
# 修复: 把这段抽成模块级 helper, improve.py 与 suggestion_actions.py 都调,
#      每次状态变化后立即刷新 (秒级 vs 之前要等下一次 push).

_OVERVIEW_HEADER_DEFAULT = "## 检视汇总"


def build_overview_body(
    *,
    project_id: int,
    mr_iid: int,
    inline_posted_count: int = 0,
    head_sha: str = "",
) -> str:
    """生成 MR 顶部"检视汇总"固定表格 markdown.

    设计 (方案 A - 单表合并):
    - Header 固定: `## 检视汇总` (pr_agent 风格, 不带版本号)
    - 单表 5 列: 严重度 × {待处理 / 已采纳 / 已忽略 / 已关闭 / 合计}
    - 末行 加粗"总计"行
    - 底部: 状态说明 + "🆕 最后新增 N 条" + 时间戳 / HEAD

    Args:
        project_id: GitLab project id
        mr_iid: MR iid
        inline_posted_count: 本轮新增的 suggestion 数 (用于"最后新增 N 条"行)
        head_sha: MR head_sha 短码 (用于底部 HEAD 行); 空则不显示 HEAD
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    # 严重度 × 状态聚合
    sev_buckets: dict[str, dict[str, int]] = {
        "high": {"open": 0, "applied": 0, "dismissed": 0, "resolved": 0},
        "medium": {"open": 0, "applied": 0, "dismissed": 0, "resolved": 0},
        "low": {"open": 0, "applied": 0, "dismissed": 0, "resolved": 0},
    }
    superseded_n = 0
    try:
        from reviewagent.telemetry.store import get_store
        store = get_store()
        # Batch2: 用 cohort_key 归并同问题, 只取最新一条非 superseded 记录参与统计.
        # 旧版直接 list_suggestions 累加所有 records → MR 249 出现 22 条 applied 但
        # 实际只有 ~13 个独立问题. 修复后: 同 cohort 只占 1 个状态.
        all_sugs = store.list_latest_by_cohort(
            project_id=project_id, mr_iid=mr_iid,
        )
        for s in all_sugs:
            sev = (s.get("severity") or "medium").lower()
            state = (s.get("state") or "open").lower()
            if sev not in sev_buckets:
                sev_buckets[sev] = {"open": 0, "applied": 0, "dismissed": 0, "resolved": 0}
            if state not in sev_buckets[sev]:
                sev_buckets[sev][state] = 0
            sev_buckets[sev][state] += 1
        try:
            # 同时统计显式 superseded + 被 cohort 归并隐藏的 (同一问题被多轮发布)
            superseded_n = (
                store.count_superseded_in_mr(project_id=project_id, mr_iid=mr_iid)
                + store.count_hidden_by_cohort(project_id=project_id, mr_iid=mr_iid)
            )
        except Exception:  # noqa: BLE001
            superseded_n = 0
    except Exception as e:
        logger.warning("build_overview_body.query failed (non-fatal): {}", e)

    rows: list[dict[str, int | str]] = []
    total_open = total_applied = total_dismissed = total_resolved = 0
    for sev in ("high", "medium", "low"):
        emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}[sev]
        label = {"high": "HIGH", "medium": "MEDIUM", "low": "LOW"}[sev]
        bucket = sev_buckets.get(
            sev, {"open": 0, "applied": 0, "dismissed": 0, "resolved": 0},
        )
        open_n = bucket["open"]
        applied_n = bucket["applied"]
        dismissed_n = bucket["dismissed"]
        resolved_n = bucket["resolved"]
        total_open += open_n
        total_applied += applied_n
        total_dismissed += dismissed_n
        total_resolved += resolved_n
        rows.append({
            "label": f"{emoji} {label}",
            "open": open_n, "applied": applied_n,
            "dismissed": dismissed_n, "resolved": resolved_n,
            "sum": open_n + applied_n + dismissed_n + resolved_n,
        })
    grand_total = total_open + total_applied + total_dismissed + total_resolved
    adoption_rate = round(total_applied / grand_total * 100, 1) if grand_total else 0.0
    head_short = (head_sha or "")[:7] if head_sha else ""

    lines: list[str] = []
    lines.append(f"## 检视汇总（总建议数 {grand_total}，采纳率 {adoption_rate}%）")
    lines.append("")
    lines.append("| 严重度 | ⏳ 待处理 | ✅ 已采纳 | ❌ 已忽略 | 🔒 已关闭（未分类） | 合计 |")
    lines.append("|:---:|:---:|:---:|:---:|:---:|:---:|")
    for row in rows:
        lines.append(
            f"| {row['label']} | {row['open']} | {row['applied']} | {row['dismissed']} | {row['resolved']} | {row['sum']} |"
        )
    lines.append(
        f"| **总计** | **{total_open}** | **{total_applied}** | **{total_dismissed}** | **{total_resolved}** | **{grand_total}** |"
    )
    lines.append("")
    try:
        ts = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S CST")
    except Exception:
        ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    meta_parts: list[str] = [f"⏱ {ts}"]
    if head_short:
        meta_parts.append(f"HEAD {head_short}")
    meta_suffix = " · " + " · ".join(meta_parts) if meta_parts else ""
    lines.append("> ✅ **已采纳**：建议代码已通过 GitLab 应用建议、手动修改或 `/adopt` 确认采纳。")
    lines.append("")
    lines.append("> ❌ **已忽略**：用户通过 `/dismiss` 明确关闭了建议，并记录忽略理由（如有）。")
    lines.append("")
    lines.append("> 🔒 **已关闭（未分类）**：用户在 GitLab 中直接解决了主题，但系统无法确认该建议是采纳还是忽略。")
    lines.append("")
    if superseded_n:
        lines.append(f"> ♻️ 同问题被多轮重复发布，{superseded_n} 条已被合并到最新版本。")
        lines.append("")
    lines.append(f"🆕 **最后新增 {inline_posted_count} 条**{meta_suffix}")
    lines.append("")
    return "\n".join(lines)


def publish_overview(
    *,
    project_id: int,
    mr_iid: int,
    inline_posted_count: int = 0,
    head_sha: str = "",
    run_late_detect: bool = True,
    gitlab: GitLabClient | None = None,
    header: str = _OVERVIEW_HEADER_DEFAULT,
) -> int | str | None:
    """Build + publish (find/create/update) the persistent MR overview comment.

    一站式入口: 调用一次就完成 build + find/create/update. idempotent.

    Args:
        project_id: GitLab project id
        mr_iid: MR iid
        inline_posted_count: 本轮新增的 suggestion 数
        head_sha: MR head_sha (空字符串则不显示 HEAD 行)
        run_late_detect: 是否先跑一次 late_detect (把"已关闭(未分类)"翻"已采纳").
            improve.py 主流程传 True (build_overview 之前调一次保证数据最新);
            /adopt /dismiss / ui_apply 路径传 False (避免无谓网络开销, 它们的
            状态变化不会触发 late_detect 的命中).
        gitlab: GitLabClient 实例 (None 则新建)
        header: 锚点 header, 默认 "## 检视汇总"

    Returns: note_id (int|str) 或 None (失败 / 跳过).
    """
    if gitlab is None:
        gitlab = GitLabClient()

    # 1. 可选: late detect (把误分类的 resolved+gitlab_resolve 翻回 applied)
    if run_late_detect and head_sha:
        try:
            from reviewagent.commands.suggestion_actions import auto_detect_applied
            auto_detect_applied(
                project_id=project_id, mr_iid=mr_iid,
                head_sha=head_sha, actor_username="telemetry-sync",
            )
        except Exception as e:
            logger.warning("publish_overview.late_detect failed (non-fatal): {}", e)

    # 2. Build markdown
    body = build_overview_body(
        project_id=project_id, mr_iid=mr_iid,
        inline_posted_count=inline_posted_count, head_sha=head_sha,
    )

    # 3. Find existing comment by header prefix → update; else post new
    try:
        notes = gitlab.list_mr_notes(project_id, mr_iid)
    except Exception as e:
        logger.warning("publish_overview.list_notes_failed (non-fatal): {}", e)
        notes = []

    for n in notes:
        body_n = n.get("body") or ""
        if body_n.startswith(header):
            try:
                gitlab.update_mr_comment(project_id, mr_iid, n["id"], body)
                logger.info(
                    "publish_overview.updated project={} mr={} note_id={}",
                    project_id, mr_iid, str(n["id"])[:12],
                )
                return n["id"]
            except Exception as e:
                logger.warning(
                    "publish_overview.update_failed (non-fatal) note_id={} err={}",
                    str(n["id"])[:12], e,
                )
                return None

    try:
        note_id = gitlab.post_mr_comment(project_id, mr_iid, body)
        logger.info(
            "publish_overview.created project={} mr={} note_id={}",
            project_id, mr_iid, str(note_id)[:12],
        )
        return note_id
    except Exception as e:
        logger.warning("publish_overview.create_failed (non-fatal): {}", e)
        return None


# Compatibility alias used by describe.py (legacy code path).
CommandError = BaseCommandError
