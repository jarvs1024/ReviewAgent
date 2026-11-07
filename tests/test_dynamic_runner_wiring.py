"""R6 接线判别测试：`review/runner.py:run()` 的动态规则分支.

Task 14 审查 Major 1 —— 三条路径必须由 `run()` 级测试区分：

* 显式 `DynamicRuleSet` + 启用规则 + 非空回调 → builder 收到该文件的 refs
  （`review_file` 标注已被剥离）且 `cross_file_enabled=True`；
* 规则全部禁用 → `cross_file_enabled=False`、refs 为空、回调不被调用；
* 降级证据（超时）→ refs 为空、section 仍启用、只记一条 `dynamic.degraded`。

全部使用假回调 / 假 policy，不触 LLM、网络与 SQLite。
"""
from __future__ import annotations

import dataclasses
import time
from types import SimpleNamespace

import pytest
from loguru import logger as loguru_logger

from reviewagent.config import current
from reviewagent.review.dynamic import DynamicRule, DynamicRuleSet
from reviewagent.review.runner import Callbacks, RunContext, run

RULE_KEY = "DYN-CROSS-FILE-REFS"


@pytest.fixture(autouse=True)
def _isolate_store(monkeypatch):
    """runner 末尾的 file-state 读写改走内存替身，避免写用户 SQLite."""

    class _Store:
        def upsert_file_review_state(self, **_kwargs) -> None:
            return None

        def get_file_review_states(self, *_args) -> dict:
            return {}

        def list_suggestions(self, **_kwargs) -> list:
            return []

    import reviewagent.telemetry.store as store_mod

    monkeypatch.setattr(store_mod, "get_store", lambda: _Store())


@pytest.fixture
def logs():
    messages: list[str] = []
    handler_id = loguru_logger.add(
        lambda message: messages.append(str(message).rstrip("\n")),
        level="INFO",
        format="{message}",
    )
    try:
        yield messages
    finally:
        loguru_logger.remove(handler_id)


def _callbacks(**overrides) -> Callbacks:
    kwargs = dict(
        split_diff=lambda diff_file, files: {fp: "+def alpha():\n" for fp in files},
        diff_line_map=lambda: {"a.py": {1}, "b.py": {2}},
        read_lines=lambda fp: [],
        read_line_count=lambda fp, ws: 20,
        stat_bytes=lambda fp: 100,
        blob_sha=lambda fp, ws: None,
        head_sha=lambda: None,
        new_files=lambda diff_file: set(),
        collect_cross_refs=lambda files, diff_by_file, worktree: {},
        build_chunk_prompt=lambda *a, **kw: "",
        build_batch_prompt=lambda *a, **kw: "",
        call_chunk=lambda prompt, ws, fp: SimpleNamespace(
            data={"summary_md": "", "suggestions": []},
            prompt_tokens=0, completion_tokens=0, cost_credits=0.0, model="fake",
        ),
        build_user_prompt=lambda: "",
        super_call_agent=lambda ws: {},
        set_token_summary=lambda p, c, m, cost: None,
        budget_fn=lambda diff_lines: 10_000,
    )
    kwargs.update(overrides)
    return Callbacks(**kwargs)


def _ctx(tmp_path, *, dynamic_rules, callbacks=None, **callback_overrides) -> RunContext:
    diff_file = tmp_path / ".diff"
    diff_file.write_text("", encoding="utf-8")
    cfg = dataclasses.replace(
        current(),
        review_exclude_extensions=(),
        improve_batch_enabled=False,
        improve_parallel_workers=1,
    )
    return RunContext(
        project_id=1, mr_iid=2,
        ws=SimpleNamespace(worktree=tmp_path, diff_file=diff_file),
        repo_context="",
        cfg=cfg,
        profile=SimpleNamespace(
            name="test-profile", language="fake", scoring={}, rule_keys={},
        ),
        rule_set=SimpleNamespace(verbatim_body=""),
        dynamic_rules=dynamic_rules,
        policy=SimpleNamespace(
            score_suggestion=lambda s: 100,
            test_feature_density=lambda fp, n: 0.0,
            extract_symbols=lambda diff: [],
        ),
        callbacks=callbacks or _callbacks(**callback_overrides),
    )


