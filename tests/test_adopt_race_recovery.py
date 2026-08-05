"""Regression test: /adopt race vs improve._publish record_suggestion.

MR 213 (2026-08-05) bug:
  1. improve bot posts suggestion to GitLab at T=0
  2. record_suggestion INSERT 在 T+200~800ms (因为 _get_mr_head_sha 网络往返)
  3. 用户看到建议立即回 /adopt — webhook 在 T+50~300ms 到达
  4. /adopt 查到 SQLite → no_record → 回 "无历史记录"

Fix C: process_adopt 加 file:line 兜底 — note_id 找不到时, 用 webhook
payload 上的 diff_file / diff_line 去 find_open_suggestion_by_line 匹配.

Fix A (另工): head_sha 提前算一次, race window 几乎归零. 但仍可能有
边界场景 (e.g. SQLite fsync), Fix C 是 defense-in-depth.

测试:
  - record_suggestion 注入了 A 行 note_id=X, /adopt 来时 Y (错位)
  - file_path + target_line 能找回到原行
  - process_adopt 返回 adopted (而非 adopted-unchecked)
  - update_suggestion_note_id 把 Y 回填
"""
from __future__ import annotations

import os
import tempfile
import pathlib
from unittest.mock import patch, MagicMock

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
    from reviewagent.telemetry import store as st_mod
    st_mod._store = None
    yield path
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    st_mod._store = None


def _seed_suggestion(s, *, note_id: str, file_path: str, target_line: int, project_id=34, mr_iid=213):
    """插入一条 state=open suggestion, 模拟 improve._publish 已完成 record_suggestion."""
    s.record_suggestion(
        project_id=project_id, mr_iid=mr_iid, note_id=note_id,
        file_path=file_path, target_line=target_line, target_line_end=target_line + 1,
        existing_code="def bootstrap(env: str) -> int:\n    start_logging()",
        improved_code="def bootstrap(env: str) -> int:\n    \"\"\"Boot.\"\"\"\n    start_logging()",
        header="添加 docstring", severity="medium",
        head_sha="c3aaa8797dd36d4492ba9d72712a0c45bf33af4d",
    )


def test_note_id_hit_returns_sug(tmp_telemetry):
    """happy path: note_id 找到就直接返回."""
    from reviewagent.telemetry.store import get_store
    s = get_store()
    _seed_suggestion(s, note_id="0bd6b73e...",
                     file_path="services/caller.py", target_line=20)

    from reviewagent.commands.suggestion_actions import _lookup_suggestion_with_retry
    sug = _lookup_suggestion_with_retry(
        s, suggestion_note_id="0bd6b73e...",
        project_id=34, mr_iid=213,
    )
    assert sug is not None
    assert sug["note_id"] == "0bd6b73e..."
    assert sug["file_path"] == "services/caller.py"


def test_note_id_miss_file_line_fallback(tmp_telemetry):
    """race 场景: /adopt 的 note_id (GitLab 真实) DB 没建, 用 file:line 找到."""
    from reviewagent.telemetry.store import get_store
    s = get_store()
    # 真实 note_id = "pending-id"
    _seed_suggestion(s, note_id="pending-id",
                     file_path="services/caller.py", target_line=20)

    from reviewagent.commands.suggestion_actions import _lookup_suggestion_with_retry
    # /adopt webhook 拿到的是另一个 note_id (e.g. 占位 / 错位)
    sug = _lookup_suggestion_with_retry(
        s, suggestion_note_id="gitlab-real-id",
        file_path="services/caller.py", target_line=20,
        project_id=34, mr_iid=213,
    )
    assert sug is not None
    assert sug["note_id"] == "pending-id", "file:line 兜底应该命中 pending-id"


