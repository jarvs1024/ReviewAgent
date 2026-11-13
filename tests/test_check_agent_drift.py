"""部署态 agent 提示词漂移守卫（N8 回归）。

Why:
`profiles/<profile>/prompts/improve.md` 改动后若没重跑 `sync_qoder_agents.py`，
部署态 `.qoder/agents/improve.md` 仍是旧版 → prompt 改动静默不生效。
历史 N8: N7 的「rationale 首句以规则键开头」只改了 profile 侧，真实检视里不生效。

测试只跑纯函数 `check_drift`（传入 tmp 目录），**不依赖谁在跑 / 在什么 shell 里跑**，
也不读写用户 HOME 下的真实部署态。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts.ops.check_agent_drift import check_drift  # noqa: E402
from scripts.sync_qoder_agents import expected_agent_text, sync_qoder_agents  # noqa: E402

_FM = (
    "---\n"
    "name: {name}\n"
    "description: {desc}\n"
    "tools: {{write: false, edit: false}}\n"
    "---\n\n"
    "{body}\n"
)


def _prompt(tmp_path: Path, name: str, body: str = "内容") -> Path:
    d = tmp_path / "prompts"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.md"
    p.write_text(_FM.format(name=name, desc=f"{name} 描述", body=body), encoding="utf-8")
    return p


def test_consistent_layout_has_no_drift(tmp_path: Path) -> None:
    prompts = tmp_path / "prompts"
    agents = tmp_path / "agents"
    _prompt(tmp_path, "improve")
    sync_qoder_agents(prompts, agents)

    errors, warnings = check_drift(prompts, agents)
    assert errors == []
    assert warnings == []


def test_missing_agent_file_is_an_error(tmp_path: Path) -> None:
    prompts = tmp_path / "prompts"
    agents = tmp_path / "agents"
    _prompt(tmp_path, "improve")
    agents.mkdir(parents=True, exist_ok=True)

    errors, _ = check_drift(prompts, agents)
    assert len(errors) == 1
    assert "缺失" in errors[0]


def test_stale_content_is_detected_even_when_mtime_is_newer(tmp_path: Path) -> None:
    """部署态文件 mtime 比源新（mtime 增量同步会跳过）时，内容漂移仍须被报出。"""
    prompts = tmp_path / "prompts"
    agents = tmp_path / "agents"
    src = _prompt(tmp_path, "improve", body="新内容")
    sync_qoder_agents(prompts, agents)

    dst = agents / "improve.md"
    dst.write_text("旧内容\n", encoding="utf-8")
    # 让部署态 mtime 明显新于源 —— 模拟 mtime 同步被跳过
    import os
    import time

    future = time.time() + 3600
    os.utime(dst, (future, future))
    os.utime(src, (future - 7200, future - 7200))

    errors, _ = check_drift(prompts, agents)
    assert len(errors) == 1
    assert "内容漂移" in errors[0]


def test_extra_agent_file_is_warning_only(tmp_path: Path) -> None:
    prompts = tmp_path / "prompts"
    agents = tmp_path / "agents"
    _prompt(tmp_path, "improve")
    sync_qoder_agents(prompts, agents)
    (agents / "improve_agent.md").write_text("历史遗留\n", encoding="utf-8")

    errors, warnings = check_drift(prompts, agents)
    assert errors == []
    assert len(warnings) == 1
    assert "多余" in warnings[0]


def test_private_underscore_prompt_is_ignored(tmp_path: Path) -> None:
    prompts = tmp_path / "prompts"
    agents = tmp_path / "agents"
    _prompt(tmp_path, "improve")
    _prompt(tmp_path, "_private")
    sync_qoder_agents(prompts, agents)

    errors, warnings = check_drift(prompts, agents)
    assert errors == []
    assert warnings == []
    assert not (agents / "_private.md").exists()


def test_expected_agent_text_matches_sync_output(tmp_path: Path) -> None:
    """单一事实源：检查用的渲染结果必须与写盘用的完全一致。"""
    prompts = tmp_path / "prompts"
    agents = tmp_path / "agents"
    src = _prompt(tmp_path, "improve")
    sync_qoder_agents(prompts, agents)

    assert (agents / "improve.md").read_text(encoding="utf-8") == expected_agent_text(src)
