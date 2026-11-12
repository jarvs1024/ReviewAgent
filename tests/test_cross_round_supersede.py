"""E12: 跨轮次 supersede 的**确定性**链路测试（不依赖 LLM 随机输出）.

Why 需要这份测试:
「同一 MR 多次检视, 同一问题被重复发布」是 ReviewAgent 最容易出错的场景:
第 1 轮发布了建议 → 用户 applied → 第 2 轮 LLM 又报同一问题 →
新一代建议必须把旧代标 superseded, 否则:
    - `build_overview` 的 cohort 归会把 V1/V2 算成两条 (MR !3 图1 实测少算/多算)
    - 汇总里出现 "applied 1 + open 1" 的自相矛盾状态
    - `/adopt` 时与旧代互相干扰

此前只有**单点**覆盖:
    - `test_handle_cohort_reoccurrence_supersedes` —— 只验「旧条变 superseded」
    - `test_handle_cohort_reoccurrence_no_op_when_problem_fixed` —— 只验「已修好则不动」
缺的是**跨轮次完整链路**的端到端确定性断言:
    V1 发布 → applied → V2 发布触发 supersede → cohort 归并后只剩最新一条 →
    计数不再双计 → generation 递增 → supersedes_note_id 反向链正确。

为什么放单元/集成层而不是 E2E:
E2E 里「LLM 第 2 轮又报同一问题」取决于 LLM 随机输出, 无法确定性构造;
而这条链路真正的风险在**状态机与归并计数**, 用受控 store 反而更能覆盖边界
(V2 是 dismissed 不是 applied / V2 之前有别的 cohort / 三代并存 …)。
"""
from __future__ import annotations

