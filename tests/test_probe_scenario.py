"""探针场景：证明「新增场景 = profile 数据 + 语言策略」即可跑通全链路.

本测试**不修改任何引擎代码**：只在 tests/fixtures/profiles/probe-scenario/ 下
提供 profile.yaml / prompts / 规则包，并在测试内注册一个 `probe` 语言策略
（GenericPolicy 子类，仅覆盖 `render_source_block` / `classify_file` 两个
语言差异钩子），然后走真实门面 `ImproveCommand._call_agent` → runner →
planner / context / assembly / validation 的完整路径。

关键断言：

* 两个 `.probe` 文件都进入 LLM 调用，产出 ≥1 条 suggestion；
* 捕获的 prompt 来自 fixture 模板（`## 探针检视文件（probe-scenario）`），
  规则块来自 fixture 规则包（`PROBE-R-001`）；
* 源码块使用探针策略的自定义行号格式 `L<line>:<text>`（引擎未感知语言）.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from reviewagent.commands.improve import ImproveCommand
from reviewagent.llm.base import LLMResult
from reviewagent.review.policies import registry
from reviewagent.review.policies.generic import GenericPolicy
from reviewagent.review.types import FileContext, FileKind
from reviewagent.scenario import profile as profile_module
from reviewagent.scenario.profile import load_profile
from reviewagent.scenario.rules import resolver
from reviewagent.telemetry import store as store_module
from reviewagent.telemetry.store import Store

PROBE_ROOT = Path(__file__).resolve().parent / "fixtures" / "profiles"
PROFILE_NAME = "probe-scenario"

ALPHA_SOURCE = "def alpha():\n    return 'alpha'\n"
BETA_SOURCE = "def beta():\n    return 'beta'\n"

DIFF_ALPHA = (
    "diff --git a/alpha.probe b/alpha.probe\n"
    "--- a/alpha.probe\n"
    "+++ b/alpha.probe\n"
    "@@ -1,2 +1,2 @@\n"
    " def alpha():\n"
    "-    return 'old'\n"
    "+    return 'alpha'\n"
)
DIFF_BETA = (
    "diff --git a/beta.probe b/beta.probe\n"
    "--- a/beta.probe\n"
    "+++ b/beta.probe\n"
    "@@ -1,2 +1,2 @@\n"
    " def beta():\n"
    "-    return 'old'\n"
    "+    return 'beta'\n"
)
LINE_MAP = {"alpha.probe": {2}, "beta.probe": {2}}
DIFF_BY_FILE = {"alpha.probe": DIFF_ALPHA, "beta.probe": DIFF_BETA}


class ProbePolicy(GenericPolicy):
    """探针语言策略：只覆盖两个语言差异钩子，其余全部委托 GenericPolicy."""

    language = "probe"

    def render_source_block(self, ctx: FileContext, strategy: str, budget: int) -> str:
        # 刻意使用与内建策略完全不同的行号格式，验证引擎不感知语言.
        return "\n".join(
            f"L{ctx.start_line + i}:{line}" for i, line in enumerate(ctx.lines)
        )

    def classify_file(self, path: str) -> FileKind:
        if (path or "").endswith(".probe"):
            return FileKind.SOURCE
        return super().classify_file(path)


def _probe_factory(*, profile, cfg, read_lines):
    return ProbePolicy(profile=profile, cfg=cfg, read_lines=read_lines)


def test_probe_profile_is_data_only():
    """探针 profile 自包含：不 extends 默认场景，也无 dynamic_rules."""
    profile = load_profile(PROFILE_NAME, root=PROBE_ROOT)
    try:
        assert profile.name == PROFILE_NAME
        assert profile.language == "probe"
        assert profile.dynamic_rules is None
        assert profile.rules == ("rules/probe_rules.md",)
        for rel in profile.prompts.values():
            assert (profile.root / rel).is_file()
        rule_set = resolver.assemble_base(profile)
        assert [rule.key for rule in rule_set.rules] == ["PROBE-R-001"]
    finally:
        profile_module.deactivate()


def test_probe_scenario_runs_end_to_end(monkeypatch, tmp_path):
    profile = load_profile(PROFILE_NAME, root=PROBE_ROOT)
    assert profile.name == PROFILE_NAME

    # 固定激活 profile / 规则集（conftest 会在用例后恢复全局配置）.
    monkeypatch.setattr(profile_module, "_active", profile)
    monkeypatch.setattr(resolver, "_ACTIVE", resolver.assemble_base(profile))

    # 注册探针策略（用例内注册、finally 移除，避免污染其他用例）.
    registry.register("probe", _probe_factory)

    # 隔离 telemetry：不让 file_review_state 写仓库 data/.
    store_module._store = Store(tmp_path / "telemetry.db")

    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "alpha.probe").write_text(ALPHA_SOURCE, encoding="utf-8")
    (worktree / "beta.probe").write_text(BETA_SOURCE, encoding="utf-8")
    diff_file = worktree / ".diff"
    diff_file.write_text(DIFF_ALPHA + DIFF_BETA, encoding="utf-8")

    prompts: dict[str, str] = {}

    def fake_chunk(self, prompt, ws, file_path):
        prompts[file_path] = prompt
        return LLMResult(
            data={
                "summary_md": "",
                "suggestions": [{
                    "file": file_path,
                    "start_line": 2,
                    "severity": "high",
                    "header": "探针占位实现",
                    "rationale": (
                        "PROBE-R-001: 探针文件包含 TODO 占位实现，"
                        "请补齐真实逻辑后再提交评审。"
                    ),
                    "label": "potential bug",
                    "existing_code": "    return 'old'",
                    "improved_code": "    return 'new'",
                }],
            },
        )

    # 门面周边 IO 全部替换为内存数据（不碰 git / rg / LLM）.
    cmd = ImproveCommand.__new__(ImproveCommand)
    cmd.project_id, cmd.mr_iid = 7, 8
    cmd.repo_context = ""
    cmd.ws = SimpleNamespace(worktree=worktree, diff_file=diff_file)
    cmd._last_oc_result = None
    cmd._diff_line_map = lambda: dict(LINE_MAP)
    cmd._split_diff_by_file = lambda diff_path, files: dict(DIFF_BY_FILE)
    cmd._collect_cross_file_refs_for_mr = (
        lambda files, diff_by_file, worktree_path, max_refs_per_file=None: {}
    )
    cmd._call_chunk = fake_chunk.__get__(cmd, ImproveCommand)

    try:
        result = cmd._call_agent(cmd.ws)
    finally:
        registry._FACTORIES.pop("probe", None)
        store_module._store = None
        profile_module.deactivate()

    assert len(result["suggestions"]) >= 1
    assert {s["file"] for s in result["suggestions"]} == {"alpha.probe", "beta.probe"}
    assert set(prompts) == {"alpha.probe", "beta.probe"}

    for prompt in prompts.values():
        # prompt 框架来自 fixture 模板.
        assert "## 探针检视文件（probe-scenario）" in prompt
        # 规则块来自 fixture 规则包.
        assert "PROBE-R-001" in prompt
        # 源码块用探针策略的自定义行号格式（引擎只透传 policy 片段）.
        assert "L1:" in prompt

    assert "L2:    return 'alpha'" in prompts["alpha.probe"]
    assert "L2:    return 'beta'" in prompts["beta.probe"]
