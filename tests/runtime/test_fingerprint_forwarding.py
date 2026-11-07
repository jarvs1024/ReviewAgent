"""runner 级指纹透传/落库：`run()` 把 profile/ruleset 指纹交给 planner 并写回 store.

对应终审 C2 Major 2：planner 的指纹失效逻辑已有单测，但「runner 是否真的把
两个指纹转发给 planner、并在 `upsert_file_review_state` 落库」缺少端到端断言。
本用例只验证转发/落库，不改变 runner 行为：

* profile / rule_set 使用真实默认对象（`load_profile(None)` + `assemble_base`）；
* `reviewagent.review.runner.plan_files` 被替换为捕获 kwargs 的桩，返回最小 `Plan`；
* fake Store 捕获 `upsert_file_review_state` 的参数（不落盘）。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from reviewagent.config import current
from reviewagent.llm.base import LLMResult
from reviewagent.review import runner as runner_module
from reviewagent.review.planner import Plan
from reviewagent.review.policies.registry import get as get_policy
from reviewagent.review.runner import Callbacks, RunContext
from reviewagent.scenario.profile import load_profile
from reviewagent.scenario.rules.resolver import assemble_base
from reviewagent.telemetry import store as store_module


class _FakeStore:
    """只实现 runner 用到的两个 store 方法，并记录 upsert 参数."""

    def __init__(self) -> None:
        self.upserts: list[dict] = []

    def get_file_review_states(self, _project_id, _mr_iid) -> dict:
        return {}

    def upsert_file_review_state(self, **kwargs) -> None:
        self.upserts.append(dict(kwargs))


@pytest.fixture
def profile():
    return load_profile(None)


@pytest.fixture
def rule_set(profile):
    return assemble_base(profile)


def _callbacks(tmp_path) -> Callbacks:
    """最小回调集合：只需覆盖单文件 strategy 路径实际走到的分支."""

    def _split_diff(_diff_file, files):
        return {fp: "" for fp in files}

    return Callbacks(
        split_diff=_split_diff,
        diff_line_map=lambda: {"a.py": {1}},
        read_lines=lambda _path: ["x = 1"],
        read_line_count=lambda _fp, _ws: 1,
        stat_bytes=lambda _fp: 7,
        blob_sha=lambda _fp, _ws: "blob-a",
        head_sha=lambda: "head-1",
        new_files=lambda _diff_file: set(),
        collect_cross_refs=lambda files, _diffs, _worktree: {},
        build_chunk_prompt=lambda *args, **kwargs: "chunk-prompt",
        build_batch_prompt=lambda *args, **kwargs: "batch-prompt",
        call_chunk=lambda *_args, **_kwargs: LLMResult(
            data={"summary_md": "", "suggestions": []}
        ),
        build_user_prompt=lambda: "user-prompt",
        super_call_agent=lambda _ws: {"summary_md": "", "suggestions": []},
        set_token_summary=lambda *_args: None,
        budget_fn=lambda _diff_lines: 131072,
    )


def test_run_forwards_and_persists_fingerprints(profile, rule_set, tmp_path, monkeypatch):
    captured_plan_kwargs: dict = {}

    def fake_plan_files(files, diff_by_file, **kwargs):
        captured_plan_kwargs.update(kwargs)
        captured_plan_kwargs["files"] = files
        captured_plan_kwargs["diff_by_file"] = diff_by_file
        return Plan(
            files=("a.py",),
            files_to_review=("a.py",),
            strategies={"a.py": "full"},
            batches=(("a.py",),),
            file_priorities={"a.py": 100.0},
            review_mode="business",
        )

    fake_store = _FakeStore()
    monkeypatch.setattr(runner_module, "plan_files", fake_plan_files)
    monkeypatch.setattr(store_module, "get_store", lambda: fake_store)

    policy = get_policy(
        "python", profile=profile, cfg=current(), read_lines=lambda _p: ["x = 1"]
    )
    ctx = RunContext(
        project_id=34,
        mr_iid=7,
        ws=SimpleNamespace(worktree=tmp_path, diff_file=None),
        repo_context="",
        cfg=current(),
        profile=profile,
        rule_set=rule_set,
        dynamic_rules=None,
        policy=policy,
        callbacks=_callbacks(tmp_path),
    )

    runner_module.run(ctx)

    # 1) planner 收到两个真实指纹（planner 据此做增量失效判定）.
    assert captured_plan_kwargs["profile_fingerprint"] == profile.fingerprint
    assert captured_plan_kwargs["ruleset_fingerprint"] == rule_set.fingerprint

    # 2) 本轮文件状态把两个指纹一并落库（下一轮复用依赖它们）.
    assert [row["file_path"] for row in fake_store.upserts] == ["a.py"]
    persisted = fake_store.upserts[0]
    assert persisted["profile_fingerprint"] == profile.fingerprint
    assert persisted["ruleset_fingerprint"] == rule_set.fingerprint
    assert persisted["head_sha"] == "head-1"
    assert persisted["project_id"] == 34 and persisted["mr_iid"] == 7