import os
import pathlib
import tempfile
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def store(monkeypatch):
    """隔离 sqlite 的 telemetry store."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    from reviewagent.config import config as _cfg

    monkeypatch.setattr(
        type(_cfg),
        "sqlite_path",
        property(lambda self: pathlib.Path(db_path)),
        raising=False,
    )
    import reviewagent.telemetry.store as st_mod

    st_mod._store = None
    try:
        yield st_mod.get_store()
    finally:
        st_mod._store = None
        try:
            os.unlink(db_path)
        except FileNotFoundError:
            pass


COHORT = "cohort-print-x"
FILE = "pkg/service.py"
EXISTING = "print(x)\n"
FIXED_CONTENT = "def build():\n    x = []\n    return x\n"
BROKEN_CONTENT = "def build():\n    print(x)\n    return x\n"


def _seed(store, *, note_id: str, head_sha: str, state: str | None = None,
          cohort_key: str = COHORT):
    """落一条 suggestion（可指定初始 state）.

    注: `cohort_generation` 不走 record_suggestion —— 它由 `supersede_suggestion`
    在把旧代标记 superseded 时写进新一代记录（见 store.py 的两段 UPDATE）。
    """
    store.record_suggestion(
        project_id=34, mr_iid=7, note_id=note_id,
        file_path=FILE, target_line=12, target_line_end=12,
        existing_code=EXISTING, improved_code="logger.info(x)\n",
        header="禁止裸 print", label="potential bug",
        fingerprint=f"fp-{note_id}", cohort_key=cohort_key,
        severity_source="rule", head_sha=head_sha,
    )
    if state:
        store.update_suggestion_state(note_id, state, actor_username="tester")


def _gitlab(content: str):
    gl = MagicMock()
    gl.get_file_at_sha.return_value = content
    return gl


def _reoccur(store, *, new_note_id: str, head_sha: str, gitlab):
    """模拟「新一代发布后触发跨轮次 supersede」.

    真实链路顺序（reviewagent/review/publishing.py 的 publish 流程）:
        1. 新建议先 record_suggestion 落库 (拿到 note_id)
        2. 再调 _handle_cohort_reoccurrence 判定是否 supersede 上一代
    所以这里必须先把新一代落库 —— 否则 supersede_suggestion 更新的是
    一条**不存在的**新记录, generation / 反向链都会写空。
    """
    _seed(store, note_id=new_note_id, head_sha=head_sha)
    from reviewagent.review.publishing import _handle_cohort_reoccurrence

    _handle_cohort_reoccurrence(
        store=store, project_id=34, mr_iid=7,
        cohort_key=COHORT, new_note_id=new_note_id,
        file_path=FILE, target_line=12, target_line_end=12,
        existing=EXISTING, head_sha=head_sha, gitlab=gitlab,
    )


# ---------------------------------------------------------------------------
# 跨轮次主链路
# ---------------------------------------------------------------------------


def test_v1_applied_then_v2_published_supersedes_v1(store):
    """V1 applied + 问题仍在 → V2 发布时 V1 变 superseded 且记录反向链."""
    _seed(store, note_id="v1", head_sha="sha-1", state="applied")
    _reoccur(store, new_note_id="v2", head_sha="sha-2", gitlab=_gitlab(BROKEN_CONTENT))

    v1 = store.get_suggestion_by_note_id("v1")
    assert v1["state"] == "superseded", f"V1 应被 V2 取代, 实际 state={v1['state']}"
    assert v1["supersedes_note_id"] == "v2", v1
    assert v1["superseded_at"] is not None, "superseded_at 必须落库"


def test_v2_generation_is_bumped(store):
    """V2 的 cohort_generation 必须 = V1 + 1（代际递增）."""
    _seed(store, note_id="v1", head_sha="sha-1", state="applied")
    _reoccur(store, new_note_id="v2", head_sha="sha-2", gitlab=_gitlab(BROKEN_CONTENT))
    v2 = store.get_suggestion_by_note_id("v2")
    assert int(v2["cohort_generation"] or 1) == 2, (
        f"V2 generation 应为 2, 实际={v2['cohort_generation']}"
    )


def test_cohort_rollup_counts_only_latest_after_supersede(store):
    """supersede 后 cohort 归并只留最新一条 —— 汇总不再双计."""
    _seed(store, note_id="v1", head_sha="sha-1", state="applied")
    _reoccur(store, new_note_id="v2", head_sha="sha-2", gitlab=_gitlab(BROKEN_CONTENT))

    latest = store.list_latest_by_cohort(project_id=34, mr_iid=7)
    note_ids = [r["note_id"] for r in latest]
    assert "v2" in note_ids, note_ids
    assert "v1" not in note_ids, f"V1 已 superseded, 不该出现在归并结果: {note_ids}"


def test_superseded_count_reflects_cross_round_supersede(store):
    """跨轮次 supersede 必须计入 count_superseded_in_mr（审计口径）."""
    _seed(store, note_id="v1", head_sha="sha-1", state="applied")
    _reoccur(store, new_note_id="v2", head_sha="sha-2", gitlab=_gitlab(BROKEN_CONTENT))
    assert store.count_superseded_in_mr(project_id=34, mr_iid=7) == 1


# ---------------------------------------------------------------------------
# 边界: 不该 supersede 的情形
# ---------------------------------------------------------------------------


def test_v1_dismissed_and_problem_still_there_supersedes(store):
    """V1 dismissed 但问题仍在 → 同样 supersede（用户 dismiss 不等于问题被修）."""
    _seed(store, note_id="v1", head_sha="sha-1", state="dismissed")
    _reoccur(store, new_note_id="v2", head_sha="sha-2", gitlab=_gitlab(BROKEN_CONTENT))
    assert store.get_suggestion_by_note_id("v1")["state"] == "superseded"


def test_v1_open_is_not_superseded(store):
    """V1 仍是 open → 不 supersede（走正常 dedup 路径, 否则用户未处理的建议被抹掉）."""
    _seed(store, note_id="v1", head_sha="sha-1", state=None)
    _reoccur(store, new_note_id="v2", head_sha="sha-2", gitlab=_gitlab(BROKEN_CONTENT))
    assert store.get_suggestion_by_note_id("v1")["state"] == "open"


def test_problem_fixed_no_supersede(store):
    """问题已真修好（existing 不再出现）→ 不 supersede."""
    _seed(store, note_id="v1", head_sha="sha-1", state="applied")
    _reoccur(store, new_note_id="v2", head_sha="sha-2", gitlab=_gitlab(FIXED_CONTENT))
    assert store.get_suggestion_by_note_id("v1")["state"] == "applied"


def test_gitlab_api_failure_is_non_fatal(store):
    """GitLab 查询失败 → 不 supersede, 且不得抛异常打断发布."""
    _seed(store, note_id="v1", head_sha="sha-1", state="applied")
    gl = MagicMock()
    gl.get_file_at_sha.side_effect = RuntimeError("gitlab down")
    _reoccur(store, new_note_id="v2", head_sha="sha-2", gitlab=gl)
    assert store.get_suggestion_by_note_id("v1")["state"] == "applied"


def test_other_cohort_is_not_touched(store):
    """不同 cohort 的建议不受影响."""
    _seed(store, note_id="v1", head_sha="sha-1", state="applied")
    _seed(store, note_id="other", head_sha="sha-1", state="applied",
          cohort_key="cohort-other")
    _reoccur(store, new_note_id="v2", head_sha="sha-2", gitlab=_gitlab(BROKEN_CONTENT))
    assert store.get_suggestion_by_note_id("other")["state"] == "applied"


# ---------------------------------------------------------------------------
# 三代并存
# ---------------------------------------------------------------------------


def test_three_generations_chain_correctly(store):
    """三代并存: V1 applied → V2 发布 supersede V1.

    设计语义（**不是**三代逐个 supersede）:
    `_handle_cohort_reoccurrence` 只在上一代处于 **applied/dismissed** 时才 supersede ——
    上一代若仍是 `open`（用户还没处理），把它抹掉等于**丢掉用户待处理的建议**。
    所以三代链的真实形态是:
        V1 applied → V2 发布时 V1 superseded（V2 成为唯一代表, generation=2）
        → V3 发布时上一代 V2 仍是 open ⇒ **不** supersede
    归并层（list_latest_by_cohort）只取最新一条, 用户看到的仍是 V3。
    """
    _seed(store, note_id="v1", head_sha="sha-1", state="applied")
    _reoccur(store, new_note_id="v2", head_sha="sha-2", gitlab=_gitlab(BROKEN_CONTENT))
    _reoccur(store, new_note_id="v3", head_sha="sha-3", gitlab=_gitlab(BROKEN_CONTENT))

    v1 = store.get_suggestion_by_note_id("v1")
    v2 = store.get_suggestion_by_note_id("v2")
    v3 = store.get_suggestion_by_note_id("v3")
    assert v1["state"] == "superseded", v1
    assert v1["supersedes_note_id"] == "v2", v1
    # V2 是 open ⇒ 用户还没处理, 不得被 V3 抹掉
    assert v2["state"] == "open", v2
    assert v3["state"] == "open", v3
    assert int(v3["cohort_generation"] or 1) == 1, (
        f"V3 上一代未终结, generation 不应被 bump, 实际={v3['cohort_generation']}"
    )


def test_three_generations_when_each_previous_is_terminal(store):
    """若每一代都被用户处理掉（applied/dismissed），则逐代 supersede 成链."""
    _seed(store, note_id="v1", head_sha="sha-1", state="applied")
    _reoccur(store, new_note_id="v2", head_sha="sha-2", gitlab=_gitlab(BROKEN_CONTENT))
    # 用户处理了 V2
    store.update_suggestion_state("v2", "dismissed", actor_username="tester")
    _reoccur(store, new_note_id="v3", head_sha="sha-3", gitlab=_gitlab(BROKEN_CONTENT))

    v1 = store.get_suggestion_by_note_id("v1")
    v2 = store.get_suggestion_by_note_id("v2")
    v3 = store.get_suggestion_by_note_id("v3")
    assert v1["state"] == "superseded", v1
    assert v1["supersedes_note_id"] == "v2", v1
    assert v2["state"] == "superseded", v2
    assert v2["supersedes_note_id"] == "v3", v2
    assert v3["state"] == "open", v3
    assert int(v3["cohort_generation"] or 1) == 3, v3


def test_final_cohort_rollup_keeps_exactly_one_open(store):
    """跨轮次发布后, cohort 归并只剩一条 open（最新代）."""
    _seed(store, note_id="v1", head_sha="sha-1", state="applied")
    _reoccur(store, new_note_id="v2", head_sha="sha-2", gitlab=_gitlab(BROKEN_CONTENT))
    _reoccur(store, new_note_id="v3", head_sha="sha-3", gitlab=_gitlab(BROKEN_CONTENT))

    latest = store.list_latest_by_cohort(project_id=34, mr_iid=7)
    opens = [r for r in latest if r["state"] == "open"]
    assert len(opens) == 1, f"应只剩一条 open, 实际={[r['note_id'] for r in opens]}"
    assert opens[0]["note_id"] == "v3"
