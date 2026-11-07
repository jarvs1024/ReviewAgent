"""回放基线门禁：固定合成 case 重跑真实门面，产出不得偏离基线.

case 数据在 `tests/replay/cases/<name>/{diff.patch,responses.json}`：

* `diff.patch` 是真实 unified diff（走门面的 diff 解析与 planner）；
* `responses.json` 是 `{file: {summary_md, suggestions}}`，即「LLM 对该文件的
  固定回答」——`_call_chunk` 只做查表，不发起任何网络 / 子进程调用。

断言（spec 回放门禁）：

* suggestion 总数相对基线偏差 ≤10%（基线为 0 时必须恰好为 0）；
* 每个 severity 的占比绝对偏差 ≤2 个百分点；
* 基线里的 high 条目（`file|header`）必须全部仍在产出中。

所有外部依赖（git / rg / LLM / telemetry 落盘）都在用例内替换或隔离，
因此同一份基线在任意机器上都必须稳定通过。
"""
from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from reviewagent import config as config_module
from reviewagent.commands.improve import ImproveCommand
from reviewagent.llm.base import LLMResult
from reviewagent.scenario import profile as profile_module
from reviewagent.scenario.profile import load_profile
from reviewagent.scenario.rules import resolver
from reviewagent.settings.schema import SCHEMA
from reviewagent.telemetry import store as store_module
from reviewagent.telemetry.store import Store

REPLAY_ROOT = Path(__file__).resolve().parent
MANIFEST = json.loads((REPLAY_ROOT / "manifest.json").read_text(encoding="utf-8"))
CASES = {case["name"]: case for case in MANIFEST["cases"]}
SEVERITIES = ("high", "medium", "low")
_DIFF_HEADER = re.compile(r"^diff --git a/(.+) b/(.+)$", re.MULTILINE)
_BATCH_FILE = re.compile(r"^- `([^`]+)`$", re.MULTILINE)


def _materialize_worktree(diff_text: str, worktree: Path) -> dict[str, str]:
    """按 diff 里的文件路径在 worktree 落地同名文件（20 行占位内容）."""
    paths: dict[str, str] = {}
    for _, new_path in _DIFF_HEADER.findall(diff_text):
        paths[new_path] = new_path
        target = worktree / new_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "\n".join(f"value_{i} = {i}" for i in range(1, 21)) + "\n",
            encoding="utf-8",
        )
    return paths


def _case_responses(name: str) -> dict[str, dict]:
    path = REPLAY_ROOT / "cases" / name / "responses.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


_SCENARIO_SPECS = tuple(spec for spec in SCHEMA if spec.scope == "scenario")


def _pin_scenario_plane(monkeypatch) -> None:
    """把场景平面钉回 SCHEMA 默认值，并隔离开发者 env（回放密闭性）.

    `IMPROVE_MIN_SCORE=50` 这类场景 env 会先进入 import 时的 `_current`；
    `load_profile` 处理 profile 映射字段时还会经 `spec_env_override` 二次
    读取 env。只复位 `_current` 不足以密闭，所以这里同时把场景字段的 env
    从进程环境中摘掉（monkeypatch 负责测试结束后还原），再让 profile 的
    preset 覆盖默认值。`conftest._restore_config_snapshots` 负责复原全局
    `_current` / `_EXPLICIT_ENV`。
    """
    defaults = {spec.name: spec.default for spec in _SCENARIO_SPECS}
    config_module._current = replace(config_module.current(), **defaults)
    for spec in _SCENARIO_SPECS:
        for env_name in (spec.env, *spec.aliases):
            if env_name:
                monkeypatch.delenv(env_name, raising=False)


def _run_case(case: dict, monkeypatch, tmp_path: Path) -> dict:
    name = case["name"]
    diff_text = (REPLAY_ROOT / "cases" / name / "diff.patch").read_text(encoding="utf-8")
    responses = _case_responses(name)

    # 先钉死场景平面（SCHEMA 默认），再加载真实默认场景让 preset 覆写.
    _pin_scenario_plane(monkeypatch)
    profile = load_profile("ssd-python-autotest")
    monkeypatch.setattr(profile_module, "_active", profile)
    monkeypatch.setattr(resolver, "_ACTIVE", resolver.assemble_base(profile))

    worktree = tmp_path / name
    worktree.mkdir(parents=True)
    _materialize_worktree(diff_text, worktree)
    diff_file = worktree / ".diff"
    diff_file.write_text(diff_text, encoding="utf-8")

    previous_store = store_module._store
    store_module._store = Store(tmp_path / f"{name}.db")

    def fake_chunk(self, prompt, ws, file_path):
        if file_path.startswith("batch("):
            files = [fp for fp in _BATCH_FILE.findall(prompt) if fp in responses]
        else:
            files = [file_path]
        suggestions = [
            suggestion
            for fp in files
            for suggestion in (responses.get(fp) or {}).get("suggestions", [])
        ]
        summary = "\n\n".join(
            (responses.get(fp) or {}).get("summary_md", "")
            for fp in files
            if (responses.get(fp) or {}).get("summary_md")
        )
        return LLMResult(data={"summary_md": summary, "suggestions": suggestions})

    cmd = ImproveCommand.__new__(ImproveCommand)
    cmd.project_id, cmd.mr_iid = case["project_id"], case["mr_iid"]
    cmd.repo_context = ""
    cmd.ws = SimpleNamespace(worktree=worktree, diff_file=diff_file)
    cmd._last_oc_result = None
    cmd._get_mr_head_sha = lambda: case["head_sha"]
    cmd._collect_cross_file_refs_for_mr = (
        lambda files, diff_by_file, worktree_path, max_refs_per_file=None: {}
    )
    cmd._call_chunk = fake_chunk.__get__(cmd, ImproveCommand)

    try:
        return cmd._call_agent(cmd.ws)
    finally:
        store_module._store = previous_store
        profile_module.deactivate()