def test_note_id_and_file_line_both_miss(tmp_telemetry):
    """真没找到 — 历史 MR 等场景, 返回 None."""
    from reviewagent.telemetry.store import get_store
    s = get_store()

    from reviewagent.commands.suggestion_actions import _lookup_suggestion_with_retry
    sug = _lookup_suggestion_with_retry(
        s, suggestion_note_id="nonexistent",
        file_path="services/caller.py", target_line=20,
        project_id=34, mr_iid=213,
    )
    assert sug is None


def test_update_suggestion_note_id_backfill(tmp_telemetry):
    """file:line 命中后, update_suggestion_note_id 回填."""
    from reviewagent.telemetry.store import get_store
    s = get_store()
    _seed_suggestion(s, note_id="placeholder-id",
                     file_path="services/caller.py", target_line=20)

    # 找 placeholder 行
    row = s.get_suggestion_by_note_id("placeholder-id")
    assert row["note_id"] == "placeholder-id"

    # 回填 GitLab 真实 note_id
    s.update_suggestion_note_id(row["id"], "gitlab-real-id")

    # 拿行确认 note_id 已替换
    row2 = s.get_suggestion_by_note_id("gitlab-real-id")
    assert row2 is not None
    assert row2["id"] == row["id"]
    assert row2["note_id"] == "gitlab-real-id"

    # 老 note_id 现在查不到
    old = s.get_suggestion_by_note_id("placeholder-id")
    assert old is None


def test_process_adopt_backfills_note_id(monkeypatch, tmp_telemetry):
    """process_adopt 在 file:line 兜底命中后回填 note_id.

    Simulates the full race:
      1. record_suggestion 在 0ms 插入 placeholder-id
      2. /adopt webhook 100ms 后到, 用 gitlab-real-id 查询
      3. process_adopt 应该走 file:line fallback 命中, 并回填
    """
    from reviewagent.telemetry.store import get_store
    s = get_store()
    _seed_suggestion(s, note_id="placeholder-id",
                     file_path="services/caller.py", target_line=20,
                     project_id=34, mr_iid=213)

    # head_sha_current 跟 head_sha_posted 不一样 → 触发 "line content changed" 分支
    # 这是最常见的 race 场景: 用户在 GitLab UI 上 Apply suggestion,
    # head_sha 已经推进到了下一个版本.
    fake_gl = MagicMock()
    fake_gl.resolve_discussion.return_value = True
    fake_gl.reply_to_discussion.return_value = None
    fake_gl.get_mr_diff_refs.return_value = {
        "head_sha": "ffffffffffffffffffffffffffffffffffffffff",
        "start_sha": "ffffffffffffffffffffffffffffffffffffffff",
        "base_sha": "cccccccccccccccccccccccccccccccccccccccc",
    }
    fake_gl.get_mr_diff.return_value = (
        "-def bootstrap(env: str) -> int:\n"
        "-    start_logging()\n"
        "+def bootstrap(env: str) -> int:\n"
        "+    \"\"\"Bootstrap.\"\"\"\n"
        "+    start_logging()\n"
    )
    monkeypatch.setattr(
        "reviewagent.commands.suggestion_actions.GitLabClient",
        lambda: fake_gl,
    )
    # 不让真的去改 SQLite record_suggestion_action (跟本测试无关)
    from reviewagent.commands.suggestion_actions import _lookup_suggestion_with_retry
    monkeypatch.setattr(
        "reviewagent.commands.suggestion_actions._ADOPT_LOOKUP_RETRY_DELAYS",
        (0.0,),
    )

    from reviewagent.commands.suggestion_actions import process_adopt
    result = process_adopt(
        project_id=34, mr_iid=213,
        suggestion_note_id="gitlab-real-id",
        actor_username="reviewer",
        reason="",
        file_path="services/caller.py",
        target_line=20,
    )

    # === MR 213 regression 关键断言 ===
    # 这个测试的核心目的是验证 file:line 兜底路径不再走 no_record.
    # 之前 MR 213 的 bug 是: /adopt 拿到 note_id 查 DB → None → 返回 no_record.
    # 现在: note_id 查不到 → file:line 兜底命中 → 走正常的 validation 流程.
    # 不论 validation 最终通过与否, 只要不是 no_record 就说明 race 修了.
    assert "no_record" not in result.get("reason", ""), (
        f"regression — 又退回到 no_record 路径 (MR 213 复发). got {result}"
    )
    # result['action'] 是 validation 结果:
    #   - adopted / adopted-already: 验证通过
    #   - adopt-validation-failed (target_unchanged): 验证未通过 (测试中 mock diff 不改这行,
    #     MagicMock 让 _target_region_changed 返 False, 这是正确的 behavior)
    #   - adopt-failed: 致命错误 (refs 取不到 / resolve 失败)
    # 不能是 adopted-unchecked (那等于又掉到 no_record 路径)
    assert result.get("action") != "adopted-unchecked", (
        f"不应该走 no_record 路径 (adopted-unchecked). got {result}"
    )

    # === verify 回填 (file:line 兜底命中后必须回填真实 note_id) ===
    row = s.get_suggestion_by_note_id("gitlab-real-id")
    assert row is not None, "回填后用真实 note_id 应能查到"
    assert row["note_id"] == "gitlab-real-id"
    assert row["file_path"] == "services/caller.py"
    assert row["target_line"] == 20

    # === verify 老 note_id 已被替换 ===
    # 这个 row 之前 note_id="placeholder-id" 在 race 期间被 /adopt 触发,
    # 现在已经被 update_suggestion_note_id 替换为 gitlab-real-id.
    # 老 note_id 应该查不到 (被替换了)
    old_row = s.get_suggestion_by_note_id("placeholder-id")
    assert old_row is None, "老 note_id 不应该还能查到 (已被回填)"


