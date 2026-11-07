"""`reviewagent/review/runner.py` + ImproveCommand 薄壳门面回归测试."""
from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace

from reviewagent.commands.improve import ImproveCommand
from reviewagent.config import current
from reviewagent.review.runner import Callbacks, RunContext, run


def test_new_without_init_can_resolve_dependencies():
    cmd = ImproveCommand.__new__(ImproveCommand)
    cmd.project_id, cmd.mr_iid = 1, 2
    cmd.repo_context = ""
    cmd.ws = SimpleNamespace(worktree=None, diff_file=None)
    profile, rule_set, policy = cmd._lazy_deps()
    assert profile.name == "ssd-python-autotest"
    assert rule_set.verbatim_body
    assert policy.keywords()


def _callbacks(**overrides) -> Callbacks:
    kwargs = dict(
        split_diff=lambda diff_file, files: {},
        diff_line_map=lambda: {},
        read_lines=lambda fp: [],
        read_line_count=lambda fp, ws: -1,
        stat_bytes=lambda fp: -1,
        blob_sha=lambda fp, ws: None,
        head_sha=lambda: None,
        new_files=lambda diff_file: set(),
        collect_cross_refs=lambda files, diff_by_file, worktree: {},
        build_chunk_prompt=lambda *a, **kw: "",
        build_batch_prompt=lambda *a, **kw: "",
        call_chunk=lambda prompt, ws, fp: None,
        build_user_prompt=lambda: "",
        super_call_agent=lambda ws: {},
        set_token_summary=lambda p, c, m, cost: None,
    )
    kwargs.update(overrides)
    return Callbacks(**kwargs)


def _ctx(tmp_path: Path, *, diff_line_map, cfg=None, **callback_overrides) -> RunContext:
    diff_file = tmp_path / ".diff"
    diff_file.write_text("", encoding="utf-8")
    return RunContext(
        project_id=34, mr_iid=999,
        ws=SimpleNamespace(worktree=tmp_path, diff_file=diff_file),
        repo_context="",
        cfg=cfg or dataclasses.replace(current(), review_exclude_extensions=()),
        profile=SimpleNamespace(name="test-profile", language="fake", scoring={}, rule_keys={}),
        rule_set=SimpleNamespace(verbatim_body=""),
        dynamic_rules=None,
        policy=SimpleNamespace(),
        callbacks=_callbacks(diff_line_map=diff_line_map, **callback_overrides),
    )


def test_run_empty_diff_returns_skip_summary(tmp_path):
    ctx = _ctx(tmp_path, diff_line_map=lambda: {})
    result = run(ctx)
    assert result == {
        "summary_md": "## 改进总览\n\n无代码文件变更，跳过检视。",
        "suggestions": [],
    }
    assert ctx.review_mode == "business"


def test_run_test_only_diff_reports_skipped_paths(tmp_path):
    cfg = dataclasses.replace(
        current(),
        review_exclude_extensions=(),
        improve_review_mode="auto",
        improve_skip_test_paths=("tests/",),
    )
    ctx = _ctx(
        tmp_path,
        diff_line_map=lambda: {"tests/test_x.py": {1}},
        cfg=cfg,
        split_diff=lambda diff_file, files: {fp: "" for fp in files},
    )
    result = run(ctx)
    assert result == {
        "summary_md": (
            "## 改进总览\n\n> ⏭️ 以下 1 个测试/配置文件跳过检视: tests/test_x.py"
        ),
        "suggestions": [],
    }
    assert ctx.review_mode == "test"


def test_facade_merge_chunks_delegates(monkeypatch):
    from reviewagent.commands import improve as improve_module

    monkeypatch.setattr(
        improve_module, "config",
        dataclasses.replace(current(), improve_min_score=50),
    )
    results = [{
        "summary_md": "",
        "suggestions": [{
            "file": "a.py", "start_line": 3, "severity": "medium",
            "header": "h", "rationale": "why", "label": "potential bug",
        }],
    }]

    filtered = ImproveCommand._merge_chunks(results)
    assert filtered == {"summary_md": "## 改进总览\n\n未发现问题。", "suggestions": []}

    monkeypatch.setattr(
        ImproveCommand, "_score_suggestion", staticmethod(lambda s: 100),
    )
    kept = ImproveCommand._merge_chunks(results)
    assert kept["summary_md"] == (
        "## 改进总览\n\n- **`a.py`**L3 — **h** [MEDIUM/potential bug]: why"
    )
    assert [s["file"] for s in kept["suggestions"]] == ["a.py"]
