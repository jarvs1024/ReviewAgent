"""D2 回归: 检视汇总 note 的 HEAD 行不能在状态迁移后消失.

背景 (MR !9 真实场景实测):
    publish_overview 的 6 处状态迁移调用点 (/dismiss /adopt /ui_apply /
    resolve /auto_detect /reconciler) 都不传 head_sha, 而
    build_overview_body 的 head_sha 默认 "" → 汇总表底部 "HEAD <短码>"
    被整行抹掉. 实测 p31 MR !9: dismiss 前 "HEAD e866ffc", dismiss 后消失;
    存量 52 个有汇总 note 的 MR 里 19 个已中招 (36.5%).

修复位置: publish_overview 内部补全 head_sha (而不是逐个改 6 个调用点),
    一处覆盖全部状态迁移路径 + scripts/ops/refresh_overviews.py 运维脚本.

不变量:
    - head_sha 显式传入时, 不额外调 GitLab (improve 主流程零新增成本)
    - head_sha 缺失时, 自动取当前 MR head (diff_refs.head_sha)
    - 取不到 head 时降级 (无 HEAD 行), 不抛异常 — 非致命
    - 自动补全的 head **不得** 触发 late_detect (那是 improve 路径的语义)
"""
from __future__ import annotations

import os
import pathlib
import tempfile
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def tmp_telemetry(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    from reviewagent.config import config as _cfg
    monkeypatch.setattr(
        type(_cfg), "sqlite_path",
        property(lambda self: pathlib.Path(path)),
        raising=False,
    )
    from reviewagent.telemetry import store as st
    st._store = None
    yield path
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    st._store = None


def _gl_with_head(head_sha: str = "949f9c8a2efc0faf3645cd8c3c5526821476ffad"):
    gl = MagicMock()
    gl.list_mr_notes.return_value = []
    gl.get_mr_diff_refs.return_value = {"head_sha": head_sha, "start_sha": "base123"}
    posted: dict = {}

    def capture_post(project_id, mr_iid, body):
        posted["body"] = body
        return 99999

    gl.post_mr_comment.side_effect = capture_post
    gl.posted = posted
    return gl


def _publish(gl, **kwargs):
    from reviewagent.commands._common import publish_overview

    with patch("reviewagent.commands.suggestion_actions.GitLabClient", return_value=gl), \
         patch("reviewagent.commands._common.GitLabClient", return_value=gl), \
         patch("reviewagent.commands.suggestion_actions.get_store"):
        return publish_overview(
            project_id=34, mr_iid=263, run_late_detect=False, **kwargs
        )


# ---------- D2.1 head_sha 缺失时自动补全 ----------

def test_publish_overview_fills_head_sha_when_missing(tmp_telemetry):
    """状态迁移路径 (不传 head_sha) 也必须写出 HEAD 行."""
    gl = _gl_with_head("949f9c8a2efc0faf3645cd8c3c5526821476ffad")

    _publish(gl, inline_posted_count=0)

    body = gl.posted["body"]
    assert "HEAD 949f9c8" in body, f"汇总表缺 HEAD 行: {body!r}"
    gl.get_mr_diff_refs.assert_called_once_with(34, 263)


def test_publish_overview_keeps_explicit_head_sha(tmp_telemetry):
    """显式传入 head_sha 时不额外调 GitLab (improve 主流程零新增成本)."""
    gl = _gl_with_head("ffffffffffffffffffffffffffffffffffffffff")

    _publish(gl, inline_posted_count=3, head_sha="e866ffcb0d8c5b5a4e0a3a2b1c0d9e8f7a6b5c4")

    body = gl.posted["body"]
    assert "HEAD e866ffc" in body
    gl.get_mr_diff_refs.assert_not_called()


def test_publish_overview_head_sha_fetch_failure_is_non_fatal(tmp_telemetry):
    """取 head 失败要降级 (无 HEAD 行), 不能把整次汇总刷新拖挂."""
    gl = _gl_with_head()
    gl.get_mr_diff_refs.side_effect = RuntimeError("network down")

    result = _publish(gl, inline_posted_count=0)

    assert result == 99999, "head 取不到不应导致汇总发布失败"
    body = gl.posted["body"]
    assert "检视汇总" in body
    assert "HEAD " not in body


def test_publish_overview_head_sha_fallback_to_start_sha(tmp_telemetry):
    """diff_refs 无 head_sha 时退化用 start_sha (与 improve._get_mr_head_sha 同源)."""
    gl = _gl_with_head()
    gl.get_mr_diff_refs.return_value = {"start_sha": "aaaa111"}

    _publish(gl, inline_posted_count=0)

    assert "HEAD aaaa111" in gl.posted["body"]


# ---------- D2.2 自动补全不得意外触发 late_detect ----------

def test_auto_filled_head_does_not_trigger_late_detect(tmp_telemetry):
    """head_sha 是"补"出来的, 不能顺带把 late_detect 拉起来.

    Why: late_detect 会把 resolution_source='gitlab_resolve' 的 resolved
    翻回 applied —— 状态迁移路径显式传 run_late_detect=False 正是为了避免
    这次网络往返. 若自动补 head 顺便触发, 等于绕过了调用方的显式意图.
    """
    gl = _gl_with_head()

    with patch("reviewagent.commands.suggestion_actions.auto_detect_applied") as mock_late:
        from reviewagent.commands._common import publish_overview

        with patch("reviewagent.commands.suggestion_actions.GitLabClient", return_value=gl), \
             patch("reviewagent.commands._common.GitLabClient", return_value=gl), \
             patch("reviewagent.commands.suggestion_actions.get_store"):
            publish_overview(
                project_id=34, mr_iid=263,
                inline_posted_count=0,
                head_sha="",          # 靠自动补
                run_late_detect=True,  # 调用方说 True, 但 head 原本是空
            )

    mock_late.assert_not_called()


def test_explicit_head_still_runs_late_detect(tmp_telemetry):
    """回归: 显式传 head_sha + run_late_detect=True 时 late_detect 仍要跑."""
    gl = _gl_with_head()

    with patch("reviewagent.commands.suggestion_actions.auto_detect_applied") as mock_late:
        from reviewagent.commands._common import publish_overview

        with patch("reviewagent.commands.suggestion_actions.GitLabClient", return_value=gl), \
             patch("reviewagent.commands._common.GitLabClient", return_value=gl), \
             patch("reviewagent.commands.suggestion_actions.get_store"):
            publish_overview(
                project_id=34, mr_iid=263,
                inline_posted_count=0,
                head_sha="e866ffcb0d8c5b5a4e0a3a2b1c0d9e8f7a6b5c4",
                run_late_detect=True,
            )

    mock_late.assert_called_once()
