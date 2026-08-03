"""/describe 命令端到端.

工作流（在 BaseCommand 基础上特化）:
    1. 调 opencode agent `describe`
    2. 解析 agent 返回 {title, description_md}
    3. 强制将首行归一为 "## Description"
    4. update_mr_title + update_mr_description
    5. emit_description_generated
"""
from __future__ import annotations

import re
from typing import Any

from reviewagent.commands._common import BaseCommand, BaseCommandError
from reviewagent.gitlab.client import GitLabError
from reviewagent.llm import OpencodeOutputError
from reviewagent.telemetry import events


# Backward-compat re-exports
DescribeError = BaseCommandError


class DescribeCommand(BaseCommand):
    COMMAND_NAME = "describe"
    DEFAULT_AGENT = "describe"

    def _should_skip(self, mr_dict: dict) -> dict[str, Any] | None:
        """一次性守卫: 该 MR 已经生成过 title+description 后不再覆盖.

        用户需求: 仅在第一次 MR 提交时改一次标题/描述，后续 push / MR update 都不再改,
        避免 GitLab 上产生多个 system note ('changed title from X to Y').
        """
        from reviewagent.telemetry.store import get_store
        mr_row = get_store().get_mr(self.project_id, self.mr_iid)
        if mr_row and mr_row.get("description_generated"):
            return {
                "reason": "already_described",
                "title_bytes": 0,
                "description_bytes": 0,
            }
        return None

    def _publish(self, agent_result: dict[str, Any]) -> dict[str, Any]:
        new_title = (agent_result.get("title") or "").strip()
        new_desc_raw = (agent_result.get("description_md") or "").strip()
        if not new_desc_raw:
            raise OpencodeOutputError("agent output missing 'description_md' (str)")

        new_desc = self._normalize_description(new_desc_raw)

        # 落库
        try:
            if new_title:
                self.gitlab.update_mr_title(self.project_id, self.mr_iid, new_title)
            if new_desc:
                self.gitlab.update_mr_description(
                    self.project_id, self.mr_iid, new_desc
                )
        except GitLabError as e:
            raise BaseCommandError(f"gitlab update failed: {e}") from e

        events.emit_description_generated(self.project_id, self.mr_iid)
        return {
            "title_bytes": len(new_title),
            "description_bytes": len(new_desc),
        }

    @staticmethod
    def _normalize_description(raw: str) -> str:
        """把常见 description 标题变体归一到字面 `## 变更概览`.

        触发场景: MiniMax-M2.7 model 经常输出 `### **变更概览**` / `## 📝 变更概览`,
        而 pr-agent 风格统一是 `## 变更概览` (两个 #，不加粗).
        """
        if not raw:
            return raw
        lines = raw.split("\n")
        for i in range(min(3, len(lines))):
            stripped = lines[i].strip()
            if re.fullmatch(r"#+\s*\**\s*(变更概览|Description|Change[ ]?[Oo]verview|Summary)\s*\**", stripped):
                lines[i] = "## 变更概览"
                break
        return "\n".join(lines)