def _ruleset(*, enabled: bool = True, params: dict | None = None) -> DynamicRuleSet:
    rule = DynamicRule(
        key=RULE_KEY,
        title="跨文件引用",
        provider="cross_file_refs",
        enabled=enabled,
        params=params or {},
    )
    return DynamicRuleSet(rules=(rule,), fingerprint=f"fp:{RULE_KEY}")


def _ref(file: str, line: int, ident: str) -> dict:
    return {"file": file, "line": line, "content": f"{ident}()", "ident": ident}


def _capture_chunk_prompts(**overrides) -> tuple[dict[str, dict], Callbacks]:
    seen: dict[str, dict] = {}

    def _build(fp, diff, lines, ws, cross_file_refs=None, *,
               strategy="full", prior_issues=None, cross_file_enabled=True):
        seen[fp] = {
            "refs": cross_file_refs,
            "enabled": cross_file_enabled,
            "strategy": strategy,
        }
        return ""

    return seen, _callbacks(build_chunk_prompt=_build, **overrides)


def test_enabled_rule_feeds_annotated_refs_into_prompts(tmp_path):
    callback_calls: list[list[str]] = []
    ref_a = _ref("caller_a.py", 3, "alpha")
    ref_b = _ref("caller_b.py", 9, "beta")

    def _collect(files, diff_by_file, worktree):
        callback_calls.append(list(files))
        return {"a.py": [ref_a], "b.py": [ref_b]}

    seen, callbacks = _capture_chunk_prompts(collect_cross_refs=_collect)
    ctx = _ctx(tmp_path, dynamic_rules=_ruleset(), callbacks=callbacks)
    run(ctx)

    assert sorted(seen) == ["a.py", "b.py"]
    # provider 标注的 review_file 已剥离，refs 与迁移前 legacy 形态逐字节一致。
    assert seen["a.py"]["refs"] == [ref_a]
    assert seen["b.py"]["refs"] == [ref_b]
    assert "review_file" not in seen["a.py"]["refs"][0]
    assert seen["a.py"]["enabled"] is True
    assert seen["b.py"]["enabled"] is True
    assert callback_calls == [["a.py", "b.py"]]


def test_disabled_ruleset_omits_section_and_skips_callback(tmp_path):
    callback_calls: list[list[str]] = []

    def _collect(files, diff_by_file, worktree):
        callback_calls.append(list(files))
        return {"a.py": [_ref("caller.py", 1, "alpha")]}

    seen, callbacks = _capture_chunk_prompts(collect_cross_refs=_collect)
    ctx = _ctx(tmp_path, dynamic_rules=_ruleset(enabled=False), callbacks=callbacks)
    run(ctx)

    assert sorted(seen) == ["a.py", "b.py"]
    assert all(item["refs"] == [] for item in seen.values())
    assert all(item["enabled"] is False for item in seen.values())
    assert callback_calls == []


def test_degraded_evidence_keeps_section_and_logs_once(tmp_path, logs):
    def _slow_collect(files, diff_by_file, worktree):
        time.sleep(0.2)
        return {fp: [_ref("late.py", 1, "alpha")] for fp in files}

    # 超时阈值取规则参数（0.01s）→ 线程未结束 → degraded reason="timeout"。
    seen, callbacks = _capture_chunk_prompts(collect_cross_refs=_slow_collect)
    ctx = _ctx(
        tmp_path,
        dynamic_rules=_ruleset(params={"timeout_seconds": 0.01}),
        callbacks=callbacks,
    )
    run(ctx)

    assert sorted(seen) == ["a.py", "b.py"]
    assert all(item["refs"] == [] for item in seen.values())
    assert all(item["enabled"] is True for item in seen.values())
    degraded = [m for m in logs if "dynamic.degraded" in m]
    assert len(degraded) == 1
    assert f"rule={RULE_KEY}" in degraded[0]
    assert "reason=timeout" in degraded[0]
