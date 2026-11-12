"""E11: `review_runs` 的成本信号语义断言 —— 不再只做恒等式.

Why 需要这份测试:
此前 `audit_review_runs` 对 token 只有一条**恒等式**断言:
    `total_tokens == prompt_tokens + completion_tokens`
恒等式恒成立（`finish_run` 就是这么写的），所以它**永远抓不到任何真实缺陷** ——
即使 provider 一次都没回填计量、即使 token 采集链路整个断了，这条断言照样绿。

而真实业务里恰恰踩过这个坑: 2026-11 复验发现
    - p31 上 33 个 run 的 `total_tokens` **全部为 0**
    - 同时有 17 个 run 的 `cost_credits > 0`
即「provider 不回填 token，但 credits 有」—— 只断言恒等式完全看不见这件事。

修复后的契约（依赖 E8 的 `model` 语义拆分）:
    `model` 非空  ⇒ provider **实际回报了模型** ⇒ 本轮真的调了 LLM
                    ⇒ 必须有成本信号 (total_tokens > 0 或 cost_credits > 0)
    `model` 为空  ⇒ 本轮**没调 LLM**（improve 全文件复用早退，见
                    reviewagent/review/runner.py 的 all_reused 分支）
                    ⇒ 必须既无 token 也无 credits

另加两条:
    - 计量值不得为负（负 token / 负 credits 一定是 bug）
    - 整个 MR 至少有一个 success run 带成本信号 —— 防止「计量链路整体静默失效」
      这种每行单看都「自洽」但整体没意义的场景。

E8 之前 `model` 列被配置 id 兜底填充，无法区分「真调了」与「没调」，
所以这条断言必须与 E8 一起生效 —— 两者的因果关系见 tests/test_review_run_model_fields.py。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "e2e"))

import telemetry_audit as ta  # noqa: E402

PROJECT = 31
MR = 3
MR_SHA = "a" * 40
NOW = "2026-11-08T10:01:00+00:00"


def _expect() -> ta.MrExpectation:
    return ta.MrExpectation(
        project_id=PROJECT,
        mr_iid=MR,
        title="E2E 真实变更 r1",
        author_username="Administrator@root",
        source_branch="e2e/change-r1",
        target_branch="main",
        state="opened",
        created_at="2026-11-08T10:00:00+00:00",
        head_sha=MR_SHA,
        changed_files=("app/service.py",),
        discussion_ids=frozenset({"d1"}),
    )


def _run(**over):
    base = dict(
        id=1, project_id=PROJECT, mr_iid=MR, command="improve", triggered_by="note",
        actor_username="review-tester@root",
        started_at=NOW, finished_at="2026-11-08T10:02:00+00:00",
        status="success", error=None,
        model="DeepSeek-Flash",           # provider 实际回报了模型 ⇒ 调了 LLM
        prompt_tokens=0, completion_tokens=0, total_tokens=0,
        duration_ms=60000, cost_credits=0.42, llm_provider="qoder-sdk",
        rule_keys_cited="R-ERR", suggestion_count=1,
        model_configured="deepseek/deepseek-v4-flash",
    )
    base.update(over)
    return base


def _snapshot(**over):
    base = dict(
        mr=None, runs=[_run()], suggestions=[], actions=[], file_states=[],
        project_runs=1, project_mrs=1, project_rule_keys=("R-ERR",),
    )
    base.update(over)
    return ta.Snapshot(**base)


# ---------------------------------------------------------------------------
# 正例
# ---------------------------------------------------------------------------


def test_provider_reported_model_with_credits_passes() -> None:
    """provider 回了模型 + 有 credits → 通过（credits 是当前 provider 的唯一可信计量）."""
    ok, detail = ta.audit_review_runs(_snapshot(), _expect())
    assert ok, detail


def test_provider_reported_model_with_tokens_passes() -> None:
    """provider 回了模型 + 有 token（provider 将来回填 token 时同样成立）."""
    snap = _snapshot(runs=[_run(total_tokens=120, prompt_tokens=100, completion_tokens=20)])
    ok, detail = ta.audit_review_runs(snap, _expect())
    assert ok, detail


def test_no_llm_call_run_with_no_signal_is_not_individually_flagged() -> None:
    """model 为空（没调 LLM）+ 无任何计量 → **单行**不报红（全复用早退是合法路径）."""
    snap = _snapshot(runs=[
        _run(model=None, cost_credits=0.0),
        _run(id=2, model="DeepSeek-Flash", cost_credits=0.42),
    ])
    ok, detail = ta.audit_review_runs(snap, _expect())
    assert ok, detail


# ---------------------------------------------------------------------------
# 反例 —— 这些是此前恒等式完全看不见的缺陷
# ---------------------------------------------------------------------------


def test_fails_when_model_reported_but_no_cost_signal() -> None:
    """provider 回报了模型（= 真调了 LLM）却零计量 → 计量链路失效, 必须报红."""
    snap = _snapshot(runs=[_run(total_tokens=0, cost_credits=0.0)])
    ok, detail = ta.audit_review_runs(snap, _expect())
    assert ok is False, "调了 LLM 却无任何成本信号, 不得通过"
    # 必须命中 **per-run** 规则（而不是靠整体规则兜底报红），
    # 否则 per-run 断言被删掉时测试仍会绿 —— 变异验证会漏。
    assert "无任何成本信号" in detail, detail


def test_fails_when_signal_exists_without_model() -> None:
    """有成本信号却没有模型回报 → 记录自相矛盾, 必须报红."""
    snap = _snapshot(runs=[_run(model=None, cost_credits=0.42)])
    ok, detail = ta.audit_review_runs(snap, _expect())
    assert ok is False, "有成本信号却无模型回报, 不得通过"
    assert "模型" in detail, detail


def test_fails_on_negative_tokens() -> None:
    """负 token → 必须报红."""
    snap = _snapshot(runs=[_run(total_tokens=-1, prompt_tokens=-1, completion_tokens=0)])
    ok, detail = ta.audit_review_runs(snap, _expect())
    assert ok is False, "负 token 不得通过"
    assert "total_tokens" in detail, detail


def test_fails_on_negative_credits() -> None:
    """负 credits → 必须报红."""
    snap = _snapshot(runs=[_run(cost_credits=-0.5)])
    ok, detail = ta.audit_review_runs(snap, _expect())
    assert ok is False, "负 credits 不得通过"
    assert "cost_credits" in detail, detail


def test_fails_when_no_success_run_carries_signal() -> None:
    """所有 success run 都零信号 → 计量链路整体失效, 必须报红.

    注意: 每行单看都「自洽」(model=None + 零计量 = 没调 LLM),
    但**整体**上意味着这个 MR 一次 LLM 都没真正跑出计量 —— 恒等式永远抓不到。
    """
    snap = _snapshot(runs=[_run(model=None, cost_credits=0.0, total_tokens=0)])
    ok, detail = ta.audit_review_runs(snap, _expect())
    assert ok is False, "全部零信号的 success run 集合必须报红"
    assert "cost signal" in detail or "成本信号" in detail, detail


def test_skipped_run_is_exempt_from_cost_signal_requirement() -> None:
    """skipped run 没调 LLM, 不要求成本信号, 也不参与「整体零信号」判定."""
    snap = _snapshot(runs=[
        _run(status="skipped", model=None, cost_credits=0.0, total_tokens=0),
        _run(id=2, model="DeepSeek-Flash", cost_credits=0.42),
    ])
    ok, detail = ta.audit_review_runs(snap, _expect())
    assert ok, detail


def test_failed_run_is_exempt_from_cost_signal_requirement() -> None:
    """failed run 不要求成本信号（可能压根没调到 LLM）."""
    snap = _snapshot(runs=[
        _run(status="failed", model=None, cost_credits=0.0, total_tokens=0, error="boom"),
        _run(id=2, model="DeepSeek-Flash", cost_credits=0.42),
    ])
    ok, detail = ta.audit_review_runs(snap, _expect())
    assert ok, detail


# ---------------------------------------------------------------------------
# 历史行豁免
# ---------------------------------------------------------------------------


def test_legacy_run_without_model_configured_is_exempt() -> None:
    """E8 之前的历史行（model_configured 为 NULL）豁免 per-run 规则.

    Why: E8 之前 `model` 被配置 id 兜底填充, 语义两义 ——
    实测 58 条历史 success 行 = 「配置 id + 零信号」, 实为全复用早退（没调 LLM）.
    若不豁免, 新断言会把这些**正确的历史数据**判成缺陷.

    判别依据 = `model_configured`: E8 之后每行都写, 之前为 NULL.
    """
    snap = _snapshot(runs=[
        _run(model="deepseek/deepseek-v4-flash", model_configured=None,
             cost_credits=0.0, total_tokens=0),
        _run(id=2, model="DeepSeek-Flash", cost_credits=0.42),
    ])
    ok, detail = ta.audit_review_runs(snap, _expect())
    assert ok, f"历史行不应被判红: {detail}"


def test_new_run_with_model_configured_is_not_exempt() -> None:
    """对照组: E8 之后的新行（model_configured 有值）**不豁免**."""
    snap = _snapshot(runs=[
        _run(model="deepseek/deepseek-v4-flash",
             model_configured="deepseek/deepseek-v4-flash",
             cost_credits=0.0, total_tokens=0),
        _run(id=2, model="DeepSeek-Flash", cost_credits=0.42),
    ])
    ok, detail = ta.audit_review_runs(snap, _expect())
    assert ok is False, "新行的零信号仍必须报红"
    assert "无任何成本信号" in detail, detail


def test_missing_model_configured_column_is_reported() -> None:
    """老库未迁移（缺 model_configured 列）→ 报红, 不得静默豁免全部行.

    Why 必须这样: 若把「列不存在」当成「全是历史行」→ 豁免,
    等于把整个 E11 断言关掉 —— 比误报危险得多。
    """
    run_without_col = _run(model="DeepSeek-Flash", cost_credits=0.42)
    run_without_col.pop("model_configured")
    snap = _snapshot(runs=[run_without_col])
    ok, detail = ta.audit_review_runs(snap, _expect())
    assert ok is False, "缺列必须报红, 不得静默豁免"
    assert "model_configured" in detail, detail