def test_manifest_covers_three_synthetic_cases():
    assert MANIFEST["source"] == "synthetic"
    assert MANIFEST["derived_from"] == "tests/golden"
    assert len(CASES) == 3
    case_dirs = {p.parent.name for p in (REPLAY_ROOT / "cases").glob("*/diff.patch")}
    assert case_dirs == set(CASES)
    for name in CASES:
        assert (REPLAY_ROOT / "cases" / name / "responses.json").is_file()
        responses = _case_responses(name)
        seen: set[tuple[str, int]] = set()
        for fp, payload in responses.items():
            for suggestion in payload.get("suggestions", []):
                assert suggestion["file"] == fp
                key = (fp, suggestion["start_line"])
                # merge_chunks 按 file:start_line 去重 — 回放数据必须每行唯一.
                assert key not in seen, f"duplicate suggestion slot in {name}: {key}"
                seen.add(key)


def _assert_matches_baseline(name: str, suggestions: list[dict]) -> None:
    """spec 回放门禁的全部断言（总数 / severity 占比 / high 条目）."""
    case = CASES[name]
    baseline = case["baseline"]

    # 1) suggestion 总数偏差.
    actual_count = len(suggestions)
    expected_count = baseline["suggestion_count"]
    tolerance = MANIFEST["tolerance"]["suggestion_count_relative"]
    if expected_count == 0:
        assert actual_count == 0
    else:
        deviation = abs(actual_count - expected_count) / expected_count
        assert deviation <= tolerance, (
            f"{name}: suggestion count {actual_count} deviates "
            f"{deviation:.1%} from baseline {expected_count}"
        )

    # 2) severity 占比偏差（绝对百分点）.
    actual_counts = {sev: 0 for sev in SEVERITIES}
    for suggestion in suggestions:
        sev = (suggestion.get("severity") or "medium").lower()
        if sev in actual_counts:
            actual_counts[sev] += 1
    baseline_counts = {
        sev: int((baseline["severity"] or {}).get(sev, 0)) for sev in SEVERITIES
    }
    baseline_total = sum(baseline_counts.values())
    actual_total = sum(actual_counts.values())
    max_pp = MANIFEST["tolerance"]["severity_share_pp"]
    for sev in SEVERITIES:
        actual_share = actual_counts[sev] / actual_total if actual_total else 0.0
        expected_share = baseline_counts[sev] / baseline_total if baseline_total else 0.0
        assert abs(actual_share - expected_share) * 100 <= max_pp, (
            f"{name}: {sev} share {actual_share:.2%} deviates from "
            f"baseline {expected_share:.2%}"
        )

    # 3) 基线的 high 条目必须全部仍在产出中.
    produced_high = {
        f"{suggestion.get('file')}|{suggestion.get('header')}"
        for suggestion in suggestions
        if (suggestion.get("severity") or "").lower() == "high"
    }
    for item in baseline["high_items"]:
        assert item in produced_high, f"{name}: missing baseline high item {item!r}"


@pytest.mark.parametrize("name", sorted(CASES))
def test_replay_matches_baseline(name, monkeypatch, tmp_path):
    result = _run_case(CASES[name], monkeypatch, tmp_path)
    _assert_matches_baseline(name, result["suggestions"])


def test_replay_ignores_scenario_env_override(monkeypatch, tmp_path):
    """开发者 env（IMPROVE_MIN_SCORE=50）不得改变回放结果.

    同时模拟「env 在 import 期已进入 `_current`」的污染：若
    `_pin_scenario_plane` 不再复位 `_current`，这里的断言会失败。
    """
    name = sorted(CASES)[0]
    case = CASES[name]
    default_min_score = next(
        spec.default for spec in _SCENARIO_SPECS if spec.name == "improve_min_score"
    )

    baseline_run = _run_case(case, monkeypatch, tmp_path / "clean")

    monkeypatch.setenv("IMPROVE_MIN_SCORE", "50")
    config_module._current = replace(config_module.current(), improve_min_score=50)
    overridden = _run_case(case, monkeypatch, tmp_path / "override")

    assert config_module.current().improve_min_score == default_min_score
    assert overridden["suggestions"] == baseline_run["suggestions"]
    _assert_matches_baseline(name, overridden["suggestions"])
