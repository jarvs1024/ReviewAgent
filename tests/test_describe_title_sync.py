"""/describe 改写 MR 标题后, telemetry 必须同步 —— 否则数据永久漂移.

缺陷背景: `mr_activity.title` 是在检视**开始前**写入的旧标题, 而
`DescribeCommand._publish` 随后把 GitLab 上的标题换成了 LLM 生成的新标题,
却没有回写 telemetry。后果:
    - dashboard 展示的永远是旧标题
    - 同一 MR 第二次 describe 换个说法, DB 与 GitLab 就对不上
    - E2E 的 `telemetry:mr_activity` 审计持续报错
      (2026-11-11 全量档实跑: 29/30, 唯一失败项就是这个)
"""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def tmp_store(monkeypatch):
    tmp = TemporaryDirectory()
    path = Path(tmp.name) / "telemetry.db"
    from reviewagent.config import config as _cfg

    monkeypatch.setattr(
        type(_cfg), "sqlite_path", property(lambda self: path), raising=False
    )
    from reviewagent.telemetry import store as st

    st._store = None
    monkeypatch.setattr("reviewagent.telemetry.store._enrich_web_url", lambda row: row)
    yield path
    st._store = None
    tmp.cleanup()


def _seed_mr(store, title: str = "旧标题") -> None:
    from reviewagent.telemetry.models import MRRecord

    store.upsert_mr(
        MRRecord(
            project_id=34,
            mr_iid=7,
            title=title,
            author_username="Alice@alice",
            source_branch="feature/x",
            target_branch="main",
            state="opened",
        )
    )


def test_store_update_mr_title_syncs_row(tmp_store):
    from reviewagent.telemetry.store import get_store

    store = get_store()
    _seed_mr(store, "旧标题")
    store.update_mr_title(34, 7, "新标题")
    assert store.get_mr(34, 7)["title"] == "新标题"


def test_store_update_mr_title_is_noop_for_unknown_mr(tmp_store):
    """未知 MR 不能抛异常 —— 同步是尽力而为, 不能因为它把整个 describe 打断."""
    from reviewagent.telemetry.store import get_store

    store = get_store()
    store.update_mr_title(34, 999, "新标题")
    assert store.get_mr(34, 999) is None


def test_describe_publish_syncs_title_to_telemetry(tmp_store, monkeypatch):
    """describe 发布标题后, telemetry 的 mr_activity.title 必须跟着变."""
    from reviewagent.commands.describe import DescribeCommand
    from reviewagent.telemetry.store import get_store

    store = get_store()
    _seed_mr(store, "旧标题")

    gitlab = MagicMock()
    cmd = DescribeCommand(project_id=34, mr_iid=7, triggered_by="note")
    cmd.gitlab = gitlab
    monkeypatch.setattr(
        "reviewagent.commands.describe.events.emit_description_generated",
        lambda *a, **k: None,
    )
    cmd._publish({"title": "新标题", "description_md": "## 变更概览\n\n- 改了点东西"})

    assert gitlab.update_mr_title.call_args[0] == (34, 7, "新标题")
    assert store.get_mr(34, 7)["title"] == "新标题"


def test_describe_publish_keeps_telemetry_on_gitlab_error(tmp_store, monkeypatch):
    """GitLab 改标题失败 → 抛错, telemetry 保持旧标题（不能写成未生效的新标题）."""
    from reviewagent.commands.describe import DescribeCommand
    from reviewagent.gitlab.client import GitLabError
    from reviewagent.telemetry.store import get_store

    store = get_store()
    _seed_mr(store, "旧标题")

    gitlab = MagicMock()
    gitlab.update_mr_title.side_effect = GitLabError("boom")
    cmd = DescribeCommand(project_id=34, mr_iid=7, triggered_by="note")
    cmd.gitlab = gitlab

    with pytest.raises(Exception):
        cmd._publish({"title": "新标题", "description_md": "## 变更概览\n\n- x"})

    assert store.get_mr(34, 7)["title"] == "旧标题"