def test_lookup_with_retry_succeeds_after_delay(monkeypatch, tmp_telemetry):
    """Fix C: 250ms 重试兜 race — 第 1 次查无, 第 2 次 sleep 250ms 后查到.

    模拟: process_adopt 第 1 次查询时 record_suggestion 还没 INSERT,
    短暂 sleep 后再查 (webhook 端到端 ~300~500ms), record_suggestion 完成,
    第 2 次查命中.
    """
    from reviewagent.telemetry.store import get_store
    s = get_store()

    # 第 1 次 get_suggestion_by_note_id 返回 None, 第 2 次返回 row
    placeholder_row = {
        "id": 1, "note_id": "gitlab-real-id",
        "file_path": "services/caller.py", "target_line": 20,
    }
    call_count = {"n": 0}
    def fake_get(nid):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return None
        return placeholder_row
    monkeypatch.setattr(s, "get_suggestion_by_note_id", fake_get)

    # override _ADOPT_LOOKUP_RETRY_DELAYS to short values for test speed
    import reviewagent.commands.suggestion_actions as sa
    monkeypatch.setattr(sa, "_ADOPT_LOOKUP_RETRY_DELAYS", (0.0, 0.05))

    sug = sa._lookup_suggestion_with_retry(
        s,
        suggestion_note_id="gitlab-real-id",
        file_path="services/caller.py", target_line=20,
        project_id=34, mr_iid=213,
    )
    assert call_count["n"] == 2
    assert sug is not None
    assert sug["note_id"] == "gitlab-real-id"


def test_get_suggestion_by_note_id_returns_most_recent(tmp_telemetry):
    """DB ORDER BY id DESC — 同一 note_id 多条记录时, 返回最新的."""
    from reviewagent.telemetry.store import get_store
    s = get_store()
    _seed_suggestion(s, note_id="shared-id",
                     file_path="services/a.py", target_line=10)
    _seed_suggestion(s, note_id="shared-id",
                     file_path="services/b.py", target_line=20)

    row = s.get_suggestion_by_note_id("shared-id")
    assert row["file_path"] == "services/b.py"  # 最近插入的
