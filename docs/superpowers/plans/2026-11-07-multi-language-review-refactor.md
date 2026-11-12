# ReviewAgent 多语言检视框架重构（首期 Python-only）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变现网 Python 检视行为与 prompt 字节的前提下，把 `commands/improve.py` 的单体检视逻辑重构为「场景包（Profile）+ 静态规则层 + 动态规则层 + 语言无关引擎 + Python policy」的分层框架，并保留 C/C++ 的加性扩展接口。

**Architecture:** 新增 L0 `settings/`（单一 schema/binder/指纹）与 `scenario/`（profile、prompt、静态规则、动态规则、仓库规则）；把 `improve.py` 拆为 `review/{planner,context,assembly,runner,validation,publishing,dynamic}/`，语言差异全部收敛到 `review/policies/python.py`；`commands/improve.py` 只保留薄壳门面与既有测试依赖的私有方法/常量。默认 profile `ssd-python-autotest` 的 prompt 渲染结果、规则正文、建议校验与发布语义必须与重构前逐字节/逐判定一致。

**Tech Stack:** Python 3.12、FastAPI、RQ/Redis、SQLite、loguru、python-frontmatter、PyYAML、pytest、ruff。

**Spec:** [2026-11-07-multi-language-review-design.md](../specs/2026-11-07-multi-language-review-design.md)（commit `586c2c8`）

## Global Constraints

- 只实现 Python 场景；不新增 `policies/cpp.py`、C/C++ 规则包、C/C++ 测试或目录。C/C++ 只保留 profile/language/policy 注册接口。
- 默认 profile 为 `ssd-python-autotest`；`tests/conftest.py` 不设置 `REVIEW_PROFILE`，必须走默认回退。
- 现有测试的断言零改动；允许新增测试与 fixture，不允许修改既有断言来「适配」重构。
- 默认配置下，`_build_chunk_prompt` / `_build_batch_prompt` / `_build_user_prompt` 的渲染结果与重构前逐字节一致；`_general_rules_block` 正文 checksum = `84ed4563f513be9589e8ab4aa97c49d2699bc226348377455dd65a8b2fa9712c`。
- `ImproveCommand._collect_cross_file_refs_for_mr(files, diff_by_file, wt)` 保持 3 个位置参数（测试以 lambda 覆盖）；`_merge_chunks` / `_normalise_suggestion` / `_score_suggestion` 保持 staticmethod 签名。
- `ImproveCommand.__new__(ImproveCommand)` + 手工赋少量属性 + 实例 monkeypatch 必须继续可用；新增依赖一律惰性解析。
- 测试会 `monkeypatch.setattr("reviewagent.commands.improve.config", _cfg)`；门面必须在调用时读取模块级 `config`，`review/**` 不得直接读这些测试旋钮。
- 不新增网络/系统依赖；`pyyaml` 以显式依赖写入（现由 `python-frontmatter` 传递安装）。
- 每个任务结束必须 `pytest` 全绿、`ruff check reviewagent tests` 通过，并单独提交。
- 严禁提交工作区现有未跟踪产物：`ppt-work/`、`r10-test-rb/`、`reviewagent/commands/improve.py.bak3.tmp`、`improve.py.bak4.tmp`、`scripts/round_test/`、`tmp-clones/`。

## 兼容铁律（每个任务都要检查）

| # | 不变量 | 验证方式 |
|---|---|---|
| C1 | 默认 profile 下 prompt 字节不变 | `tests/golden/test_prompt_golden.py`（Task 9 起） |
| C2 | 通用规则正文 checksum 不变 | `tests/test_rule_checksum.py`（Task 0） |
| C3 | 既有测试断言不变且全绿 | `pytest` |
| C4 | 配置无 env 时的默认行为不变 | `test_settings_defaults` + `test_config_provider_defaults` |
| C5 | 单文件捷径仍走可被 patch 的 `_build_user_prompt` | `test_improve_strategy.py` 单文件用例 |
| C6 | 动态证据回调用 3 个位置参数 | `test_improve_strategy.py` / `test_improve_alignment.py` |
| C7 | `config` 晚绑定（monkeypatch 生效） | 预算/策略测试 |
| C8 | 无 C/C++ 代码进入 `review/` 或 `scenario/` | `tests/architecture/test_no_language_literals.py` |

---

## 文件结构图（目标态）

```
reviewagent/
├── settings/
│   ├── schema.py            # FieldSpec + SCHEMA（默认值唯一来源）
│   └── binder.py            # env 解析 / profile 值绑定 / 指纹
├── scenario/
│   ├── profile.py           # Profile 模型、preset 合并、instance.json、单例
│   ├── prompts.py           # profile prompt 加载 + {{placeholder}} 严格渲染
│   ├── dynamic.py           # dynamic_rules.yaml 加载/校验
│   └── rules/
│       ├── model.py         # Rule / RulePack
│       ├── parser.py        # Markdown 规则包解析（verbatim/structured）
│       ├── resolver.py      # RuleSet / 优先级合并 / 覆盖 / 禁用 / 命名空间
│       ├── keys.py          # KeyPatterns / extract / normalize_keys
│       ├── render.py        # verbatim/structured 渲染
│       └── repo.py          # 仓库规则（默认分支）+ 请求级 overlay
├── review/
│   ├── types.py             # FileKind / FileContext / Suggestion / RiskVerdict
│   ├── policy.py            # LanguagePolicy Protocol
│   ├── policies/
│   │   ├── registry.py      # 语言注册表（惰性 import）
│   │   ├── generic.py       # GenericPolicy 默认实现
│   │   └── python.py        # PythonPolicy（唯一语言出口）
│   ├── planner.py           # 过滤 / 优先级 / 抽样 / 分批 / 策略 / 增量
│   ├── context.py           # 源码块 / 动态证据段 / worktree 约束读取
│   ├── assembly.py          # chunk/batch prompt 纯装配
│   ├── runner.py            # 单次 run 编排（并行 / 合并 / 预算）
│   ├── validation.py        # 行号锚定 / 去重 / 规则键归一
│   ├── publishing.py        # 发布 / cohort / supersede / 总览
│   ├── dynamic/
│   │   ├── model.py         # DynamicRuleSet / DynamicEvidence
│   │   ├── registry.py      # provider 注册
│   │   ├── runner.py        # 超时 / 降级 / 证据分发
│   │   └── providers/cross_file_refs.py
│   └── manifest.py          # 运行指纹
├── profiles/
│   ├── _presets/python-autotest.yaml
│   ├── _shared/prompts/...
│   └── ssd-python-autotest/
│       ├── profile.yaml
│       ├── dynamic_rules.yaml
│       ├── prompts/{improve,describe,weekly_*,dynamic/*}.md
│       ├── rules/general_rules.md
│       └── reporting/rule_actions.yaml
├── prompts/loader.py        # 兼容 re-export
├── repo_context.py          # 兼容 re-export
├── cli.py                   # config check/show, rules check
└── __main__.py
tests/
├── golden/prompts/ssd-python-autotest/*.txt
├── golden/manifest.json
├── architecture/
├── security/
├── fixtures/profiles/probe-scenario/
└── replay/
```

---

## Task 0: 基线捕获与兼容护栏

**Files:**
- Create: `scripts/capture_prompt_goldens.py`
- Create: `tests/golden/prompts/ssd-python-autotest/*.txt`（脚本生成）
- Create: `tests/golden/manifest.json`（脚本生成）
- Test: `tests/test_rule_checksum.py`

**Interfaces:**
- Produces: `tests/golden/manifest.json`，供 Task 9 的 golden 对比使用。
- Produces: `RULE_BODY_SHA256` 常量（仅测试使用）。

- [ ] **Step 1: 写 golden 捕获脚本**

```python
# scripts/capture_prompt_goldens.py
"""从重构前实现捕获 prompt 基线。仅在 Task 0 运行一次；结果提交到 tests/golden/。"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

_DATA = tempfile.mkdtemp(prefix="ra-golden-")
os.environ.setdefault("REVIEWAGENT_DATA_DIR", _DATA)
os.environ.setdefault("GITLAB_URL", "http://localhost")
os.environ.setdefault("GITLAB_PERSONAL_ACCESS_TOKEN", "x")
os.environ.setdefault("GITLAB_WEBHOOK_SECRET", "x")

from reviewagent.commands.improve import ImproveCommand  # noqa: E402

DIFF = (
    "diff --git a/core.py b/core.py\n"
    "--- a/core.py\n+++ b/core.py\n"
    "@@ -1,3 +1,4 @@\n import os\n+import json\n def f():\n-    return 1\n+    return 2\n"
)
SOURCE = "import os\nimport json\ndef f():\n    return 2\n"
VALID = {3, 5, 6}
REFS = [{"file": "tests/test_core.py", "line": 12, "ident": "f", "content": "assert f() == 2"}]
PRIOR = [{"severity": "high", "header": "资源未关闭", "label": "potential bug"}]


def _cmd(worktree: Path, repo_context: str = "") -> ImproveCommand:
    (worktree / "core.py").write_text(SOURCE)
    (worktree / "tests").mkdir()
    (worktree / "tests" / "test_core.py").write_text("from core import f\nassert f() == 2\n")
    (worktree / ".diff").write_text(DIFF)
    cmd = ImproveCommand.__new__(ImproveCommand)
    cmd.project_id = 1
    cmd.mr_iid = 1
    cmd.ws = SimpleNamespace(worktree=worktree, diff_file=worktree / ".diff")
    cmd.repo_context = repo_context
    cmd._last_oc_result = None
    return cmd


def main() -> None:
    out = Path("tests/golden/prompts/ssd-python-autotest")
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        wt = Path(td)
        cmd = _cmd(wt)
        cases = {
            "chunk_full.txt": cmd._build_chunk_prompt("core.py", DIFF, VALID, cmd.ws, cross_file_refs=[], strategy="full"),
            "chunk_partial_refs.txt": cmd._build_chunk_prompt("core.py", DIFF, VALID, cmd.ws, cross_file_refs=REFS, strategy="partial"),
            "chunk_patch_prior.txt": cmd._build_chunk_prompt("core.py", DIFF, VALID, cmd.ws, cross_file_refs=[], strategy="patch", prior_issues=PRIOR),
            "batch.txt": cmd._build_batch_prompt(
                ["core.py"], {"core.py": DIFF}, {"core.py": VALID}, cmd.ws,
                {"core.py": REFS}, {"core.py": "patch"}, {"core.py": PRIOR},
            ),
            "user_prompt.txt": cmd._build_user_prompt(),
        }
        repo_cmd = _cmd(wt, repo_context="## AGENTS.md\n\n- 必须使用 with\n")
        cases["chunk_repo_ctx.txt"] = repo_cmd._build_chunk_prompt(
            "core.py", DIFF, VALID, repo_cmd.ws, cross_file_refs=[], strategy="full",
        )
        manifest = {}
        for name, text in cases.items():
            data = text.encode("utf-8")
            (out / name).write_bytes(data)
            manifest[name] = hashlib.sha256(data).hexdigest()
    Path("tests/golden/manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 运行脚本生成基线**

Run: `.venv/bin/python scripts/capture_prompt_goldens.py`
Expected: 打印 6 个 sha256，`tests/golden/prompts/ssd-python-autotest/` 下生成 6 个 `.txt`。

- [ ] **Step 3: 写规则正文 checksum 护栏测试**

```python
# tests/test_rule_checksum.py
import hashlib

RULE_BODY_SHA256 = "84ed4563f513be9589e8ab4aa97c49d2699bc226348377455dd65a8b2fa9712c"


def test_general_rules_block_checksum():
    from reviewagent.prompts.loader import load_block

    body = load_block("_general_rules_block")
    assert hashlib.sha256(body.encode("utf-8")).hexdigest() == RULE_BODY_SHA256
```

- [ ] **Step 4: 运行确认**

Run: `.venv/bin/pytest tests/test_rule_checksum.py -v`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add scripts/capture_prompt_goldens.py tests/golden tests/test_rule_checksum.py
git commit -m "test: 捕获重构前 prompt 基线与规则正文 checksum"
```

---

## Task 1: settings 单一 schema + binder + config 代理

**Files:**
- Create: `reviewagent/settings/__init__.py`
- Create: `reviewagent/settings/schema.py`
- Create: `reviewagent/settings/binder.py`
- Modify: `reviewagent/config.py`
- Modify: `pyproject.toml`（显式 `pyyaml>=6.0`）
- Test: `tests/test_settings_schema.py`, `tests/test_config_proxy.py`

**Interfaces:**
- Produces: `FieldSpec(name, type, default, env, scope, choices, minimum, maximum, secret, restart_required, doc, aliases)`。
- Produces: `SCHEMA: tuple[FieldSpec, ...]`、`SCHEMA_BY_NAME: dict[str, FieldSpec]`。
- Produces: `settings.from_env(environ: Mapping[str, str] | None = None) -> Config`。
- Produces: `config.apply_scenario(values: Mapping[str, Any]) -> None`、`config.current() -> Config`。
- 修改 `Config`：新增 `review_profile: str = ""`、`review_profile_root: str = ""`；所有类默认值统一为现网 `from_env` 回退值；`Config.from_env()` 委托 `settings.from_env()`。

- [ ] **Step 1: 写失败测试（schema 全覆盖 + 默认值唯一）**

```python
# tests/test_settings_schema.py
from dataclasses import fields

import pytest

from reviewagent.config import Config
from reviewagent.settings.binder import from_env
from reviewagent.settings.schema import SCHEMA, SCHEMA_BY_NAME

REQUIRED = {
    "GITLAB_URL": "http://localhost",
    "GITLAB_PERSONAL_ACCESS_TOKEN": "x",
    "GITLAB_WEBHOOK_SECRET": "x",
}


def test_schema_covers_every_config_field():
    assert {f.name for f in fields(Config)} == set(SCHEMA_BY_NAME)


def test_schema_names_are_unique():
    assert len(SCHEMA) == len(SCHEMA_BY_NAME)


def test_no_env_defaults_match_production_fallbacks():
    cfg = from_env(REQUIRED)
    assert cfg.improve_review_mode == "auto"
    assert cfg.improve_priority_weight_diff == 20.0
    assert cfg.improve_priority_weight_keyword == 25.0
    assert cfg.improve_priority_weight_density == 20.0
    assert cfg.improve_priority_weight_test_feature == 15.0
    assert cfg.improve_skip_test_paths == (
        "tests/", "/tests/", "test_", "_test.", ".test.", "conftest.py",
    )
    assert cfg.improve_keyword_paths == (
        "/services/", "/api/", "/core/", "/main.", "/models/", "/handlers/", "/routers/",
    )
    for name in ("improve_review_mode", "improve_priority_weight_diff", "improve_skip_test_paths"):
        assert getattr(cfg, name) == SCHEMA_BY_NAME[name].default


def test_env_bool_and_list_semantics():
    cfg = from_env({**REQUIRED, "IMPROVE_BATCH_ENABLED": "off", "PR_COMMANDS": "describe"})
    assert cfg.improve_batch_enabled is False
    assert cfg.pr_commands == ("describe",)
    with pytest.raises(ValueError, match="IMPROVE_BATCH_ENABLED"):
        from_env({**REQUIRED, "IMPROVE_BATCH_ENABLED": "maybe"})
```

```python
# tests/test_config_proxy.py
from reviewagent.config import apply_scenario, config


def test_proxy_sees_scenario_values_through_imported_reference():
    from reviewagent.commands.improve import config as improve_config

    apply_scenario({"improve_max_suggestions": 7})
    try:
        assert improve_config.improve_max_suggestions == 7
        assert config.improve_max_suggestions == 7
    finally:
        apply_scenario({"improve_max_suggestions": 15})


def test_apply_scenario_rejects_unknown_and_secret():
    import pytest

    with pytest.raises(KeyError):
        apply_scenario({"not_a_field": 1})
    with pytest.raises(PermissionError):
        apply_scenario({"gitlab_pat": "leak"})
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/pytest tests/test_settings_schema.py tests/test_config_proxy.py -v`
Expected: FAIL（`ModuleNotFoundError: reviewagent.settings`）。

- [ ] **Step 3: 实现 schema**

```python
# reviewagent/settings/schema.py
"""配置字段的唯一默认值来源。纯数据，不 import 上层模块。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

Scope = Literal["instance", "scenario"]


@dataclass(frozen=True)
class FieldSpec:
    name: str
    type: type
    default: Any
    env: str | None = None
    scope: Scope = "scenario"
    choices: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    secret: bool = False
    restart_required: bool = True
    doc: str = ""
    aliases: tuple[str, ...] = ()


def _profiles_root() -> str:
    return str(Path(__file__).resolve().parents[2] / "profiles")


SCHEMA: tuple[FieldSpec, ...] = (
    FieldSpec("gitlab_url", str, "", env="GITLAB_URL", scope="instance", secret=True, doc="GitLab 地址"),
    FieldSpec("gitlab_pat", str, "", env="GITLAB_PERSONAL_ACCESS_TOKEN", scope="instance", secret=True),
    FieldSpec("gitlab_webhook_secret", str, "", env="GITLAB_WEBHOOK_SECRET", scope="instance", secret=True),
    FieldSpec("gitlab_bot_username", str, "review-agent", env="GITLAB_BOT_USERNAME", scope="instance"),
    FieldSpec("gitlab_disable_bot_loop_check", bool, False, env="GITLAB_DISABLE_BOT_LOOP_CHECK", scope="instance"),
    FieldSpec("opencode_url", str, "http://localhost:4096", env="OPENCODE_URL", scope="instance"),
    FieldSpec("opencode_username", str, "opencode", env="OPENCODE_USERNAME", scope="instance"),
    FieldSpec("opencode_password", str, "", env="OPENCODE_PASSWORD", scope="instance", secret=True),
    FieldSpec("opencode_model", str, "deepseek/deepseek-v4-flash", env="OPENCODE_MODEL", scope="instance"),
    FieldSpec("opencode_timeout", int, 900, env="OPENCODE_TIMEOUT", scope="instance", minimum=1),
    FieldSpec("llm_provider", str, "qodercli", env="LLM_PROVIDER", scope="instance",
              choices=("opencode", "qodercli", "qoder-sdk")),
    FieldSpec("qodercli_node_path", str, "", env="QODERCLI_NODE_PATH", scope="instance"),
    FieldSpec("qodercli_js_path", str, "", env="QODERCLI_JS_PATH", scope="instance"),
    FieldSpec("qodercli_model", str, "DeepSeek-V4-Flash", env="QODERCLI_MODEL", scope="instance"),
    FieldSpec("qodercli_timeout", int, 600, env="QODERCLI_TIMEOUT", scope="instance", minimum=1),
    FieldSpec("qodercli_permission_mode", str, "", env="QODERCLI_PERMISSION_MODE", scope="instance"),
    FieldSpec("qodercli_max_turns", int, 20, env="QODERCLI_MAX_TURNS", scope="instance", minimum=0),
    FieldSpec("qodercli_fallback_model", str, "", env="QODERCLI_FALLBACK_MODEL", scope="instance"),
    FieldSpec("qoder_sdk_pat", str, "", env="QODER_SDK_PAT", scope="instance", secret=True),
    FieldSpec("qoder_sdk_edition", str, "intl", env="QODER_SDK_EDITION", scope="instance", choices=("intl", "cn")),
    FieldSpec("qoder_sdk_cli_path", str, "", env="QODER_SDK_CLI_PATH", scope="instance"),
    FieldSpec("qoder_sdk_model", str, "", env="QODER_SDK_MODEL", scope="instance"),
    FieldSpec("qoder_sdk_fallback_model", str, "", env="QODER_SDK_FALLBACK_MODEL", scope="instance"),
    FieldSpec("qoder_sdk_timeout", int, 600, env="QODER_SDK_TIMEOUT", scope="instance", minimum=1),
    FieldSpec("qoder_sdk_max_turns", int, 0, env="QODER_SDK_MAX_TURNS", scope="instance", minimum=0),
    FieldSpec("qoder_sdk_permission_mode", str, "", env="QODER_SDK_PERMISSION_MODE", scope="instance"),
    FieldSpec("qoder_sdk_disallowed_tools", tuple, ("write", "edit", "bash", "webfetch", "websearch"),
              env="QODER_SDK_DISALLOWED_TOOLS", scope="instance"),
    FieldSpec("redis_url", str, "redis://localhost:6379/0", env="REDIS_URL", scope="instance"),
    FieldSpec("rq_queue_name", str, "review", env="RQ_QUEUE_NAME", scope="instance"),
    FieldSpec("rq_weekly_queue_name", str, "review-weekly", env="RQ_WEEKLY_QUEUE_NAME", scope="instance"),
    FieldSpec("rq_worker_timeout", int, 1200, env="RQ_WORKER_TIMEOUT", scope="instance", minimum=1),
    FieldSpec("rq_worker_count", int, 3, env="RQ_WORKER_COUNT", scope="instance", minimum=1),
    FieldSpec("pr_commands", tuple, ("describe", "improve"), env="PR_COMMANDS", scope="instance"),
    FieldSpec("push_commands", tuple, ("describe", "improve"), env="PUSH_COMMANDS", scope="instance"),
    FieldSpec("data_dir", Path, Path("./data"), env="REVIEWAGENT_DATA_DIR", scope="instance"),
    FieldSpec("log_level", str, "INFO", env="REVIEWAGENT_LOG_LEVEL", scope="instance"),
    FieldSpec("review_profile", str, "", env="REVIEW_PROFILE", scope="instance"),
    FieldSpec("review_profile_root", str, _profiles_root(), env="REVIEW_PROFILE_ROOT", scope="instance"),
    FieldSpec("mr_cooldown_seconds", int, 60, env="MR_COOLDOWN_SECONDS", scope="instance", minimum=0),
    FieldSpec("max_review_calls_per_mr", int, 30, env="MAX_REVIEW_CALLS_PER_MR", scope="instance", minimum=0),
    FieldSpec("max_diff_chars", int, 50000, env="MAX_DIFF_CHARS", scope="scenario", minimum=1),
    FieldSpec("opencode_max_diff_chars", int, 20000, env="OPENCODE_MAX_DIFF_CHARS", scope="scenario", minimum=1),
    FieldSpec("repo_context_files", tuple, ("AGENTS.md",), env="REPO_CONTEXT_FILES", scope="scenario"),
    FieldSpec("repo_context_rules_dir", str, ".agents/rules", env="REPO_CONTEXT_RULES_DIR", scope="scenario"),
    FieldSpec("rule_key_prefix", str, "SSD", env="RULE_KEY_PREFIX", scope="scenario"),
    FieldSpec("repo_context_max_lines", int, 2000, env="REPO_CONTEXT_MAX_LINES", scope="scenario", minimum=1),
    FieldSpec("improve_parallel_workers", int, 3, env="IMPROVE_PARALLEL_WORKERS", scope="scenario", minimum=1),
    FieldSpec("improve_full_files", int, 20, env="IMPROVE_FULL_FILES", scope="scenario", minimum=0),
    FieldSpec("improve_full_source_max_bytes", int, 0, env="IMPROVE_FULL_SOURCE_MAX_BYTES", scope="scenario"),
    FieldSpec("improve_keyword_paths", tuple, (
        "/services/", "/api/", "/core/", "/main.", "/models/", "/handlers/", "/routers/",
    ), env="IMPROVE_KEYWORD_PATHS", scope="scenario"),
    FieldSpec("improve_skip_test_paths", tuple, (
        "tests/", "/tests/", "test_", "_test.", ".test.", "conftest.py",
    ), env="IMPROVE_SKIP_TEST_PATHS", scope="scenario"),
    FieldSpec("improve_partial_context_lines", int, 20, env="IMPROVE_PARTIAL_CONTEXT_LINES", scope="scenario", minimum=0),
    FieldSpec("improve_patch_context_lines", int, 3, env="IMPROVE_PATCH_CONTEXT_LINES", scope="scenario", minimum=0),
    FieldSpec("improve_max_suggestions", int, 15, env="IMPROVE_MAX_SUGGESTIONS", scope="scenario", minimum=0),
    FieldSpec("improve_max_suggestions_per_file", int, 5, env="IMPROVE_MAX_SUGGESTIONS_PER_FILE", scope="scenario", minimum=0),
    FieldSpec("improve_min_suggestions_per_file", int, 2, env="IMPROVE_MIN_SUGGESTIONS_PER_FILE", scope="scenario", minimum=0),
    FieldSpec("improve_min_score", int, 0, env="IMPROVE_MIN_SCORE", scope="scenario", minimum=0),
    FieldSpec("improve_min_severity", str, "", env="IMPROVE_MIN_SEVERITY", scope="scenario",
              choices=("", "critical", "high", "medium", "low")),
    FieldSpec("improve_review_mode", str, "auto", env="IMPROVE_REVIEW_MODE", scope="scenario",
              choices=("auto", "business", "test")),
    FieldSpec("improve_priority_weight_diff", float, 20.0, env="IMPROVE_PRIORITY_WEIGHT_DIFF", scope="scenario", minimum=0),
    FieldSpec("improve_priority_weight_keyword", float, 25.0, env="IMPROVE_PRIORITY_WEIGHT_KEYWORD", scope="scenario", minimum=0),
    FieldSpec("improve_priority_weight_density", float, 20.0, env="IMPROVE_PRIORITY_WEIGHT_DENSITY", scope="scenario", minimum=0),
    FieldSpec("improve_priority_weight_test_feature", float, 15.0, env="IMPROVE_PRIORITY_WEIGHT_TEST_FEATURE", scope="scenario", minimum=0),
    FieldSpec("improve_test_feature_keywords", tuple, (
        "pytest.fixture", "fixture(", "@fixture", "mock.patch", "MagicMock", "patch(",
        "parametrize", "@pytest.mark", "setup_module", "teardown_module", "setup_class",
        "teardown_class", "setUp", "tearDown", "assert ", "yield fixture", "pytest.raises",
        "contextmanager", "tmp_path", "caplog", "monkeypatch",
    ), env="IMPROVE_TEST_FEATURE_KEYWORDS", scope="scenario"),
    FieldSpec("review_exclude_extensions", tuple, (
        ".md", ".doc", ".docx", ".txt", ".rst", ".csv", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
    ), env="REVIEW_EXCLUDE_EXTENSIONS", scope="scenario"),
    FieldSpec("improve_max_files_to_review", int, 60, env="IMPROVE_MAX_FILES_TO_REVIEW", scope="scenario", minimum=1),
    FieldSpec("improve_new_file_full", bool, True, env="IMPROVE_NEW_FILE_FULL", scope="scenario"),
    FieldSpec("improve_test_sample_paths", tuple, (), env="IMPROVE_TEST_SAMPLE_PATHS", scope="scenario"),
    FieldSpec("improve_test_sample_max", int, 0, env="IMPROVE_TEST_SAMPLE_MAX", scope="scenario", minimum=0),
    FieldSpec("improve_batch_enabled", bool, True, env="IMPROVE_BATCH_ENABLED", scope="scenario"),
    FieldSpec("improve_batch_max_diff_lines", int, 60, env="IMPROVE_BATCH_MAX_DIFF_LINES", scope="scenario", minimum=1),
    FieldSpec("improve_batch_max_files", int, 5, env="IMPROVE_BATCH_MAX_FILES", scope="scenario", minimum=1),
    FieldSpec("improve_batch_max_total_lines", int, 300, env="IMPROVE_BATCH_MAX_TOTAL_LINES", scope="scenario", minimum=1),
    FieldSpec("improve_clip_diff_lines", int, 800, env="IMPROVE_CLIP_DIFF_LINES", scope="scenario", minimum=0),
)

SCHEMA_BY_NAME: dict[str, FieldSpec] = {spec.name: spec for spec in SCHEMA}
```

- [ ] **Step 4: 实现 binder**

```python
# reviewagent/settings/binder.py
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

from reviewagent.settings.schema import SCHEMA, SCHEMA_BY_NAME, FieldSpec

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}
_UNSET = object()

_EXPLICIT_ENV: frozenset[str] = frozenset()


class _Missing:
    pass


def explicit_env() -> frozenset[str]:
    return _EXPLICIT_ENV


def _fail(spec: FieldSpec, raw: Any, why: str) -> ValueError:
    return ValueError(f"config field '{spec.name}' (env={spec.env}): {why}: {raw!r}")


def coerce(spec: FieldSpec, raw: Any) -> Any:
    if raw is None:
        return _UNSET
    if isinstance(raw, str):
        raw = raw.strip()
        if spec.type is tuple:
            return tuple(x.strip() for x in raw.split(",") if x.strip())
        if raw == "":
            return _UNSET
        if spec.type is bool:
            low = raw.lower()
            if low in _TRUE:
                return True
            if low in _FALSE:
                return False
            raise _fail(spec, raw, "invalid bool (expected 1/0/true/false/yes/no/on/off)")
        if spec.type is int:
            try:
                return int(raw)
            except ValueError as exc:
                raise _fail(spec, raw, "invalid int") from exc
        if spec.type is float:
            try:
                return float(raw)
            except ValueError as exc:
                raise _fail(spec, raw, "invalid float") from exc
        return raw
    if spec.type is tuple and isinstance(raw, list):
        return tuple(raw)
    if spec.type is Path and isinstance(raw, str):
        return Path(raw)
    return raw


def _validate(spec: FieldSpec, value: Any) -> None:
    if spec.choices and value not in spec.choices:
        raise _fail(spec, value, f"not in {spec.choices}")
    if spec.minimum is not None and isinstance(value, (int, float)) and value < spec.minimum:
        raise _fail(spec, value, f"< minimum {spec.minimum}")
    if spec.maximum is not None and isinstance(value, (int, float)) and value > spec.maximum:
        raise _fail(spec, value, f"> maximum {spec.maximum}")


def from_env(environ: Mapping[str, str] | None = None):
    global _EXPLICIT_ENV
    src = dict(os.environ if environ is None else environ)
    kwargs: dict[str, Any] = {}
    explicit: set[str] = set()
    for spec in SCHEMA:
        value, is_explicit = spec.default, False
        if spec.env and spec.env in src:
            parsed = coerce(spec, src[spec.env])
            if parsed is not _UNSET:
                value, is_explicit = parsed, True
        for alias in spec.aliases:
            if alias in src:
                parsed = coerce(spec, src[alias])
                if parsed is not _UNSET:
                    value, is_explicit = parsed, True
        if spec.name == "data_dir" and not isinstance(value, Path):
            value = Path(value)
        _validate(spec, value)
        kwargs[spec.name] = value
        if is_explicit:
            explicit.add(spec.env or spec.name)
    _EXPLICIT_ENV = frozenset(explicit)
    from reviewagent.config import Config

    return Config(**kwargs)


def fingerprint(values: Mapping[str, Any]) -> str:
    payload = {}
    for name, value in values.items():
        spec = SCHEMA_BY_NAME[name]
        if spec.secret:
            payload[name] = "***"
        elif isinstance(value, Path):
            payload[name] = str(value)
        elif isinstance(value, tuple):
            payload[name] = list(value)
        else:
            payload[name] = value
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
```

- [ ] **Step 5: 改造 config.py**

```python
# reviewagent/config.py（保留 dataclass 字段；仅改默认值 + 末尾代理）
from typing import Any, Mapping

# Config 字段默认值逐项对齐 SCHEMA（Task 1 Step 3）：
#   improve_review_mode="auto"
#   improve_priority_weight_diff=20.0 / keyword=25.0 / density=20.0 / test_feature=15.0
#   improve_skip_test_paths=("tests/", "/tests/", "test_", "_test.", ".test.", "conftest.py")
#   improve_keyword_paths 保持现网默认（不含 /pages/）
# 其余字段保持当前值不变；from_env 统一委托 settings.binder.from_env()。

    @classmethod
    def from_env(cls) -> "Config":
        from reviewagent.settings.binder import from_env

        return from_env()


_current: Config = Config.from_env()


class _ConfigProxy:
    """模块级稳定引用；scenario 应用时只替换 _current，不重绑全局。"""

    __slots__ = ()

    def __getattr__(self, name: str) -> Any:
        return getattr(_current, name)

    def __repr__(self) -> str:
        return repr(_current)


config: Config = _ConfigProxy()  # type: ignore[assignment]


def current() -> Config:
    return _current


def apply_scenario(values: Mapping[str, Any]) -> None:
    global _current
    from dataclasses import replace

    from reviewagent.settings.schema import SCHEMA_BY_NAME
    from reviewagent.settings.binder import coerce

    updates: dict[str, Any] = {}
    for name, raw in values.items():
        spec = SCHEMA_BY_NAME[name]
        if spec.secret:
            raise PermissionError(f"scenario must not set secret field: {name}")
        value = coerce(spec, raw) if isinstance(raw, (str, int, float, bool)) else raw
        if value is not None:
            updates[name] = value
    _current = replace(_current, **updates)
```

- [ ] **Step 6: 运行全部测试并修正默认值漂移**

Run: `.venv/bin/pytest -q`
Expected: 全绿。若既有测试依赖旧类默认值（`review_mode="test"` 等），以 `from_env` 回退值为准，检查是否有测试显式构造 `Config()`；不得改测试断言。

- [ ] **Step 7: 提交**

```bash
git add reviewagent/settings reviewagent/config.py pyproject.toml tests/test_settings_schema.py tests/test_config_proxy.py
git commit -m "feat(settings): 单一 schema/binder 与可替换 config 代理"
```

---

## Task 2: scenario profile / prompt 加载 + preset + 数据迁移 + instance.json

**Files:**
- Create: `profiles/_presets/python-autotest.yaml`
- Create: `profiles/ssd-python-autotest/profile.yaml`
- Create: `profiles/ssd-python-autotest/prompts/{improve,describe,weekly_inspection_summary,weekly_change_summary,weekly_quality_scan}.md`
- Create: `profiles/ssd-python-autotest/prompts/_general_rules_block.md`（Task 5 删除的临时副本）
- Create: `profiles/ssd-python-autotest/dynamic_rules.yaml`
- Create: `profiles/ssd-python-autotest/reporting/rule_actions.yaml`
- Create: `reviewagent/scenario/__init__.py`, `reviewagent/scenario/profile.py`, `reviewagent/scenario/prompts.py`
- Modify: `reviewagent/prompts/loader.py`（委托 scenario.prompts）
- Test: `tests/test_scenario_profile.py`, `tests/test_scenario_prompts.py`

**Interfaces:**
- Produces: `Profile`（字段见下）、`load_profile(name: str | None = None, *, root: Path | None = None) -> Profile`、`active() -> Profile`、`active_or_load() -> Profile`、`load_and_activate() -> Profile`。
- Produces: `bind_instance(profile: Profile, data_dir: Path) -> None`。
- Produces: `scenario.prompts.load(name, profile=None) -> dict`、`available(profile=None) -> list[str]`、`load_block(name, profile=None) -> str`、`load_template(rel_path, profile=None, *, trim_trailing_newline=False) -> str`、`render_template(text, values, *, template_path) -> str`。
- `Profile.fingerprint: str`；`Profile.sources: dict[str, str]`。

- [ ] **Step 1: 迁移 prompt 文件（逐字节）**

```bash
mkdir -p profiles/ssd-python-autotest/prompts
cp reviewagent/prompts/improve.md profiles/ssd-python-autotest/prompts/improve.md
cp reviewagent/prompts/describe.md profiles/ssd-python-autotest/prompts/describe.md
cp reviewagent/prompts/weekly_inspection_summary.md profiles/ssd-python-autotest/prompts/
cp reviewagent/prompts/weekly_change_summary.md profiles/ssd-python-autotest/prompts/
cp reviewagent/prompts/weekly_quality_scan.md profiles/ssd-python-autotest/prompts/
cp reviewagent/prompts/_general_rules_block.md profiles/ssd-python-autotest/prompts/_general_rules_block.md
```

- [ ] **Step 2: 写 preset 与 profile**

```yaml
# profiles/_presets/python-autotest.yaml
schema_version: 1
name: python-autotest
language: python
file_filter:
  scope_paths: null
  include_extensions: null
  exclude_extensions: [.md, .doc, .docx, .txt, .rst, .csv, .png, .jpg, .jpeg, .gif, .svg, .ico]
  skip_paths: [tests/, /tests/, test_, _test., .test., conftest.py]
  priority_paths: [/services/, /api/, /core/, /main., /models/, /handlers/, /routers/]
scoring:
  review_mode: auto
  priority_weights: {diff: 20, keyword: 25, density: 20, test_feature: 15}
  test_paths: [tests/, test_*.py]
  test_sample_paths: []
  test_sample_max: 0
  feature_keywords: null
capabilities: {ast_validation: true, cross_file_search: true, apply_suggestion: true, full_source_analysis: true}
security: {worktree_containment: true, normalize_untrusted: true, prompt_hardening: false}
context: {partial_context_lines: 20, patch_context_lines: 3}
limits:
  max_suggestions: 15
  max_suggestions_per_file: 5
  min_suggestions_per_file: 2
  max_files_to_review: 60
  full_files: 20
  parallel_workers: 3
  clip_diff_lines: 800
  batch: {enabled: true, max_diff_lines: 60, max_files: 5, max_total_lines: 300}
repo_context: {files: [AGENTS.md], rules_dir: .agents/rules, max_lines: 2000}
prompts:
  improve: prompts/improve.md
  describe: prompts/describe.md
  weekly_inspection_summary: prompts/weekly_inspection_summary.md
  weekly_change_summary: prompts/weekly_change_summary.md
  weekly_quality_scan: prompts/weekly_quality_scan.md
rules: [rules/general_rules.md]
```

```yaml
# profiles/ssd-python-autotest/profile.yaml
schema_version: 1
name: ssd-python-autotest
description: SSD 自动化测试 Python 代码检视（现网行为）
extends: python-autotest
language: python
prompts:
  improve: prompts/improve.md
  describe: prompts/describe.md
  weekly_inspection_summary: prompts/weekly_inspection_summary.md
  weekly_change_summary: prompts/weekly_change_summary.md
  weekly_quality_scan: prompts/weekly_quality_scan.md
rules: [rules/general_rules.md]
dynamic_rules: dynamic_rules.yaml
rule_keys:
  builtin_pattern: '(?:R-OTHER-IMPACT:[a-z0-9_]+|R-OTHER:[a-z0-9_]+|R-[A-Z]+(?:-[A-Z0-9_]+)*)'
  free_prefixes: [R-OTHER-IMPACT, R-OTHER]
  repo_prefix: SSD
  display_prefix: SSD
rule_key_promotions:
  - when_any: [wildcard]
    to_key: SSD-RULE-FORBIDDEN-WILDCARD-IMPORT
  - when_all: ["from ", "import *"]
    to_key: SSD-RULE-FORBIDDEN-WILDCARD-IMPORT
reporting:
  weekly_title: "SSD自动化代码检视周报"
  actions_file: reporting/rule_actions.yaml
  repo_scan_extensions: [.py, .js, .ts, .go, .java, .rs, .rb, .sh]
  repo_scan_entry_files: [__init__.py, main.py, app.py, config.py, settings.py]
```

```yaml
# profiles/ssd-python-autotest/dynamic_rules.yaml
schema_version: 1
rules:
  - key: DYN-CROSS-FILE-REFS
    title: 跨文件引用
    provider: cross_file_refs
    enabled: true
    template: prompts/dynamic/cross_file_refs.md
    params:
      rg_file_type: py
      max_symbols_per_file: 12
      max_refs_per_file: 5
      hits_per_symbol: 3
      timeout_seconds: 20
    limits: {max_files: 60}
```

- [ ] **Step 3: 写 profile 模型与加载器**

```python
# reviewagent/scenario/profile.py
from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from reviewagent.config import apply_scenario
from reviewagent.logging_setup import logger
from reviewagent.settings.binder import explicit_env
from reviewagent.settings.schema import SCHEMA_BY_NAME

DEFAULT_PROFILE = "ssd-python-autotest"
_SHARED = "_shared"
_PRESETS = "_presets"
_SUPPORTED_SCHEMA = 1
_ALLOWED_PROFILE_KEYS = {
    "schema_version", "name", "description", "extends", "language", "prompts", "rules",
    "dynamic_rules", "rule_keys", "rule_key_promotions", "rule_overrides", "disable",
    "repo_context", "capabilities", "security", "file_filter", "scoring", "reporting",
    "limits", "context",
}


@dataclass(frozen=True)
class Profile:
    name: str
    language: str
    root: Path
    prompts: dict[str, str]
    rules: tuple[str, ...]
    dynamic_rules: str | None
    rule_keys: dict[str, Any]
    rule_key_promotions: tuple[dict[str, Any], ...]
    rule_overrides: dict[str, dict[str, Any]]
    disable: tuple[str, ...]
    repo_context: dict[str, Any]
    capabilities: dict[str, Any]
    security: dict[str, Any]
    file_filter: dict[str, Any]
    scoring: dict[str, Any]
    reporting: dict[str, Any]
    resolved: dict[str, Any]
    sources: dict[str, str]
    fingerprint: str


def _deep_merge(base: Any, override: Any) -> Any:
    if override is None:
        return copy.deepcopy(base)
    if isinstance(base, dict) and isinstance(override, dict):
        out = dict(base)
        for key, value in override.items():
            out[key] = _deep_merge(out.get(key), value)
        return out
    return copy.deepcopy(override)


def _read_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"profile must be a mapping: {path}")
    return data


def _resolve_root(root: Path | None) -> Path:
    if root is not None:
        return Path(root).expanduser().resolve()
    from reviewagent.config import current

    return Path(current().review_profile_root).expanduser().resolve()


def _resolve_profile_dir(name: str, root: Path) -> Path:
    if not name:
        name = DEFAULT_PROFILE
        logger.warning("scenario.profile_default name={} (REVIEW_PROFILE unset)", name)
    candidate = Path(name)
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        resolved = (root / name).resolve()
    if not resolved.is_dir() or resolved.parent != root:
        available = sorted(p.parent.name for p in root.glob("*/profile.yaml"))
        raise ValueError(f"profile '{name}' not found under {root}; available={available}")
    return resolved


def _has_path(node: Any, path: tuple[str, ...]) -> bool:
    for part in path:
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def _apply_config(profile_dir: Path, merged: dict[str, Any], raw: dict[str, Any], sources: dict[str, str]) -> dict[str, Any]:
    mapping = {
        ("file_filter", "exclude_extensions"): "review_exclude_extensions",
        ("file_filter", "skip_paths"): "improve_skip_test_paths",
        ("file_filter", "priority_paths"): "improve_keyword_paths",
        ("scoring", "feature_keywords"): "improve_test_feature_keywords",
        ("scoring", "review_mode"): "improve_review_mode",
        ("scoring", "test_sample_paths"): "improve_test_sample_paths",
        ("scoring", "test_sample_max"): "improve_test_sample_max",
        ("context", "partial_context_lines"): "improve_partial_context_lines",
        ("context", "patch_context_lines"): "improve_patch_context_lines",
        ("limits", "max_suggestions"): "improve_max_suggestions",
        ("limits", "max_suggestions_per_file"): "improve_max_suggestions_per_file",
        ("limits", "min_suggestions_per_file"): "improve_min_suggestions_per_file",
        ("limits", "max_files_to_review"): "improve_max_files_to_review",
        ("limits", "full_files"): "improve_full_files",
        ("limits", "parallel_workers"): "improve_parallel_workers",
        ("limits", "clip_diff_lines"): "improve_clip_diff_lines",
        ("limits", "batch", "enabled"): "improve_batch_enabled",
        ("limits", "batch", "max_diff_lines"): "improve_batch_max_diff_lines",
        ("limits", "batch", "max_files"): "improve_batch_max_files",
        ("limits", "batch", "max_total_lines"): "improve_batch_max_total_lines",
        ("repo_context", "files"): "repo_context_files",
        ("repo_context", "rules_dir"): "repo_context_rules_dir",
        ("repo_context", "max_lines"): "repo_context_max_lines",
        ("rule_keys", "repo_prefix"): "rule_key_prefix",
    }
    for weight in ("diff", "keyword", "density", "test_feature"):
        mapping[("scoring", "priority_weights", weight)] = f"improve_priority_weight_{weight}"
    explicit = explicit_env()
    updates: dict[str, Any] = {}
    for path, field_name in mapping.items():
        node: Any = merged
        for part in path:
            if not isinstance(node, dict) or part not in node:
                node = None
                break
            node = node[part]
        if node is None:
            continue
        spec = SCHEMA_BY_NAME[field_name]
        if spec.env and spec.env in explicit:
            sources[field_name] = "env"
            continue
        updates[field_name] = node
        sources[field_name] = "profile" if _has_path(raw, path) else "preset"
    apply_scenario(updates)
    return updates


def _fingerprint(root: Path, merged: dict[str, Any], files: list[str]) -> str:
    digest = hashlib.sha256()
    digest.update(json.dumps(merged, sort_keys=True, ensure_ascii=False).encode())
    for rel in sorted(files):
        data = (root / rel).read_bytes() if (root / rel).exists() else b""
        digest.update(rel.encode() + b"\0" + hashlib.sha256(data).digest())
    return digest.hexdigest()


def load_profile(name: str | None = None, *, root: Path | None = None) -> Profile:
    profile_root = _resolve_root(root)
    chosen = name if name is not None else ""
    profile_dir = _resolve_profile_dir(chosen, profile_root)
    raw = _read_yaml(profile_dir / "profile.yaml")
    unknown = set(raw) - _ALLOWED_PROFILE_KEYS
    if unknown:
        raise ValueError(f"unknown profile fields: {sorted(unknown)}")
    if raw.get("schema_version") != _SUPPORTED_SCHEMA:
        raise ValueError(f"unsupported profile schema_version: {raw.get('schema_version')!r}")
    extends = raw.get("extends")
    merged: dict[str, Any] = {}
    sources: dict[str, str] = {}
    if extends:
        if "/" in str(extends) or str(extends).startswith("."):
            raise ValueError(f"invalid preset name: {extends}")
        preset = _read_yaml(profile_root / _PRESETS / f"{extends}.yaml")
        if "extends" in preset:
            raise ValueError("preset must not extend another preset")
        unknown = set(preset) - _ALLOWED_PROFILE_KEYS
        if unknown:
            raise ValueError(f"unknown preset fields: {sorted(unknown)}")
        merged = _deep_merge(merged, preset)
        sources.update({k: "preset" for k in preset})
    for key, value in raw.items():
        if key in {"schema_version", "name", "description", "extends"}:
            continue
        merged[key] = _deep_merge(merged.get(key), value)
        sources[key] = "profile"
    language = merged.get("language")
    if not language:
        raise ValueError(f"profile missing language: {profile_dir}")
    prompts = merged.get("prompts") or {}
    for required in ("improve", "describe"):
        if required not in prompts:
            raise ValueError(f"profile missing prompts.{required}: {profile_dir}")
    _apply_config(profile_dir, merged, raw, sources)
    referenced = list(prompts.values()) + list(merged.get("rules") or [])
    if merged.get("dynamic_rules"):
        referenced.append(merged["dynamic_rules"])
    if merged.get("reporting", {}).get("actions_file"):
        referenced.append(merged["reporting"]["actions_file"])
    return Profile(
        name=str(merged.get("name") or profile_dir.name),
        language=str(language),
        root=profile_dir,
        prompts={k: str(v) for k, v in prompts.items()},
        rules=tuple(str(p) for p in (merged.get("rules") or [])),
        dynamic_rules=merged.get("dynamic_rules"),
        rule_keys=dict(merged.get("rule_keys") or {}),
        rule_key_promotions=tuple(merged.get("rule_key_promotions") or ()),
        rule_overrides=dict(merged.get("rule_overrides") or {}),
        disable=tuple(merged.get("disable") or ()),
        repo_context=dict(merged.get("repo_context") or {}),
        capabilities=dict(merged.get("capabilities") or {}),
        security=dict(merged.get("security") or {}),
        file_filter=dict(merged.get("file_filter") or {}),
        scoring=dict(merged.get("scoring") or {}),
        reporting=dict(merged.get("reporting") or {}),
        resolved=merged,
        sources=sources,
        fingerprint=_fingerprint(profile_dir, merged, referenced),
    )


_active: Profile | None = None


def active() -> Profile:
    if _active is None:
        raise RuntimeError("scenario profile not initialized; call load_and_activate() at startup")
    return _active


def active_or_load() -> Profile:
    global _active
    if _active is None:
        from reviewagent.config import current

        _active = load_profile(current().review_profile or None)
    return _active


def load_and_activate() -> Profile:
    global _active
    from reviewagent.config import current

    _active = load_profile(current().review_profile or None)
    return _active


def bind_instance(profile: Profile, data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "instance.json"
    payload = {"schema_version": 1, "profile": profile.name}
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing.get("profile") != profile.name:
            raise RuntimeError(
                f"data_dir {data_dir} is bound to profile {existing.get('profile')!r}, "
                f"refusing {profile.name!r}; use a new REVIEWAGENT_DATA_DIR"
            )
        return
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
```

- [ ] **Step 4: 写 scenario/prompts.py 与 loader 委托**

```python
# reviewagent/scenario/prompts.py
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

import frontmatter

from reviewagent.scenario.profile import Profile, active_or_load

_PLACEHOLDER = re.compile(r"\{\{([a-zA-Z_][a-zA-Z0-9_]*)\}\}")


def _profile(profile: Profile | None) -> Profile:
    return profile or active_or_load()


def _prompt_path(name: str, profile: Profile | None = None) -> Path:
    prof = _profile(profile)
    rel = prof.prompts.get(name)
    if rel is None:
        path = (prof.root.parent / "_shared" / "prompts" / f"{name}.md").resolve()
        if not path.exists():
            raise FileNotFoundError(f"prompt '{name}' not configured in profile {prof.name}")
        return path
    path = (prof.root / rel).resolve()
    if prof.root.resolve() not in path.parents and path != prof.root.resolve():
        raise ValueError(f"prompt escapes profile root: {rel}")
    return path


def load(name: str, profile: Profile | None = None) -> dict[str, Any]:
    md = frontmatter.load(_prompt_path(name, profile))
    meta = dict(md.metadata)
    tools = meta.get("tools", {})
    return {
        "name": meta.get("name") or name,
        "description": meta.get("description", ""),
        "output_schema": meta.get("output_schema"),
        "tools": tools,
        "tools_disabled": [k for k, v in tools.items() if v is False],
        "prompt": md.content,
    }


def available(profile: Profile | None = None) -> list[str]:
    prof = _profile(profile)
    names = {p.stem for p in prof.root.glob("prompts/*.md") if not p.stem.startswith("_")}
    return sorted(names)


def load_block(name: str, profile: Profile | None = None) -> str:
    if not name.startswith("_"):
        raise ValueError(f"load_block 只接受下划线开头的 block 名, got: {name}")
    prof = _profile(profile)
    path = (prof.root / "prompts" / f"{name}.md").resolve()
    if not path.exists():
        path = (prof.root.parent / "_shared" / "prompts" / f"{name}.md").resolve()
    if prof.root.resolve() not in path.parents:
        raise ValueError(f"block escapes profile root: {name}")
    return frontmatter.load(path).content


def load_template(rel_path: str, profile: Profile | None = None, *, trim_trailing_newline: bool = False) -> str:
    prof = _profile(profile)
    path = (prof.root / rel_path).resolve()
    if prof.root.resolve() not in path.parents:
        raise ValueError(f"template escapes profile root: {rel_path}")
    text = path.read_text(encoding="utf-8")
    return text.rstrip("\n") if trim_trailing_newline else text


def render_template(text: str, values: Mapping[str, str], *, template_path: str) -> str:
    found = set(_PLACEHOLDER.findall(text))
    missing = found - set(values)
    extra = set(values) - found
    if missing or extra:
        raise ValueError(f"template {template_path}: missing={sorted(missing)} extra={sorted(extra)}")
    return _PLACEHOLDER.sub(lambda m: values[m.group(1)], text)
```

```python
# reviewagent/prompts/loader.py（保留 PROMPTS_DIR 常量供 sync 脚本导入）
from pathlib import Path

from reviewagent.scenario.prompts import available as _available
from reviewagent.scenario.prompts import load as _load
from reviewagent.scenario.prompts import load_block as _load_block

PROMPTS_DIR = Path(__file__).parent


def load(name: str) -> dict:
    return _load(name)


def available() -> list[str]:
    return _available()


def load_block(name: str) -> str:
    return _load_block(name)
```

- [ ] **Step 5: 写测试**

```python
# tests/test_scenario_profile.py
import json
from dataclasses import replace

import pytest

from reviewagent.scenario.profile import DEFAULT_PROFILE, bind_instance, load_profile


def test_default_profile_falls_back(monkeypatch):
    monkeypatch.delenv("REVIEW_PROFILE", raising=False)
    profile = load_profile(None)
    assert profile.name == DEFAULT_PROFILE
    assert profile.language == "python"


def test_missing_profile_lists_available(tmp_path):
    with pytest.raises(ValueError, match="available="):
        load_profile("does-not-exist", root=tmp_path)


def test_profile_merges_preset_and_keeps_env_override(monkeypatch):
    monkeypatch.setenv("IMPROVE_PRIORITY_WEIGHT_DIFF", "33")
    profile = load_profile(None)
    assert profile.name == DEFAULT_PROFILE
    assert profile.language == "python"
    assert profile.resolved["scoring"]["priority_weights"]["diff"] == 20
    from reviewagent.config import current

    # env 只覆盖运行配置，不污染 profile 数据（§5.4）
    assert current().improve_priority_weight_diff == 33.0


def test_instance_binding_rejects_other_profile(tmp_path):
    profile = load_profile(None)
    bind_instance(profile, tmp_path)
    other = replace(profile, name="other")
    with pytest.raises(RuntimeError, match="bound to profile"):
        bind_instance(other, tmp_path)
    assert json.loads((tmp_path / "instance.json").read_text())["profile"] == DEFAULT_PROFILE
```

```python
# tests/test_scenario_prompts.py
import hashlib

from reviewagent.prompts.loader import load, load_block
from reviewagent.scenario.prompts import render_template


def test_prompt_bytes_match_legacy_source():
    legacy = open("reviewagent/prompts/improve.md", encoding="utf-8").read()
    assert load("improve")["prompt"] in legacy
    assert hashlib.sha256(load_block("_general_rules_block").encode()).hexdigest() == (
        "84ed4563f513be9589e8ab4aa97c49d2699bc226348377455dd65a8b2fa9712c"
    )


def test_template_placeholder_is_strict():
    assert render_template("a{{x}}b", {"x": "1"}, template_path="t") == "a1b"
    import pytest

    with pytest.raises(ValueError, match="missing"):
        render_template("a{{x}}", {}, template_path="t")
    with pytest.raises(ValueError, match="extra"):
        render_template("a", {"x": "1"}, template_path="t")
```

- [ ] **Step 6: 同步脚本按 profile 解析 prompt 目录**

`scripts/sync_agents.py` 与 `scripts/sync_qoder_agents.py` 中硬编码的 `reviewagent/prompts` 改为：

```python
from reviewagent.scenario.profile import active_or_load

PROMPTS_DIR = active_or_load().root / "prompts"
```

`sync_qoder_agents.py` 保留 `from reviewagent.prompts.loader import PROMPTS_DIR` 的兼容导入，但渲染源改为 `active_or_load()`。现有 `tests/test_sync_qoder_agents.py` 必须零改动通过。

- [ ] **Step 7: 运行测试**

Run: `.venv/bin/pytest tests/test_scenario_profile.py tests/test_scenario_prompts.py tests/test_rule_checksum.py tests/test_sync_qoder_agents.py -v`
Expected: PASS。

- [ ] **Step 8: 提交**

```bash
git add profiles reviewagent/scenario reviewagent/prompts/loader.py scripts/sync_agents.py scripts/sync_qoder_agents.py tests/test_scenario_profile.py tests/test_scenario_prompts.py
git commit -m "feat(scenario): profile/preset/prompt 加载与数据目录绑定"
```

---

## Task 3: CLI 与启动装配（config check/show）

**Files:**
- Create: `reviewagent/cli.py`, `reviewagent/__main__.py`
- Modify: `reviewagent/main.py`（lifespan 调用 bootstrap）
- Modify: `reviewagent/workers/rq_worker.py`（bootstrap 调用）
- Test: `tests/test_cli_config.py`

**Interfaces:**
- Produces: `bootstrap.startup(kind: str) -> BootstrapResult`（Task 15 扩展 RuleSet/manifest；本任务先做 config/profile/instance）。
- Produces: `cli.main(argv: list[str] | None = None) -> int`。
- `config show --sources` 输出 `key: value  # source=env|profile|preset|default`，secret 显示 `***`。

- [ ] **Step 1: 写测试**

```python
# tests/test_cli_config.py
from reviewagent.cli import main


def test_config_check_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("REVIEWAGENT_DATA_DIR", str(tmp_path))
    assert main(["config", "check", "--profile", "ssd-python-autotest"]) == 0


def test_config_check_unknown_profile_lists_available(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("REVIEWAGENT_DATA_DIR", str(tmp_path))
    assert main(["config", "check", "--profile", "nope"]) != 0
    assert "ssd-python-autotest" in capsys.readouterr().err


def test_config_show_redacts_secret(monkeypatch, capsys):
    monkeypatch.setenv("GITLAB_PERSONAL_ACCESS_TOKEN", "super-secret")
    assert main(["config", "show"]) == 0
    out = capsys.readouterr().out
    assert "super-secret" not in out
    assert "***" in out
```

- [ ] **Step 2: 实现 CLI 与启动**

```python
# reviewagent/cli.py
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from reviewagent.config import apply_scenario, current
from reviewagent.scenario.profile import bind_instance, load_profile
from reviewagent.settings.binder import fingerprint
from reviewagent.settings.schema import SCHEMA


def _load(args):
    profile = load_profile(args.profile)
    bind_instance(profile, current().data_dir)
    return profile


def _cmd_config_check(args) -> int:
    try:
        _load(args)
    except Exception as exc:
        print(f"config check failed: {exc}", file=sys.stderr)
        return 2
    print("ok")
    return 0


def _cmd_config_show(args) -> int:
    profile = _load(args)
    default = {spec.name: spec.default for spec in SCHEMA}
    for spec in SCHEMA:
        value = getattr(current(), spec.name)
        if args.diff_default and value == default[spec.name]:
            continue
        shown = "***" if spec.secret else value
        source = "default"
        if spec.name in profile.sources:
            source = profile.sources[spec.name]
        elif spec.env and spec.env in os.environ:
            source = "env"
        line = f"{spec.name}: {shown}"
        if args.sources:
            line += f"  # source={source}"
        print(line)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="reviewagent")
    sub = parser.add_subparsers(dest="cmd", required=True)
    cfg = sub.add_parser("config")
    cfg_sub = cfg.add_subparsers(dest="action", required=True)
    for action in ("check", "show"):
        p = cfg_sub.add_parser(action)
        p.add_argument("--profile", default=None)
        p.add_argument("--sources", action="store_true")
        p.add_argument("--diff-default", action="store_true")
        p.set_defaults(func=_cmd_config_check if action == "check" else _cmd_config_show)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
```

```python
# reviewagent/bootstrap.py
from __future__ import annotations

from dataclasses import dataclass

from reviewagent.config import current
from reviewagent.logging_setup import logger, setup_logging
from reviewagent.scenario.profile import bind_instance, load_and_activate


@dataclass(frozen=True)
class BootstrapResult:
    profile_name: str
    profile_fingerprint: str


def startup(kind: str) -> BootstrapResult:
    setup_logging()
    profile = load_and_activate()
    bind_instance(profile, current().data_dir)
    logger.info(
        "config.resolved kind={} profile={} profile_fingerprint={}",
        kind, profile.name, profile.fingerprint,
    )
    return BootstrapResult(profile.name, profile.fingerprint)
```

- [ ] **Step 3: 接入 API 与 worker**

`reviewagent/main.py` lifespan 首行改为 `bootstrap.startup("api")`（保留 `setup_logging` 幂等）；`rq_worker.bootstrap()` 开头调用 `bootstrap.startup("worker")`。

- [ ] **Step 4: WeeklyReportConfig 兼容视图**

`reviewagent/reporting/config.py` 的 `WeeklyReportConfig.from_env()` 改为从 `config.current()` 读取实例字段，保留类属性与 `from_env()` 签名（`tests` 与 `scripts/weekly_report.py` 零改动）：

```python
    @classmethod
    def from_env(cls) -> "WeeklyReportConfig":
        from reviewagent.config import current

        cfg = current()
        return cls(
            enabled=cfg.weekly_enabled,
            target_project_id=cfg.weekly_target_project_id,
            target_branch=cfg.weekly_target_branch,
            timezone=cfg.weekly_timezone,
            collectors=cfg.weekly_collectors,
            notifier=cfg.weekly_notifier,
            dingtalk_webhook_url=cfg.weekly_dingtalk_webhook_url,
            dingtalk_secret=cfg.weekly_dingtalk_secret,
            dingtalk_dry_run=cfg.weekly_dingtalk_dry_run,
            dingtalk_retry_attempts=cfg.weekly_dingtalk_retry,
            markdown_chunk_limit=cfg.weekly_md_chunk_limit,
            report_title=cfg.weekly_report_title,
            report_emoji=cfg.weekly_report_emoji,
            cron_schedule=cfg.weekly_cron_schedule,
            dashboard_url=cfg.weekly_dashboard_url,
        )
```

这些字段在 Task 1 的 `SCHEMA` 末尾追加（`scope="instance"`，env 用现有键）：`weekly_enabled(REVIEWAGENT_WEEKLY_ENABLED, True)`、`weekly_target_project_id(..._TARGET_PROJECT_ID, 0)`、`weekly_target_branch(..._TARGET_BRANCH, "main")`、`weekly_timezone(..._TIMEZONE, "Asia/Shanghai")`、`weekly_collectors(..._COLLECTORS, ("telemetry","merged_mrs","repo_scan"))`、`weekly_notifier(..._NOTIFIER, "dingtalk")`、`weekly_dingtalk_webhook_url(..._DINGTALK_WEBHOOK_URL, "")`（兼容 `DINGTALK_WEBHOOK` alias）、`weekly_dingtalk_secret(..._DINGTALK_SECRET, "")`、`weekly_dingtalk_dry_run(..._DINGTALK_DRY_RUN, True)`、`weekly_dingtalk_retry(..._DINGTALK_RETRY, 3)`、`weekly_md_chunk_limit(..._MD_CHUNK_LIMIT, 18000)`、`weekly_report_title(..._REPORT_TITLE, "SSD自动化代码检视周报")`、`weekly_report_emoji(..._REPORT_EMOJI, "📊")`、`weekly_cron_schedule(..._CRON_SCHEDULE, "Mon 10:30")`、`weekly_dashboard_url(..._DASHBOARD_URL, "http://127.0.0.1:8080/code-review")`。`collectors` 按空格/逗号分割；所有字段加入 `Config` dataclass 后 `test_schema_covers_every_config_field` 才通过。`WeeklyReportConfig` 不再自行解析 env。

- [ ] **Step 5: 启动日志只打一条 config.resolved**

`bootstrap.startup()` 追加：

```python
    from reviewagent.settings.binder import explicit_env

    compat = sorted(k for k in explicit_env() if k in _SCENARIO_ENV_KEYS)
    if compat:
        logger.info("config.compat_env keys={}", compat)
```

`_SCENARIO_ENV_KEYS` 由 `SCHEMA` 中 `scope == "scenario"` 的 `env` 集合生成。

- [ ] **Step 6: 运行测试**

Run: `.venv/bin/pytest tests/test_cli_config.py tests/test_metrics_endpoint.py tests/test_worker_runtime.py -q`
Expected: PASS。

- [ ] **Step 7: 提交**

```bash
git add reviewagent/cli.py reviewagent/__main__.py reviewagent/bootstrap.py reviewagent/main.py reviewagent/workers/rq_worker.py tests/test_cli_config.py
git commit -m "feat(cli): config check/show 与启动装配入口"
```

---

## Task 4: 规则模型与 Markdown 解析器

**Files:**
- Create: `reviewagent/scenario/rules/__init__.py`, `model.py`, `parser.py`
- Test: `tests/test_rules_parser.py`

**Interfaces:**
- Produces: `Rule`、`RulePack`（frozen dataclass，字段见 spec §6.2/§6.3）。
- Produces: `parse_pack(path: Path, *, language: str, root: Path) -> RulePack`。
- Produces: `RULE_KEY_RE`（键语法校验，不绑定语言前缀）。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_rules_parser.py
import hashlib
from pathlib import Path

import pytest

from reviewagent.scenario.rules.parser import parse_pack

ROOT = Path("profiles/ssd-python-autotest")


def test_verbatim_body_is_byte_identical():
    pack = parse_pack(ROOT / "rules/general_rules.md", language="python", root=ROOT)
    assert pack.render == "verbatim"
    assert hashlib.sha256(pack.body.encode()).hexdigest() == (
        "84ed4563f513be9589e8ab4aa97c49d2699bc226348377455dd65a8b2fa9712c"
    )
    assert {"R-RES", "R-LOG", "R-LOOP"} <= set(pack.index_by_key)


def test_unknown_frontmatter_field_fails(tmp_path):
    pack = tmp_path / "bad.md"
    pack.write_text("---\nschema_version: 1\nid: x\npriority: 800\nrender: verbatim\nnope: 1\n---\n")
    with pytest.raises(ValueError, match="unknown frontmatter"):
        parse_pack(pack, language="python", root=tmp_path)


def test_unsupported_schema_version_fails(tmp_path):
    pack = tmp_path / "bad.md"
    pack.write_text("---\nschema_version: 2\nid: x\npriority: 800\nrender: verbatim\n---\n")
    with pytest.raises(ValueError, match="schema_version"):
        parse_pack(pack, language="python", root=tmp_path)
```

- [ ] **Step 2: 生成规则包（正文逐字节复制）**

```bash
mkdir -p profiles/ssd-python-autotest/rules
.venv/bin/python - <<'PY'
import frontmatter
from pathlib import Path

body = frontmatter.load("reviewagent/prompts/_general_rules_block.md").content
head = (
    "---\n"
    "schema_version: 1\n"
    "id: ssd-python-autotest/core\n"
    "title: SSD 自动化测试通用规则\n"
    "priority: 800\n"
    "render: verbatim\n"
    "applies_to:\n  languages: [python]\n"
    "---\n"
)
Path("profiles/ssd-python-autotest/rules/general_rules.md").write_text(head + body, encoding="utf-8")
PY
.venv/bin/python - <<'PY'
import frontmatter
a = frontmatter.load("reviewagent/prompts/_general_rules_block.md").content
b = frontmatter.load("profiles/ssd-python-autotest/rules/general_rules.md").content
assert a == b, "body drifted"
print("verbatim body ok")
PY
```

- [ ] **Step 3: 实现 model 与 parser**

```python
# reviewagent/scenario/rules/model.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Rule:
    key: str
    title: str
    body: str = ""
    severity: str = "medium"
    weight: float = 1.0
    applies_to: dict[str, Any] = field(default_factory=dict)
    examples: tuple[dict[str, str], ...] = ()
    actions: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    detect_hints: tuple[str, ...] = ()
    source: dict[str, Any] = field(default_factory=dict)
    priority: int = 800


@dataclass(frozen=True)
class RulePack:
    id: str
    title: str
    priority: int
    render: str
    body: str
    rules: tuple[Rule, ...]
    index_by_key: dict[str, Rule]
    path: str
    fingerprint: str
```

```python
# reviewagent/scenario/rules/parser.py
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import frontmatter

from reviewagent.scenario.rules.model import Rule, RulePack

SUPPORTED_SCHEMA = 1
_ALLOWED = {"schema_version", "id", "title", "priority", "render", "applies_to", "aliases", "actions"}
RULE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*$")


def _require_str(fm: dict[str, Any], name: str) -> str:
    value = fm.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"rule pack missing '{name}'")
    return value.strip()


def _table_rules(body: str, pack_id: str, priority: int) -> tuple[Rule, ...]:
    rules: list[Rule] = []
    for line in body.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not cells:
            continue
        key = cells[0].strip("`")
        if not RULE_KEY_RE.match(key):
            continue
        title = cells[1] if len(cells) > 1 else key
        bad = cells[2] if len(cells) > 2 else ""
        fix = cells[3] if len(cells) > 3 else ""
        rules.append(Rule(key=key, title=title, body=f"{bad}\n\n{fix}".strip(),
                          priority=priority, source={"kind": "profile", "pack_id": pack_id}))
    return tuple(rules)


def _structured_rules(body: str, pack_id: str, priority: int, fm: dict[str, Any]) -> tuple[Rule, ...]:
    rules: list[Rule] = []
    parts = re.split(r"(?m)^###\s+(\S+)\s*$", body)
    if len(parts) == 1:
        return _table_rules(body, pack_id, priority)
    for idx in range(1, len(parts), 2):
        key, section = parts[idx].strip("`"), parts[idx + 1]
        if not RULE_KEY_RE.match(key):
            continue
        examples = []
        for kind, code in re.findall(r"```(bad|good)\n(.*?)```", section, flags=re.S):
            examples.append({"kind": kind, "code": code.strip("\n"), "lang": "text"})
        rules.append(Rule(
            key=key, title=key, body=section.strip(), examples=tuple(examples), priority=priority,
            applies_to=dict(fm.get("applies_to") or {}),
            aliases=tuple(fm.get("aliases") or ()), actions=tuple(fm.get("actions") or ()),
            source={"kind": "profile", "pack_id": pack_id},
        ))
    return tuple(rules)


def parse_pack(path: Path, *, language: str, root: Path) -> RulePack:
    resolved = path.resolve()
    if root.resolve() not in resolved.parents:
        raise ValueError(f"rule pack escapes profile root: {path}")
    md = frontmatter.load(resolved)
    fm = dict(md.metadata)
    unknown = set(fm) - _ALLOWED
    if unknown:
        raise ValueError(f"unknown frontmatter fields {sorted(unknown)} in {path}")
    if fm.get("schema_version") != SUPPORTED_SCHEMA:
        raise ValueError(f"unsupported rule pack schema_version {fm.get('schema_version')!r} in {path}")
    pack_id = _require_str(fm, "id")
    render = fm.get("render", "structured")
    if render not in ("verbatim", "structured"):
        raise ValueError(f"unknown render mode {render!r} in {path}")
    priority = int(fm.get("priority", 800))
    if not 0 <= priority <= 1000:
        raise ValueError(f"priority out of range in {path}: {priority}")
    langs = (fm.get("applies_to") or {}).get("languages")
    if langs and language not in langs:
        rules: tuple[Rule, ...] = ()
    elif render == "verbatim":
        rules = _table_rules(md.content, pack_id, priority)
    else:
        rules = _structured_rules(md.content, pack_id, priority, fm)
    index = {r.key: r for r in rules}
    if len(index) != len(rules):
        raise ValueError(f"duplicate rule key in {path}")
    return RulePack(
        id=pack_id, title=fm.get("title", pack_id), priority=priority, render=render,
        body=md.content, rules=rules, index_by_key=index, path=str(resolved),
        fingerprint=hashlib.sha256(resolved.read_bytes()).hexdigest(),
    )
```

- [ ] **Step 4: 运行测试**

Run: `.venv/bin/pytest tests/test_rules_parser.py tests/test_rule_checksum.py -v`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add reviewagent/scenario/rules profiles/ssd-python-autotest/rules tests/test_rules_parser.py
git commit -m "feat(rules): 规则包模型与 verbatim/structured 解析器"
```

---

## Task 5: 规则装配 / 键 / 渲染，并切换到规则包正文

**Files:**
- Create: `reviewagent/scenario/rules/resolver.py`, `keys.py`, `render.py`
- Modify: `reviewagent/scenario/prompts.py`（`_general_rules_block` 改读 RuleSet）
- Delete: `reviewagent/prompts/_general_rules_block.md`、`profiles/ssd-python-autotest/prompts/_general_rules_block.md`
- Test: `tests/test_rules_resolver.py`, `tests/test_rules_render.py`, `tests/test_rules_keys.py`

**Interfaces:**
- Produces: `RuleSet(rules, index_by_key, alias_map, promotions, key_patterns, sources, render, verbatim_body, fingerprint)`。
- Produces: `assemble_base(profile: Profile) -> RuleSet`、`active_rule_set() -> RuleSet`。
- Produces: `KeyPatterns(builtin_raw, builtin, repo, free_prefixes)` + `build_key_patterns(rule_keys: dict) -> KeyPatterns`。
- Produces: `render_rule_block(rule_set: RuleSet, *, profile: Profile, file_paths: Sequence[str] = (), budget: int | None = None) -> str`。
- `scenario.prompts.load_block("_general_rules_block")` 返回 `active_rule_set().verbatim_body`。

- [ ] **Step 1: 写优先级/namespace 测试**

```python
# tests/test_rules_resolver.py
from dataclasses import replace

import pytest

from reviewagent.scenario.profile import load_profile
from reviewagent.scenario.rules.resolver import assemble_base


def test_base_ruleset_orders_and_indexes():
    ruleset = assemble_base(load_profile(None))
    assert ruleset.verbatim_body
    assert "R-LOG" in ruleset.index_by_key
    assert ruleset.sources[0]["kind"] == "profile"


def test_unknown_override_key_fails():
    profile = load_profile(None)
    bad = replace(profile, rule_overrides={"R-NOPE": {"severity": "high"}})
    with pytest.raises(ValueError, match="override"):
        assemble_base(bad)


def test_disable_keeps_verbatim_body_but_drops_index():
    profile = load_profile(None)
    bad = replace(profile, disable=("R-LOG",))
    ruleset = assemble_base(bad)
    assert "R-LOG" not in ruleset.index_by_key
    assert "R-LOG" in ruleset.verbatim_body
```

```python
# tests/test_rules_keys.py
from reviewagent.scenario.rules.keys import build_key_patterns

KEYS = {
    "builtin_pattern": r"(?:R-OTHER-IMPACT:[a-z0-9_]+|R-[A-Z]+(?:-[A-Z0-9_]+)*)",
    "free_prefixes": ["R-OTHER-IMPACT"],
    "repo_prefix": "SSD",
    "display_prefix": "SSD",
}


def test_extract_matches_legacy_shapes():
    kp = build_key_patterns(KEYS)
    assert kp.extract("命中 R-LOG 与 SSD-RULE-NO-LOG-EXC，另有 R-OTHER-IMPACT:caller_param") == [
        "R-LOG", "SSD-RULE-NO-LOG-EXC", "R-OTHER-IMPACT:caller_param",
    ]
    assert kp.is_repo("SSD-RULE-X") is True
    assert kp.is_repo("R-LOG") is False
```

- [ ] **Step 2: 实现 resolver/keys/render**

```python
# reviewagent/scenario/rules/keys.py
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class KeyPatterns:
    builtin_raw: re.Pattern[str]
    builtin: re.Pattern[str]
    repo: re.Pattern[str]
    free_prefixes: tuple[str, ...]

    def extract(self, text: str) -> list[str]:
        text = text or ""
        hits: list[tuple[int, str]] = []
        for pattern in (self.builtin, self.repo):
            for match in pattern.finditer(text):
                hits.append((match.start(), match.group(1)))
        found: list[str] = []
        for _, key in sorted(hits):
            if key not in found:
                found.append(key)
        return found

    def is_builtin(self, key: str) -> bool:
        return bool(self.builtin_raw.fullmatch(key))

    def is_repo(self, key: str) -> bool:
        return bool(self.repo.fullmatch(key))


def build_key_patterns(rule_keys: dict[str, Any]) -> KeyPatterns:
    pattern = str(rule_keys["builtin_pattern"])
    builtin_raw = re.compile(pattern)
    builtin = re.compile(rf"(?<![\w-])({pattern})")
    repo_prefix = str(rule_keys.get("repo_prefix") or "SSD")
    repo = re.compile(rf"(?<![\w])({re.escape(repo_prefix)}-RULE-[A-Z0-9-]+)(?![\w])")
    return KeyPatterns(builtin_raw, builtin, repo, tuple(rule_keys.get("free_prefixes") or ()))
```

```python
# reviewagent/scenario/rules/resolver.py
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any

from reviewagent.scenario.profile import Profile
from reviewagent.scenario.rules.keys import KeyPatterns, build_key_patterns
from reviewagent.scenario.rules.model import Rule
from reviewagent.scenario.rules.parser import parse_pack


@dataclass(frozen=True)
class RuleSet:
    rules: tuple[Rule, ...]
    index_by_key: dict[str, Rule]
    alias_map: dict[str, str]
    promotions: tuple[dict[str, Any], ...]
    key_patterns: KeyPatterns
    sources: tuple[dict[str, Any], ...]
    render: str
    verbatim_body: str
    fingerprint: str

    def normalize_keys(self, text: str) -> list[str]:
        keys = self.key_patterns.extract(text)
        lowered = (text or "").lower()
        for promo in self.promotions:
            if promo.get("when_any") and any(w.lower() in lowered for w in promo["when_any"]):
                if promo["to_key"] not in keys:
                    keys.append(promo["to_key"])
            if promo.get("when_all") and all(w.lower() in lowered for w in promo["when_all"]):
                if promo["to_key"] not in keys:
                    keys.append(promo["to_key"])
        return keys


def assemble_base(profile: Profile) -> RuleSet:
    pairs = [
        (order, parse_pack(profile.root / rel, language=profile.language, root=profile.root))
        for order, rel in enumerate(profile.rules)
    ]
    pairs.sort(key=lambda item: (-item[1].priority, item[0]))
    packs = [pack for _, pack in pairs]
    index: dict[str, Rule] = {}
    rules: list[Rule] = []
    for pack in packs:
        for rule in pack.rules:
            if rule.key in profile.disable:
                continue
            index[rule.key] = rule
            rules.append(rule)
    for key, override in profile.rule_overrides.items():
        if key not in index:
            raise ValueError(f"rule override references unknown key: {key}")
        index[key] = replace(index[key], **{k: v for k, v in override.items() if k in {"severity", "weight"}})
    rules = [index.get(r.key, r) for r in rules]
    body = "\n\n".join(pack.body for pack in packs)
    alias_map = {rule.key: rule.key for rule in rules}
    for rule in rules:
        for alias in rule.aliases:
            alias_map[alias] = rule.key
    payload = json.dumps([[r.key, r.severity, r.weight] for r in rules], sort_keys=True)
    render_mode = packs[0].render if packs else "verbatim"
    return RuleSet(
        rules=tuple(rules), index_by_key=index, alias_map=alias_map,
        promotions=profile.rule_key_promotions,
        key_patterns=build_key_patterns(profile.rule_keys),
        sources=tuple({"kind": "profile", "path": p.path, "pack_id": p.id} for p in packs),
        render=render_mode, verbatim_body=body,
        fingerprint=hashlib.sha256(body.encode()).hexdigest()[:16] + hashlib.sha256(payload.encode()).hexdigest()[:16],
    )
```

```python
# reviewagent/scenario/rules/render.py
from __future__ import annotations

from typing import Sequence

from reviewagent.scenario.profile import Profile
from reviewagent.scenario.rules.resolver import RuleSet


def render_rule_block(
    rule_set: RuleSet, *, profile: Profile, file_paths: Sequence[str] = (), budget: int | None = None,
) -> str:
    if rule_set.render == "verbatim":
        if budget is not None and len(rule_set.verbatim_body.encode()) > budget:
            raise ValueError(
                f"verbatim rule block exceeds budget for profile {profile.name}: "
                f"{len(rule_set.verbatim_body.encode())} > {budget}"
            )
        return rule_set.verbatim_body
    lines = ["| 规则键 | 严重度 | 摘要 |", "|---|---|---|"]
    lines += [f"| {r.key} | {r.severity} | {r.title} |" for r in rule_set.rules]
    return "\n".join(lines)
```

- [ ] **Step 3: 切换 load_block 并删除旧文件**

```python
# reviewagent/scenario/prompts.py 追加
_RULE_SET = None


def set_active_rule_set(rule_set) -> None:
    global _RULE_SET
    _RULE_SET = rule_set


def load_block(name: str, profile: Profile | None = None) -> str:
    if name == "_general_rules_block":
        from reviewagent.scenario.rules.render import render_rule_block
        from reviewagent.scenario.rules.resolver import assemble_base

        rule_set = _RULE_SET or assemble_base(_profile(profile))
        return render_rule_block(rule_set, profile=_profile(profile))
    if not name.startswith("_"):
        raise ValueError(f"load_block 只接受下划线开头的 block 名, got: {name}")
    return frontmatter.load(_prompt_path(name[1:], profile)).content
```

```bash
git rm reviewagent/prompts/_general_rules_block.md profiles/ssd-python-autotest/prompts/_general_rules_block.md
```

- [ ] **Step 4: 运行 checksum 与全量测试**

Run: `.venv/bin/pytest tests/test_rule_checksum.py tests/test_rules_*.py -q && .venv/bin/pytest -q`
Expected: 全绿；checksum 仍为 `84ed...`。

- [ ] **Step 5: 提交**

```bash
git add -A reviewagent/scenario/rules reviewagent/scenario/prompts.py reviewagent/prompts tests/test_rules_resolver.py tests/test_rules_render.py tests/test_rules_keys.py
git commit -m "feat(rules): 规则装配、键模式与 verbatim 渲染，移除旧规则块"
```

---

## Task 6: 仓库规则模块 + 请求级 overlay + rules check

**Files:**
- Create: `reviewagent/scenario/rules/repo.py`
- Modify: `reviewagent/repo_context.py`（仅 re-export）
- Modify: `reviewagent/cli.py`（`rules check`）
- Test: `tests/test_rules_repo.py`

**Interfaces:**
- Produces: `fetch_rule_files(gitlab_client, project_id) -> dict[str, str]`、`build_repo_context(gitlab_client, project_id) -> str`、`extract_rule_keys(text) -> list[str]`、`render_instruction_files(files, max_lines) -> str`。
- Produces: `parse_repo_overlay(files: dict[str, str], *, profile: Profile, base: RuleSet) -> tuple[Rule, ...]`、`RuleSet.with_repo(rules) -> RuleSet`。
- `reviewagent.repo_context` 只做 re-export，不保留实现。

- [ ] **Step 1: 写测试**

```python
# tests/test_rules_repo.py
from reviewagent.scenario.profile import load_profile
from reviewagent.scenario.rules.repo import parse_repo_overlay, render_instruction_files
from reviewagent.scenario.rules.resolver import assemble_base


def test_instruction_files_render_escapes_and_truncates():
    text = render_instruction_files({"AGENTS.md": "# hi\n" + "x\n" * 10}, max_lines=8)
    assert "<instruction_files>" in text
    assert "truncated" in text


def test_repo_overlay_structured_entry_overrides_scenario_rule():
    profile = load_profile(None)
    base = assemble_base(profile)
    files = {"AGENTS.md": "| 键 | 严重度 | 摘要 |\n|---|---|---|\n| `R-LOG` | high | 仓库要求 |\n"}
    overlay = parse_repo_overlay(files, profile=profile, base=base)
    merged = base.with_repo(overlay)
    assert merged.index_by_key["R-LOG"].severity == "high"
    assert merged.index_by_key["R-LOG"].priority == 1000
    # 仓库规则原文走 prompt 的 {{repo_rules}}，不得改写场景规则正文
    assert merged.verbatim_body == base.verbatim_body
    assert merged.fingerprint != base.fingerprint


def test_repo_free_text_is_not_a_rule():
    profile = load_profile(None)
    overlay = parse_repo_overlay({"AGENTS.md": "请使用 with 管理资源。\n"}, profile=profile, base=assemble_base(profile))
    assert overlay == ()
```

- [ ] **Step 2: 迁移实现**

把 `reviewagent/repo_context.py` 的 `_RepoContextCache`、`_fetch_all_rule_files`、`render_instruction_files`、`extract_rule_keys` 原样迁入 `reviewagent/scenario/rules/repo.py`，仅将 `from reviewagent.config import config` 改为函数内读取 `current()`；追加：

```python
from dataclasses import replace

from reviewagent.scenario.rules.model import Rule
from reviewagent.scenario.rules.parser import RULE_KEY_RE


def parse_repo_overlay(files: dict[str, str], *, profile, base) -> tuple[Rule, ...]:
    rules: dict[str, Rule] = {}
    for path, content in files.items():
        for line in content.splitlines():
            if not line.lstrip().startswith("|"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            key = cells[0].strip("`") if cells else ""
            if not base.key_patterns.is_repo(key):
                continue
            severity = cells[1] if len(cells) > 1 and cells[1] in {"critical", "high", "medium", "low"} else "medium"
            title = cells[2] if len(cells) > 2 else key
            rules[key] = Rule(key=key, title=title or key, severity=severity, priority=1000,
                              source={"kind": "repo", "path": path})
    return tuple(rules.values())
```

`RuleSet.with_repo` 在 `resolver.py` 中补：

```python
    def with_repo(self, overlay: tuple[Rule, ...]) -> "RuleSet":
        if not overlay:
            return self
        import hashlib

        index = dict(self.index_by_key)
        for rule in overlay:
            index[rule.key] = rule
        digest = hashlib.sha256(
            ":".join(sorted(rule.key for rule in overlay)).encode()
        ).hexdigest()[:12]
        # 只换索引与指纹；verbatim_body 仍是场景规则正文，仓库原文经 {{repo_rules}} 注入
        return replace(self, index_by_key=index, rules=tuple(index.values()),
                       fingerprint=f"{self.fingerprint}:repo:{digest}")
```

- [ ] **Step 3: repo_context.py 改为 re-export**

```python
"""兼容门面：实现已迁至 scenario/rules/repo.py。"""
from reviewagent.scenario.rules.repo import (  # noqa: F401
    build_repo_context,
    extract_rule_keys,
    fetch_rule_files,
    render_instruction_files,
)

__all__ = ["build_repo_context", "extract_rule_keys", "fetch_rule_files", "render_instruction_files"]
```

- [ ] **Step 4: `rules check` CLI**

```python
import hashlib
import sys


def _cmd_rules_check(args) -> int:
    from reviewagent.scenario.profile import load_profile
    from reviewagent.scenario.rules.resolver import assemble_base

    try:
        rule_set = assemble_base(load_profile(args.profile))
    except Exception as exc:
        print(f"rules check failed: {exc}", file=sys.stderr)
        return 2
    for rule in rule_set.rules:
        print(f"{rule.key}\t{rule.severity}\t{rule.title}")
    print(f"ruleset_fingerprint={rule_set.fingerprint}")
    print(f"render_checksum={hashlib.sha256(rule_set.verbatim_body.encode()).hexdigest()}")
    return 0
```

`assemble_base` 已经覆盖作者链路校验：`parse_pack` 校验 `schema_version` / 重复 id / 非法 glob / 示例围栏闭合，resolver 校验命名空间与共享包语言声明。`rules check` 只需把首个错误原文打到 stderr 并返回 2，不在 CLI 里复制校验逻辑。

- [ ] **Step 5: 运行测试**

Run: `.venv/bin/pytest tests/test_rules_repo.py tests/test_rule_checksum.py -q`
Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add reviewagent/scenario/rules/repo.py reviewagent/scenario/rules/resolver.py reviewagent/repo_context.py reviewagent/cli.py tests/test_rules_repo.py
git commit -m "feat(rules): 仓库规则模块、请求级 overlay 与 rules check"
```

---

## Task 7: review 类型 + LanguagePolicy + registry + PythonPolicy

**Files:**
- Create: `reviewagent/review/__init__.py`, `types.py`, `policy.py`
- Create: `reviewagent/review/policies/__init__.py`, `registry.py`, `generic.py`, `python.py`
- Modify: `reviewagent/commands/improve.py`（迁移纯函数，门面委托）
- Modify: `reviewagent/commands/suggestion_actions.py`（关键字注入）
- Test: `tests/test_policy_python.py`

**Interfaces:**
- Produces: `FileKind`、`FileContext`、`Suggestion = dict[str, Any]`、`RiskVerdict = dict[str, Any]`。
- Produces: `LanguagePolicy` Protocol（spec §7.3 的 9 个钩子）。
- Produces: `registry.register(language, factory)`、`registry.get(language, *, profile, cfg, read_lines) -> LanguagePolicy`。
- Produces: `PythonPolicy.extract_symbols / normalize_suggestion / validate_suggestion / score_suggestion / test_feature_density / classify_file / keywords / render_source_block / prompt_sections`。
- 迁移函数：`find_line_by_existing_code`、`fix_indent`、`normalise_suggestion`、`score_suggestion`、`code_first_line_matches`、`collect_defined_names`、`safe_parse_defs`、`detect_apply_risk`（源范围 `improve.py:1772-1862, 1976-2294, 3134-3300`）。

- [ ] **Step 1: 写类型与协议**

```python
# reviewagent/review/types.py
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

Suggestion = dict[str, Any]
RiskVerdict = dict[str, Any]


class FileKind(str, Enum):
    TEST = "test"
    SOURCE = "source"
    FIXTURE = "fixture"
    OTHER = "other"


@dataclass(frozen=True)
class FileContext:
    file_path: str
    lines: tuple[str, ...]
    valid_lines: frozenset[int]
    diff: str = ""
    worktree: Path | None = None
    start_line: int = 1  # lines[0] 在文件中的 1-based 行号（片段渲染用）
```

```python
# reviewagent/review/policy.py
from typing import Protocol

from reviewagent.review.types import FileContext, FileKind, RiskVerdict, Suggestion


class LanguagePolicy(Protocol):
    def classify_file(self, path: str) -> FileKind: ...
    def test_feature_density(self, path: str, size: int) -> float: ...
    def extract_symbols(self, diff: str) -> list[str]: ...
    def render_source_block(self, ctx: FileContext, strategy: str, budget: int) -> str: ...
    def normalize_suggestion(self, sug: Suggestion, ctx: FileContext) -> Suggestion: ...
    def validate_suggestion(self, sug: Suggestion, ctx: FileContext, *,
                            rule_set, file_sources: dict[str, list[str]],
                            line_map: dict[str, set[int]]) -> RiskVerdict: ...
    def score_suggestion(self, sug: Suggestion) -> int: ...
    def keywords(self) -> set[str]: ...
    def prompt_sections(self) -> dict[str, str]: ...
```

- [ ] **Step 2: 迁移 Python 实现**

新建 `reviewagent/review/policies/python.py`，把 `improve.py` 中对应函数体**原样复制**并去掉 `self`：保留函数名与参数顺序；`_validate_suggestion` 的 `file_sources` 改为由 `FileContext` 携带的 `file_sources` 字典参数（门面传入同一 dict，保持缓存语义）。`PythonPolicy` 仅做参数装配：

```python
class PythonPolicy:
    def __init__(self, *, profile, cfg, read_lines):
        self._profile = profile
        self._cfg = cfg
        self._read_lines = read_lines

    def classify_file(self, path: str) -> FileKind:
        lowered = path.replace("\\", "/")
        if lowered.endswith("conftest.py") or "fixture" in lowered:
            return FileKind.FIXTURE
        if any(part in lowered for part in ("/tests/", "tests/")) or lowered.rsplit("/", 1)[-1].startswith("test_"):
            return FileKind.TEST
        return FileKind.SOURCE

    def extract_symbols(self, diff: str) -> list[str]:
        return extract_identifiers(diff)

    def test_feature_density(self, path: str, size: int) -> float:
        return test_feature_density(
            path, size,
            read_lines=self._read_lines,
            keywords=self._cfg.improve_test_feature_keywords,
        )

    def score_suggestion(self, sug: Suggestion) -> int:
        return score_suggestion(sug, rule_prefix=self._profile.rule_keys.get("repo_prefix", "SSD"))

    def keywords(self) -> set[str]:
        return set(PYTHON_KEYWORDS)

    def prompt_sections(self) -> dict[str, str]:
        return {}

    def render_source_block(self, ctx: FileContext, strategy: str, budget: int) -> str:
        return render_python_source_block(ctx, strategy, budget, cfg=self._cfg)

    def normalize_suggestion(self, sug: Suggestion, ctx: FileContext) -> Suggestion:
        return normalise_suggestion(sug)

    def validate_suggestion(self, sug, ctx, *, rule_set, file_sources, line_map):
        # 薄派发：通用校验顺序在 review/validation.py，语言差异（AST/apply risk）经 policy 钩子；
        # 模块实现不得回调 policy.validate_suggestion，避免递归。
        from reviewagent.review.validation import validate_suggestion as _validate

        return _validate(
            sug, ctx=ctx, policy=self, rule_set=rule_set, cfg=self._cfg,
            find_line=find_line_by_existing_code,
            file_sources=file_sources, line_map=line_map,
        )
```

`score_suggestion` 的数表原样复制；`rule_prefix` 仅替换原先写死的 `SSD`：

```python
_RULE_EXEMPT = re.compile(rf"(?<![\w])({re.escape(rule_prefix)}-RULE-[A-Z0-9-]+)(?![\w])")
```

`render_source_block(ctx, strategy, budget)` 只负责**片段内的行号/缩进格式化**：`ctx.lines` 是 context 已切好的片段，`ctx.start_line` 是片段首行的 1-based 行号；Python 实现为 `"\n".join(f"{ctx.start_line + i:4d}| {ln}" ...)`。块标题、分段合并、截断上限、段数上限全部由 `review/context.py` 决定，policy 不感知。

- [ ] **Step 3: registry 与 GenericPolicy**

```python
# reviewagent/review/policies/registry.py
from __future__ import annotations

import importlib
from typing import Any, Callable

from reviewagent.review.policy import LanguagePolicy

_FACTORIES: dict[str, Callable[..., LanguagePolicy]] = {}


def register(language: str, factory: Callable[..., LanguagePolicy]) -> None:
    _FACTORIES[language] = factory


def _builtin(language: str) -> Callable[..., LanguagePolicy]:
    if language == "python":
        module = importlib.import_module("reviewagent.review.policies.python")
        return module.PythonPolicy
    module = importlib.import_module("reviewagent.review.policies.generic")
    return module.GenericPolicy


def get(language: str, **kwargs: Any) -> LanguagePolicy:
    factory = _FACTORIES.get(language) or _builtin(language)
    return factory(**kwargs)
```

`GenericPolicy` 实现全部钩子：`classify_file` 按 profile `test_paths`、`extract_symbols` 用通用标识符正则、`score_suggestion` 按 severity、`keywords` 返回空集、`render_source_block` 返回带行号源码、`normalize/validate` 返回透传/`{"action": "post"}`。构造函数签名与 `PythonPolicy` 一致（`profile, cfg, read_lines`）。

- [ ] **Step 4: suggestion_actions 注入关键字**

`_extract_identifiers(code, keywords: set[str])` 与内部调用点全部传入 policy 关键字；`suggestion_actions` 的公开函数新增可选参数 `keywords: set[str] | None = None`，`None` 时使用原 `_KEYWORDS` 常量保持旧行为。测试 `test_suggestion_actions.py` 不需修改。

- [ ] **Step 5: 写 policy 测试**

```python
# tests/test_policy_python.py
from reviewagent.config import current
from reviewagent.review.policies.python import PythonPolicy
from reviewagent.scenario.profile import load_profile


def _policy():
    return PythonPolicy(profile=load_profile(None), cfg=current(), read_lines=lambda p: [])


def test_score_matches_legacy_table():
    p = _policy()
    assert p.score_suggestion({"severity": "high", "label": "security",
                               "rationale": "x" * 210, "header": "h",
                               "ref": "SSD-RULE-X"}) == 100


def test_extract_symbols_ignores_python_keywords():
    p = _policy()
    assert p.extract_symbols("+def foo(bar):\n+    return bar") == ["foo", "bar"]


def test_keywords_contains_python_keyword_set():
    assert {"if", "self", "None"} <= _policy().keywords()
```

- [ ] **Step 6: 运行全量测试**

Run: `.venv/bin/pytest -q`
Expected: 全绿；门面尚未切换到 policy 的方法仍走旧实现，但新模块测试通过。

- [ ] **Step 7: 提交**

```bash
git add reviewagent/review reviewagent/commands/improve.py reviewagent/commands/suggestion_actions.py tests/test_policy_python.py
git commit -m "feat(review): LanguagePolicy 协议、registry 与 Python policy"
```

---

## Task 8: review/planner.py

**Files:**
- Create: `reviewagent/review/planner.py`
- Modify: `reviewagent/commands/improve.py`（门面委托 `_sample_case_files` / `_apply_file_count_cap` / `_assign_strategy` / `_single_file_over_full_budget` / `_full_source_budget_bytes`）
- Test: `tests/test_planner.py`

**Interfaces:**
- Produces: `Plan`（`files_to_review`, `strategies`, `batches`, `skipped`, `prior_by_file`, `file_priorities`, `new_files`, `cross_reuse`, `single_file`, `overflow`）。
- Produces: `plan(files, diff_by_file, *, profile, cfg, policy, prior_states, head_sha, blob_sha, read_line_count, stat_bytes, rule_block_bytes, profile_fingerprint=None, ruleset_fingerprint=None) -> Plan`。
- `assign_strategy(sorted_files, file_meta, overflow_set, *, cfg, stat_bytes, budget_fn)`。
- `single_file_over_full_budget(files, file_meta, *, stat_bytes, budget_fn) -> bool`（原 `_single_file_over_full_budget`）。

- [ ] **Step 1: 写测试（顺序与降级）**

```python
# tests/test_planner.py
from reviewagent.config import current
from reviewagent.review.planner import assign_strategy


def test_strategy_order_matches_legacy(monkeypatch):
    cfg = current()
    out = assign_strategy(
        ["core.py"], {"core.py": (10, 2100)}, set(),
        cfg=cfg, stat_bytes=lambda p: 100, budget_fn=lambda n: 40_000,
    )
    assert out["core.py"] == "partial"


def test_overflow_forces_patch():
    out = assign_strategy(["big.py"], {"big.py": (10, 2100)}, {"big.py"},
                          cfg=current(), stat_bytes=lambda p: 1, budget_fn=lambda n: 1)
    assert out["big.py"] == "patch"
```

- [ ] **Step 2: 迁移编排**

把 `_call_agent` 中 `:204-330` 的过滤/打分/抽样/截断、`:363-455` 增量复用判定、`:1307-1655` 策略与批处理，按原顺序迁入 `plan()`；所有 `config.*` 读取改为 `cfg` 入参。保持：

1. `review_exclude_extensions` 过滤；
2. review_mode 测试路径跳过（`cfg.improve_skip_test_paths`）；
3. `_sample_case_files` 确定性抽样；
4. priority 计算（`math.log1p` + keyword + density + test_feature + new_file）；
5. `_apply_file_count_cap`；
6. 策略分配；
7. 批组合并；
8. 指纹失效：`prior_states[fp]` 里保存的 `profile_fingerprint` / `ruleset_fingerprint` 与入参不一致时，该文件不进入复用集合，记录 `logger.info("review.state_invalidated project={} mr={} file={} reason=profile|ruleset")`；入参为 `None` 时（Task 15 之前）保持只按 blob 复用，行为不变。

- [ ] **Step 3: 门面委托（晚绑定 config）**

```python
    def _assign_strategy(self, sorted_files, file_meta, overflow_set):
        from reviewagent.review.planner import assign_strategy as _assign

        cfg = globals()["config"]
        return _assign(
            sorted_files, file_meta, overflow_set, cfg=cfg,
            stat_bytes=self._stat_file_bytes,
            budget_fn=self._full_source_budget_bytes,
        )

    def _full_source_budget_bytes(self, diff_lines: int) -> int:
        from reviewagent.review.planner import full_source_budget_bytes

        cfg = globals()["config"]
        rule_bytes = len(self._rule_block_text().encode("utf-8"))
        return full_source_budget_bytes(diff_lines, cfg=cfg, rule_bytes=rule_bytes,
                                        repo_context=getattr(self, "repo_context", "") or "")

    def _single_file_over_full_budget(self, files, file_meta):
        from reviewagent.review.planner import single_file_over_full_budget

        over = single_file_over_full_budget(
            files, file_meta,
            stat_bytes=self._stat_file_bytes,
            budget_fn=self._full_source_budget_bytes,
        )
        if over:
            diff_lines, file_lines = file_meta.get(files[0], (0, 0))
            logger.info(
                "improve.full_source_budget_downgrade project={} mr={} file={} "
                "file_bytes={} est_source_bytes={} budget={} -> strategy path (single-file)",
                self.project_id, self.mr_iid, files[0], self._stat_file_bytes(files[0]),
                self._stat_file_bytes(files[0]) + file_lines * 7,
                self._full_source_budget_bytes(diff_lines),
            )
        return over
```

`_rule_block_text()` 惰性取 `active_rule_set().verbatim_body`；失败时返回 `""`（与旧实现 `except: pass` 一致）。

- [ ] **Step 4: 运行策略/预算测试**

Run: `.venv/bin/pytest tests/test_improve_strategy.py tests/test_planner.py -q`
Expected: PASS（`test_full_source_budget_three_modes` 数值不变）。

- [ ] **Step 5: 提交**

```bash
git add reviewagent/review/planner.py reviewagent/commands/improve.py tests/test_planner.py
git commit -m "refactor(review): 文件规划/优先级/策略迁移至 planner"
```

---

## Task 9: review/context.py + review/assembly.py（prompt golden）

**Files:**
- Create: `reviewagent/review/context.py`, `reviewagent/review/assembly.py`
- Create: `profiles/ssd-python-autotest/prompts/chunk.md`, `batch.md`, `rules_repo.md`, `rules_general.md`
- Create: `profiles/ssd-python-autotest/prompts/dynamic/cross_file_refs_empty.md`, `cross_file_refs_refs.md`
- Modify: `reviewagent/commands/improve.py`（`_build_chunk_prompt` / `_build_batch_prompt` / source blocks / cross-file render 委托）
- Test: `tests/golden/test_prompt_golden.py`

**Interfaces:**
- Produces: `ChunkPromptParts`、`BatchPromptParts`、`SectionParts`（frozen dataclass）。
- Produces: `build_chunk_prompt(parts: ChunkPromptParts, *, profile) -> str`、`build_batch_prompt(parts: BatchPromptParts, *, profile) -> str`。
- Produces: `build_rules_block(repo_rules: str, general_rules: str, *, profile) -> str`、`build_source_block(ctx, strategy, *, cfg, policy) -> str`、`context.render_cross_file_section(refs, *, profile) -> str`（Task 14 再加 `evidence` 关键字参数，默认 `None` 保持本节签名可用）。
- 模板每次从 `profile.root` 读取（`load_template` 做路径逃逸校验）；golden 测试依赖磁盘原文，故不加进程内缓存。

- [ ] **Step 1: 写模板（含精确空行/围栏）**

````markdown
<!-- profiles/ssd-python-autotest/prompts/chunk.md -->
{{cross_file_section}}{{rules_block}}{{prior_section}}## 本次检视文件: `{{file_path}}`

### diff
```diff
{{file_diff}}
```

{{source_block}}

### VALID NEW LINES（start_line 只能从此取）

{{valid_lines}}

## 输出

按 system prompt 输出 JSON。**summary_md 输出空字符串 `""`**，只输出本文件的 suggestions。
````

````markdown
<!-- profiles/ssd-python-autotest/prompts/batch.md -->
{{rules_block}}## 本次检视共 {{file_count}} 个文件 (小改动合并批次)

**每条 suggestion 的 `file` 字段只能从以下路径中选:**
{{file_list}}

---

{{sections}}

## 输出

按 system prompt 输出 JSON。**summary_md 输出空字符串 `""`**。
输出本批次全部文件的 suggestions 合并列表; 每条必须带正确的 `file` 字段 (file 不在上述清单中的 suggestion 会被丢弃)。
````

````markdown
<!-- profiles/ssd-python-autotest/prompts/rules_repo.md -->
## 🔴 优先 2 — SSD 自定义规则 (项目方定义)

先扫下面 SSD 规则命中, 命中即产 suggestion, rationale 引用规则键:

{{repo_rules}}

---

{{general_rules}}

````
（该文件末尾必须保留一个空行，渲染结果为 `...\n\n`。`load_template(..., trim_trailing_newline=False)`。）

````markdown
<!-- profiles/ssd-python-autotest/prompts/rules_general.md -->
{{general_rules}}

````
（末尾保留一个空行，渲染结果为 `...\n\n`。）

````markdown
<!-- profiles/ssd-python-autotest/prompts/dynamic/cross_file_refs_empty.md -->
## 🟠 优先 1 — 跨文件影响分析 (规则检查之前必做, **P1**)

Python 端 rg 没找到 cross-file caller 引用 (本文件可能是新增 / 改动是局部 / 仓库无其他文件引用).

**仍请按 P1 优先级自行判断是否需要产 R-OTHER-IMPACT suggestion** — Python 端仅做粗扫，如下情况仍可能有跨文件风险:
- 新增 / 重命名的公共函数、类、常量 (无 caller 但下游模块可能依赖)
- 改了 fixture / 测试 helper (本 diff 未引用但其它 test 可能引用)
- 改了 import 路径 (旧路径可能还有引用未被发现)
- 改了 SQL / ORM schema (model/migration 可能未同步)

确认无风险 → 在 summary_md 注明 '未发现 cross-file 关联'。**不要为凑数硬编 R-OTHER-IMPACT**。

````
（末尾保留空行，渲染结果以 `\n\n` 结束。）

````markdown
<!-- profiles/ssd-python-autotest/prompts/dynamic/cross_file_refs_refs.md -->
## 🟠 优先 1 — 跨文件影响分析 (Python 端已 grep, **P1, 在所有规则检查之前**)

在 worktree 找到 {{refs_count}} 条其他文件对本文件改动的引用:

{{refs_block}}**请逐一分析每条 caller 是否需要同步更新**：
- 函数签名变了但 caller 没传新参数 / 类型不匹配 → 产 suggestion, `label: cross-file impact`
- 常量改了引用方没同步 → 产 suggestion, `label: cross-file impact`
- 删除/重命名的函数别处还在用 → 写进 summary_md 文字, 格式 `> 跨文件影响: <文件> L<行号> <问题>`
- 跨文件影响类问题**不要求先命中 R-XXX 20 类**, 命中即产 suggestion
- `rationale` 必须以 `R-OTHER-IMPACT:<简短描述>` 开头 (例 `R-OTHER-IMPACT:caller_param` / `R-OTHER-IMPACT:schema_drift` / `R-OTHER-IMPACT:import_path` / `R-OTHER-IMPACT:fixture_break`)
- **严格只用于跨文件影响** — 同文件内的资源/异常/循环问题应归 R-RES / R-LOOP / R-ERR 等, 不要错归 R-OTHER-IMPACT

````
（`{{refs_block}}` 由 context 生成，每条格式 `` **`{file}:{line}`** 引用 `{ident}`:\n```\n{content}\n```\n\n ``；它与下一行 `**请逐一` 之间不额外加空行，靠每条自带的 `\n\n`。）

- [ ] **Step 2: 写 golden 测试**

```python
# tests/golden/test_prompt_golden.py
import json
from pathlib import Path

import pytest

from reviewagent.scenario.profile import load_profile

ROOT = Path("tests/golden/prompts/ssd-python-autotest")
MANIFEST = json.loads(Path("tests/golden/manifest.json").read_text())


def _section(name: str) -> str:
    path = ROOT / name
    assert path.exists()
    return path.read_text(encoding="utf-8")


def test_goldens_exist_and_are_stable():
    for name, expected in MANIFEST.items():
        import hashlib

        assert hashlib.sha256(_section(name).encode()).hexdigest() == expected


def test_profile_prompts_are_byte_equal_to_legacy():
    profile = load_profile(None)
    for name in ("improve", "describe"):
        legacy = Path(f"reviewagent/prompts/{name}.md").read_text(encoding="utf-8")
        current = (profile.root / profile.prompts[name]).read_text(encoding="utf-8")
        assert current == legacy
```

- [ ] **Step 3: 实现 context/assembly**

把 `_build_full_source_block` / `_build_partial_context_block` / `_build_patch_only_block`（`:1656-1750`）迁入 `context.py`，读取经 `read_lines` 注入；把 `_render_cross_file_section` 的两段文案改为模板；`assembly.py` 只做变量替换。

**归属边界**：`context.build_source_block` 决定块标题、读写行、±N 合并、5000 行截断、前 5 段上限与最终拼接；`policy.render_source_block` 只把切好的 `FileContext(start_line=lo)` 渲染成 `NNNN| line` 片段。这样 C/C++ 只需换行号格式/缩进，不会复制分段与预算逻辑：

```python
# reviewagent/review/context.py
def _fragment(policy, lines, lo: int, strategy: str) -> str:
    return policy.render_source_block(
        FileContext(file_path="", lines=tuple(lines), valid_lines=frozenset(),
                    start_line=lo),
        strategy, 0,
    )


def _merge_ranges(valid_lines, n: int, total: int) -> list[list[int]]:
    ranges = [(max(1, v - n), min(total, v + n)) for v in valid_lines]
    merged: list[list[int]] = []
    for lo, hi in sorted(ranges):
        if merged and lo <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    return merged


def build_source_block(ctx: FileContext, strategy: str, *, cfg, policy) -> str:
    """标题/分段/截断与 improve.py:1656-1750 逐字节一致；行号格式交 policy。"""
    file_path = ctx.file_path
    lines = list(ctx.lines)
    if strategy == "full":
        if not lines:
            return f"### 完整源码\n(无法读取 {file_path})"
        total = len(lines)
        if total > 5000:
            numbered = _fragment(policy, lines[:5000], 1, "full")
            return (
                f"### 完整源码: `{file_path}` (共 {total} 行, **已截断到前 5000 行**)\n"
                f"⚠️ 末尾 {total - 5000} 行未加载, 如需检视请缩小 diff 范围或拆分 MR\n```\n{numbered}\n```"
            )
        numbered = _fragment(policy, lines, 1, "full")
        return f"### 完整源码: `{file_path}` (共 {total} 行)\n```\n{numbered}\n```"

    if strategy == "partial":
        if not lines:
            return f"### 上下文 (partial)\n(无法读取 {file_path})"
        n = cfg.improve_partial_context_lines
        merged = _merge_ranges(sorted(ctx.valid_lines), n, len(lines))
        blocks = [_fragment(policy, lines[lo - 1:hi], lo, "partial") for lo, hi in merged]
        return (
            f"### diff 上下文 (partial, ±{n} 行): `{file_path}`\n"
            f"⚠️ 此文件超出 IMPROVE_FULL_FILES={cfg.improve_full_files} 配额, 已降级为 partial 模式\n"
            f"⚠️ 仅显示 diff 附近 {len(merged)} 段 (±{n} 行), 跨段语义识别能力下降\n"
            f"```\n" + "\n...\n".join(blocks) + "\n```"
        )

    n = cfg.improve_patch_context_lines
    vl = sorted(ctx.valid_lines)
    vl_display = vl[:20]
    vl_suffix = "..." if len(vl) > 20 else ""
    context_block = ""
    if lines and n > 0:
        merged = _merge_ranges(vl, n, len(lines))
        blocks = [_fragment(policy, lines[lo - 1:hi], lo, "patch") for lo, hi in merged[:5]]
        omitted = len(merged) - 5 if len(merged) > 5 else 0
        context_block = (
            f"\n### 最小上下文 (patch, ±{n} 行, 前 {len(merged[:5])} 段)\n"
            f"```\n" + "\n...\n".join(blocks) + "\n```\n"
            + (f"(另有 {omitted} 段未展示)\n" if omitted else "")
        )
    return (
        f"### patch-only: `{file_path}`\n"
        f"⚠️ 已降级为 patch-only (overflow 或低优先级), 仅 diff + ±{n} 行最小 context\n"
        f"⚠️ 跨行/跨段语义识别能力有限, 优先识别行内问题 (裸 except / 可变默认参数 / 硬编码等)\n"
        f"建议关注行: {vl_display}{vl_suffix}\n"
        f"{context_block}"
    )
```

```python
# reviewagent/review/assembly.py
from dataclasses import dataclass


@dataclass(frozen=True)
class ChunkPromptParts:
    cross_file_section: str
    rules_block: str
    prior_section: str
    file_path: str
    file_diff: str
    source_block: str
    valid_lines: str


def build_chunk_prompt(parts: ChunkPromptParts, *, profile) -> str:
    from reviewagent.scenario.prompts import load_template, render_template

    template = load_template("prompts/chunk.md", profile, trim_trailing_newline=True)
    return render_template(template, {
        "cross_file_section": parts.cross_file_section,
        "rules_block": parts.rules_block,
        "prior_section": parts.prior_section,
        "file_path": parts.file_path,
        "file_diff": parts.file_diff,
        "source_block": parts.source_block,
        "valid_lines": parts.valid_lines,
    }, template_path="prompts/chunk.md")


@dataclass(frozen=True)
class BatchPromptParts:
    rules_block: str
    file_count: int
    file_list: str     # "- `a.py`\n- `b.py`"
    sections: str      # 各文件 section 以 "\n\n---\n\n" 连接


def build_batch_prompt(parts: BatchPromptParts, *, profile) -> str:
    from reviewagent.scenario.prompts import load_template, render_template

    template = load_template("prompts/batch.md", profile, trim_trailing_newline=True)
    return render_template(template, {
        "rules_block": parts.rules_block,
        "file_count": parts.file_count,
        "file_list": parts.file_list,
        "sections": parts.sections,
    }, template_path="prompts/batch.md")
```

规则块由 `build_rules_block(repo_rules_text, general_rules_text, *, profile)` 实现，两条路径共用：

```python
def build_rules_block(repo_rules: str, general_rules: str, *, profile) -> str:
    from reviewagent.scenario.prompts import load_template, render_template

    if repo_rules:
        template = load_template("prompts/rules_repo.md", profile)
        return render_template(template, {"repo_rules": repo_rules, "general_rules": general_rules},
                               template_path="prompts/rules_repo.md")
    template = load_template("prompts/rules_general.md", profile)
    return render_template(template, {"general_rules": general_rules},
                           template_path="prompts/rules_general.md")
```

- [ ] **Step 4: 门面委托并跑 golden**

`_build_chunk_prompt` 保留原签名与 `cross_file_refs is None` 的 fallback，装配改为调用 `assembly`；`_build_batch_prompt` 同样。运行：

Run: `.venv/bin/pytest tests/golden/test_prompt_golden.py -q`
Expected: PASS。若字节不一致，用 `git diff --no-index` 对比 golden 与现场输出，修正模板空行/尾随换行，禁止改 golden 文件。

- [ ] **Step 5: 运行全量测试**

Run: `.venv/bin/pytest -q`
Expected: 全绿。

- [ ] **Step 6: 提交**

```bash
git add reviewagent/review/context.py reviewagent/review/assembly.py profiles/ssd-python-autotest/prompts reviewagent/commands/improve.py tests/golden/test_prompt_golden.py
git commit -m "refactor(review): prompt/source 装配迁入 context/assembly，golden 通过"
```

---

## Task 10: review/runner.py + ImproveCommand 薄壳门面

**Files:**
- Create: `reviewagent/review/runner.py`
- Modify: `reviewagent/commands/improve.py`（`_call_agent` 仅组装依赖 + 调用 runner）
- Modify: `reviewagent/commands/_common.py`（仅在必要时暴露回调）
- Test: `tests/test_runner_facade.py`

**Interfaces:**
- Produces: `RunContext`（`project_id`, `mr_iid`, `ws`, `repo_context`, `cfg`, `profile`, `rule_set`, `dynamic_rules`, `policy`, `callbacks`）。
- Produces: `Callbacks`（`split_diff`, `diff_line_map`, `read_lines`, `read_line_count`, `stat_bytes`, `blob_sha`, `head_sha`, `collect_cross_refs`, `call_chunk`, `build_user_prompt`, `super_call_agent`）。
- Produces: `run(ctx: RunContext) -> dict[str, Any]`（返回 `_merge_chunks` 结果）。
- 门面 `_call_agent(ws)` 惰性构建 `RunContext`。

- [ ] **Step 1: 写门面构造测试**

```python
# tests/test_runner_facade.py
from types import SimpleNamespace

from reviewagent.commands.improve import ImproveCommand


def test_new_without_init_can_resolve_dependencies():
    cmd = ImproveCommand.__new__(ImproveCommand)
    cmd.project_id, cmd.mr_iid = 1, 2
    cmd.repo_context = ""
    cmd.ws = SimpleNamespace(worktree=None, diff_file=None)
    profile, rule_set, policy = cmd._lazy_deps()
    assert profile.name == "ssd-python-autotest"
    assert rule_set.verbatim_body
    assert policy.keywords()
```

- [ ] **Step 2: 实现 runner**

按现有 `_call_agent` 顺序逐段迁移（源 `improve.py:188-620`），保持：

1. 扩展名/测试路径过滤与 skipped 分类；
2. 单文件捷径：`plan.single_file` 且未超预算 → `ctx.callbacks.super_call_agent(ctx.ws)` 直接返回；
3. 预算/策略/批次；
4. 动态证据预采集；
5. 复用建议/锚点；
6. `ThreadPoolExecutor` 调 `ctx.callbacks.call_chunk(prompt, ws, fp)`（保持 3 个位置参数）；
7. `_merge_chunks` 静态实现（从门面 re-export，runner 直接调用 `validation.merge_chunks` 或 `commands.improve._merge_chunks` 不应反向依赖——把实现放在 `review/runner.py`，门面 staticmethod 委托）。

- [ ] **Step 3: 门面薄壳**

```python
    def _lazy_deps(self):
        from reviewagent.review.policies.registry import get as get_policy
        from reviewagent.scenario.profile import active_or_load
        from reviewagent.scenario.rules.resolver import assemble_base

        profile = getattr(self, "_profile", None) or active_or_load()
        rule_set = getattr(self, "_rule_set", None) or assemble_base(profile)
        policy = getattr(self, "_policy", None) or get_policy(
            profile.language, profile=profile, cfg=globals()["config"], read_lines=self._read_file_lines,
        )
        self._profile, self._rule_set, self._policy = profile, rule_set, policy
        return profile, rule_set, policy

    def _call_agent(self, ws):
        from reviewagent.review.runner import Callbacks, RunContext, run

        profile, rule_set, policy = self._lazy_deps()
        ctx = RunContext(
            project_id=self.project_id, mr_iid=self.mr_iid, ws=ws,
            repo_context=getattr(self, "repo_context", "") or "",
            cfg=globals()["config"], profile=profile, rule_set=rule_set,
            dynamic_rules=getattr(self, "_dynamic_rules", None), policy=policy,
            callbacks=Callbacks(
                split_diff=self._split_diff_by_file,
                diff_line_map=self._diff_line_map,
                read_lines=self._read_file_lines,
                read_line_count=self._read_file_line_count,
                stat_bytes=self._stat_file_bytes,
                blob_sha=self._file_blob_sha,
                head_sha=self._get_mr_head_sha,
                collect_cross_refs=self._collect_cross_file_refs_for_mr,
                call_chunk=self._call_chunk,
                build_user_prompt=self._build_user_prompt,
                super_call_agent=lambda ws_: super(ImproveCommand, self)._call_agent(ws_),
            ),
        )
        return run(ctx)
```

- [ ] **Step 4: 运行全量测试**

Run: `.venv/bin/pytest -q`
Expected: 全绿，包括 `test_improve_strategy.py` 10 处 `_collect_cross_file_refs_for_mr` patch、单文件捷径、token summary。

- [ ] **Step 5: 提交**

```bash
git add reviewagent/review/runner.py reviewagent/commands/improve.py reviewagent/commands/_common.py tests/test_runner_facade.py
git commit -m "refactor(review): 编排迁入 runner，ImproveCommand 收敛为兼容门面"
```

---

## Task 11: review/validation.py + suggestion_actions 策略注入

**Files:**
- Create: `reviewagent/review/validation.py`
- Modify: `reviewagent/commands/improve.py`（`_validate_suggestion` / `_score_suggestion` / `_normalise_suggestion` / `_suggestion_fingerprint` 委托）
- Modify: `reviewagent/commands/suggestion_actions.py`（policy 注入）
- Test: `tests/test_validation_module.py`

**Interfaces:**
- Produces: `suggestion_fingerprint(...)`、`suggestion_content_fingerprint(...)`、`validate_suggestion(sug, *, ctx: FileContext, policy, rule_set, cfg, find_line, file_sources, line_map) -> RiskVerdict`。
- `file_sources: dict[str, list[str]]` 与 `line_map: dict[str, set[int]]` 是请求级缓存，由 runner 持有并原样传入；`FileContext` 不携带缓存（避免同一文件多 chunk 复制大对象）。
- Produces: `merge_chunks(...)`（原 `_merge_chunks` 实现）。
- `_RULE_REF_REGEX` / `_RULE_EXEMPT_REGEX` 在门面按默认 profile 生成，运行时使用 `rule_set.key_patterns`。

- [ ] **Step 1: 写校验顺序测试**

```python
# tests/test_validation_module.py
from reviewagent.config import current
from reviewagent.review.policies.python import PythonPolicy
from reviewagent.review.types import FileContext
from reviewagent.review.validation import validate_suggestion
from reviewagent.scenario.profile import load_profile
from reviewagent.scenario.rules.resolver import assemble_base

LINES = ("import os", "def f():", "    return open('x').read()")


def _ctx():
    return FileContext(file_path="core.py", lines=LINES, valid_lines=frozenset({3}),
                       diff="+    return open('x').read()")


def test_drop_when_file_not_in_diff():
    profile = load_profile(None)
    policy = PythonPolicy(profile=profile, cfg=current(), read_lines=lambda p: [])
    verdict = validate_suggestion(
        {"file": "other.py", "start_line": 1, "existing_code": "x", "improved_code": "y"},
        ctx=_ctx(), policy=policy, rule_set=assemble_base(profile),
        cfg=current(), find_line=lambda *a, **k: 0,
        file_sources={"core.py": list(LINES)},
        line_map={"core.py": {3}},
    )
    assert verdict["action"] == "drop"
```

- [ ] **Step 2: 迁移实现并保持门面 staticmethod**

把 `improve.py` 的 `_suggestion_fingerprint` / `_suggestion_content_fingerprint` / `_validate_suggestion` / `_merge_chunks` / `_score_suggestion` / `_normalise_suggestion` / `_code_first_line_matches` 原样迁入；门面：

```python
    @staticmethod
    def _merge_chunks(results, *, skipped_files=None, file_priorities=None, new_files=None, cap_truncated=None):
        from reviewagent.review.validation import merge_chunks

        return merge_chunks(results, skipped_files=skipped_files, file_priorities=file_priorities,
                            new_files=new_files, cap_truncated=cap_truncated)

    @staticmethod
    def _score_suggestion(s):
        from reviewagent.review.policies.python import score_suggestion
        from reviewagent.scenario.profile import active_or_load

        prefix = active_or_load().rule_keys.get("repo_prefix", "SSD")
        return score_suggestion(s, rule_prefix=prefix)
```

`_normalise_suggestion` 保持 staticmethod 并委托 `normalise_suggestion`。

- [ ] **Step 3: suggestion_actions 注入**

`run_suggestion_action(...)` 等入口接收 `policy`（可选）；门面/worker 调用时传 `get_policy(profile.language, ...)`。`keywords=None` 时回退旧集合，保证现有测试零改动。

- [ ] **Step 4: 运行测试**

Run: `.venv/bin/pytest tests/test_improve_alignment.py tests/test_suggestion_actions.py tests/test_validation_module.py tests/test_apply_risk_check.py -q && .venv/bin/pytest -q`
Expected: 全绿。

- [ ] **Step 5: 提交**

```bash
git add reviewagent/review/validation.py reviewagent/commands/improve.py reviewagent/commands/suggestion_actions.py tests/test_validation_module.py
git commit -m "refactor(review): 建议校验/合并迁入 validation，采纳判定注入 policy"
```

---

## Task 12: review/publishing.py + reporting profile 化

**Files:**
- Create: `reviewagent/review/publishing.py`
- Create: `profiles/ssd-python-autotest/reporting/rule_actions.yaml`
- Modify: `reviewagent/commands/improve.py`（`_publish` / summary 委托）
- Modify: `reviewagent/reporting/rule_translate.py`, `renderer.py`, `collectors/repo_scan.py`, `weekly.py`（标题）
- Test: `tests/test_publishing_module.py`, 现有 reporting 测试

**Interfaces:**
- Produces: `publish(ctx, merged, *, rule_set, profile, cfg, callbacks) -> dict`。
- Produces: `build_summary_v2(...)`、`build_summary_placeholder(...)`、`make_token_summary(...)`。
- Produces: `load_rule_actions(profile) -> dict[str, str]`。

- [ ] **Step 1: 迁移发布与 cohort**

把 `improve.py:107-170, 2423-2904`（`_handle_cohort_reoccurrence`、`_publish`、overview 刷新、placeholder 编辑、pre-reconcile、late-detect）原样迁入 `publishing.py`；`SSD-RULE-FORBIDDEN-WILDCARD-IMPORT` 后处理改为 `rule_set.normalize_keys(header + " " + rationale)`。规则正则改用 `rule_set.key_patterns`。门面 `_publish` 委托，保留原签名。

- [ ] **Step 2: reporting 数据化**

```yaml
# profiles/ssd-python-autotest/reporting/rule_actions.yaml
SSD-RULE-TYPEHINTS: "启用 ruff `ANN`/mypy strict 加入 CI, 一次性消存量"
SSD-RULE-DOCSTRING-REQUIRED: "在 pre-commit 跑 ruff `D` 系列, docstring 缺失直接 fail"
SSD-RULE-NO-LOG-EXC: "用 ruff `BLE` + `S` (logging 规范) 加 CI, 对裸 `except Exception` 报错"
SSD-RULE-NO-BARE-PRINT: "用 ruff `T201` (禁止 print) 加入 CI 阻断列表"
SSD-RULE-NO-MUTABLE-DEFAULT: "用 ruff `B006` 拦截可变默认参数"
SSD-RULE-RESOURCE-CONTEXT-MANAGER: "用 ruff `SIM`/`PTH123` 拦截裸 open/close, 强制 with 语句"
SSD-RULE-FORBIDDEN-COMMENT: "把无效注释扫一遍存 issue, 用 ruff `ERA` 抑制注释式代码"
SSD-RULE-FORBIDDEN-WILDCARD-IMPORT: "用 ruff `F401`/`F403` 拦截 `from foo import *`"
R-LOOP: "把『循环边界/无限循环』作为 review checklist 红线条款"
R-RES: "扫一遍 `open/requests/socket` 调用, 强制 `with` + 超时"
R-ERR: "把裸 `except:` 与静默 `pass` 列为硬错误, 加入 review 范本"
R-SHELL: "对所有 shell 调用加超时 + 异常类型细化 + sandbox 跑"
R-CI: "对并行用例加临时目录 PID 隔离 / 串行锁, 修 flaky"
R-OTHER-IMPACT: "对跨文件函数签名变化在 MR 描述里点出 caller, 拉对应 owner review"
```

`renderer.py` 删除 `_RULE_ACTIONS` 常量，改为 `load_rule_actions(profile)`（进程内按 profile 指纹缓存）；`repo_scan.py` 的局部 `code_exts` / `priority_names` 改读 `profile.reporting["repo_scan_extensions"]` / `profile.reporting["repo_scan_entry_files"]`；`rule_translate.py` 的 `_PROMPTS_DIR` / `_load_generic_map` 改为 `RuleSet.index_by_key` + profile `improve.md` 的 R-* 自由前缀；weekly 标题改读 `profile.reporting["weekly_title"]`。

- [ ] **Step 3: 运行 reporting 测试**

Run: `.venv/bin/pytest tests/test_rule_translate_xxx.py tests/test_telemetry_section_render.py tests/test_build_overview_body.py tests/test_cohort_dedup.py tests/test_publishing_module.py -q`
Expected: 全绿，输出快照不变。

- [ ] **Step 4: 提交**

```bash
git add reviewagent/review/publishing.py profiles/ssd-python-autotest/reporting reviewagent/reporting reviewagent/commands/improve.py tests/test_publishing_module.py
git commit -m "refactor(review): 发布/cohort 迁入 publishing，reporting 读取 profile 数据"
```

---

## Task 13: 动态规则加载与执行骨架

**Files:**
- Create: `reviewagent/scenario/dynamic.py`
- Create: `reviewagent/review/dynamic/__init__.py`, `model.py`, `registry.py`, `runner.py`
- Test: `tests/test_dynamic_rules.py`

**Interfaces:**
- Produces: `DynamicRule`、`DynamicRuleSet`（`rules` / `fingerprint` / `with_rule(rule) -> DynamicRuleSet`）、`DynamicEvidence`。
- Produces: `load_dynamic_rules(profile) -> DynamicRuleSet`。
- Produces: `registry.register(name, provider)`、`registry.get(name)`。
- Produces: `run_dynamic(ruleset, *, files, diff_by_file, worktree, timeout_s, fetch_callback=None) -> dict[str, DynamicEvidence]`。

- [ ] **Step 1: 写加载与降级测试**

```python
# tests/test_dynamic_rules.py
from dataclasses import replace

import pytest

from reviewagent.review.dynamic.model import DynamicEvidence
from reviewagent.review.dynamic.registry import register
from reviewagent.review.dynamic.runner import run_dynamic
from reviewagent.scenario.dynamic import load_dynamic_rules
from reviewagent.scenario.profile import load_profile


class _Boom:
    def fetch(self, **kwargs):
        raise TimeoutError("rg timeout")


def test_load_declared_rule():
    ruleset = load_dynamic_rules(load_profile(None))
    assert ruleset.rules[0].key == "DYN-CROSS-FILE-REFS"
    assert ruleset.rules[0].provider == "cross_file_refs"


def test_missing_dynamic_rules_file_fails(tmp_path):
    profile = load_profile(None)
    bad = replace(profile, dynamic_rules="missing.yaml")
    with pytest.raises((ValueError, FileNotFoundError)):
        load_dynamic_rules(bad)


def test_provider_failure_degrades():
    register("boom", lambda **kw: _Boom())
    rules = load_dynamic_rules(load_profile(None))
    rule = replace(rules.rules[0], provider="boom")
    out = run_dynamic(rules.with_rule(rule), files=["core.py"], diff_by_file={}, worktree=None, timeout_s=1)
    evidence = out["DYN-CROSS-FILE-REFS"]
    assert evidence.degraded is True
    assert isinstance(evidence, DynamicEvidence)
```

- [ ] **Step 2: 实现模型与加载器**

```python
# reviewagent/scenario/dynamic.py
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

import re

from reviewagent.review.dynamic.model import DynamicRule, DynamicRuleSet

_KEY = re.compile(r"^DYN-[A-Z0-9-]+$")
_ALLOWED = {"schema_version", "rules"}
_RULE_ALLOWED = {"key", "title", "provider", "enabled", "template", "params", "limits"}


def load_dynamic_rules(profile) -> DynamicRuleSet:
    rel = profile.dynamic_rules
    if not rel:
        return DynamicRuleSet(rules=(), fingerprint="")
    path = (profile.root / rel).resolve()
    if profile.root.resolve() not in path.parents:
        raise ValueError(f"dynamic_rules escapes profile root: {rel}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError(f"unsupported dynamic_rules schema in {path}")
    unknown = set(data) - _ALLOWED
    if unknown:
        raise ValueError(f"unknown dynamic_rules fields {sorted(unknown)}")
    rules = []
    seen = set()
    for raw in data.get("rules") or []:
        unknown = set(raw) - _RULE_ALLOWED
        if unknown:
            raise ValueError(f"unknown dynamic rule fields {sorted(unknown)}")
        key = raw.get("key", "")
        if not _KEY.match(key):
            raise ValueError(f"invalid dynamic rule key: {key!r}")
        if key in seen:
            raise ValueError(f"duplicate dynamic rule key: {key}")
        seen.add(key)
        template = raw.get("template")
        if template and not (profile.root / template).exists():
            raise ValueError(f"dynamic template not found: {template}")
        rules.append(DynamicRule(
            key=key, title=raw.get("title", key), provider=raw["provider"],
            enabled=bool(raw.get("enabled", True)), template=template,
            params=dict(raw.get("params") or {}), limits=dict(raw.get("limits") or {}),
        ))
    return DynamicRuleSet(rules=tuple(rules), fingerprint=str(path.stat().st_mtime_ns))
```

模型补充：

```python
# reviewagent/review/dynamic/model.py（节选）
from dataclasses import dataclass, field, replace
from typing import Any


@dataclass(frozen=True)
class DynamicRule:
    key: str
    title: str
    provider: str
    enabled: bool = True
    template: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    limits: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DynamicEvidence:
    rule_key: str
    provider: str
    refs: tuple[dict[str, Any], ...] = ()
    degraded: bool = False
    reason: str = ""


@dataclass(frozen=True)
class DynamicRuleSet:
    rules: tuple[DynamicRule, ...]
    fingerprint: str

    def with_rule(self, rule: DynamicRule) -> "DynamicRuleSet":
        merged = tuple(rule if r.key == rule.key else r for r in self.rules)
        return replace(self, rules=merged, fingerprint=f"{self.fingerprint}:{rule.key}")
```

- [ ] **Step 3: 实现 runner**

```python
# reviewagent/review/dynamic/runner.py
from __future__ import annotations

import threading

from reviewagent.logging_setup import logger
from reviewagent.review.dynamic.model import DynamicEvidence, DynamicRuleSet
from reviewagent.review.dynamic.registry import get as get_provider


def _run_one(rule, *, files, diff_by_file, worktree, fetch_callback):
    provider = get_provider(rule.provider, fetch_callback=fetch_callback)
    refs = provider.fetch(files=files, diff_by_file=diff_by_file, worktree=worktree, params=rule.params)
    return DynamicEvidence(rule_key=rule.key, provider=rule.provider, refs=tuple(refs))


def run_dynamic(ruleset: DynamicRuleSet, *, files, diff_by_file, worktree, timeout_s: int,
                fetch_callback=None) -> dict[str, DynamicEvidence]:
    out: dict[str, DynamicEvidence] = {}
    for rule in ruleset.rules:
        if not rule.enabled:
            continue
        box: dict[str, object] = {}

        def _target():
            try:
                box["ok"] = _run_one(
                    rule, files=files, diff_by_file=diff_by_file,
                    worktree=worktree, fetch_callback=fetch_callback,
                )
            except Exception as exc:  # 降级：不阻塞 MR
                box["err"] = exc

        thread = threading.Thread(target=_target, daemon=True)
        thread.start()
        thread.join(rule.params.get("timeout_seconds", timeout_s))
        if thread.is_alive():
            out[rule.key] = DynamicEvidence(rule_key=rule.key, provider=rule.provider, refs=(), degraded=True,
                                            reason="timeout")
            logger.warning("dynamic.timeout rule={} provider={}", rule.key, rule.provider)
        elif "err" in box:
            out[rule.key] = DynamicEvidence(rule_key=rule.key, provider=rule.provider, refs=(), degraded=True,
                                            reason=type(box["err"]).__name__)
        else:
            out[rule.key] = box["ok"]  # type: ignore[assignment]
    return out
```

- [ ] **Step 4: 运行测试**

Run: `.venv/bin/pytest tests/test_dynamic_rules.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add reviewagent/scenario/dynamic.py reviewagent/review/dynamic tests/test_dynamic_rules.py
git commit -m "feat(dynamic): 动态规则模型、加载与超时降级 runner"
```

---

## Task 14: cross_file_refs provider + 三态 golden

**Files:**
- Create: `reviewagent/review/dynamic/providers/__init__.py`, `cross_file_refs.py`
- Modify: `reviewagent/commands/improve.py`（`_find_cross_file_refs` / `_collect_cross_file_refs_for_mr` 委托，保留 3 位置参数）
- Modify: `reviewagent/review/context.py`（动态证据段）
- Test: `tests/test_cross_file_provider.py`, `tests/golden/test_dynamic_golden.py`

**Interfaces:**
- Produces: `CrossFileRefsProvider(fetch_callback)`，`fetch(*, files, diff_by_file, worktree, params) -> list[dict]`。
- 回调用 `self._collect_cross_file_refs_for_mr(files, diff_by_file, wt)`，**只传 3 个位置参数**。
- 单文件回退 `find_refs(file_path, diff, wt)` → `_find_cross_file_refs` 委托。

- [ ] **Step 1: 迁移实现**

把 `improve.py:699-855` 原样迁入 provider；`params` 中的 `rg_file_type` / `excludes` / `max_symbols_per_file` / `max_refs_per_file` / `hits_per_symbol` 作为默认值，门面在自身实现里从当前 `DynamicRuleSet` 读取并作为默认值使用（不额外传给被 patch 的 lambda）。

注册工厂（`providers/__init__.py` 导入即注册；bootstrap 先导入本包，再加载/校验 dynamic rules）：

```python
from reviewagent.review.dynamic.providers.cross_file_refs import CrossFileRefsProvider
from reviewagent.review.dynamic.registry import register

register("cross_file_refs", lambda *, fetch_callback=None, **kw: CrossFileRefsProvider(fetch_callback))
```

- [ ] **Step 2: 渲染接入**

`runner.run` 在 `plan` 后调用 `run_dynamic`，把 evidence 转为 `dict[str, list[dict]]` 传给 assembly；`context.render_cross_file_section(refs, *, profile, evidence: DynamicEvidence | None)` 按四态选择：

| 状态 | 输出 |
|---|---|
| profile 无该动态规则 / `enabled=false` | `""`（整段不存在，探针场景走这里） |
| 规则启用且 `refs` 非空 | `cross_file_refs_refs.md` 渲染 |
| 规则启用且 `refs` 为空 | `cross_file_refs_empty.md` 渲染（现网空结果文案） |
| 规则启用但 provider 超时/异常 | `cross_file_refs_empty.md` + `logger.warning("dynamic.degraded ...")` |

首期默认配置必须有规则且正常执行，保证 golden 与现网一致。

- [ ] **Step 3: 写三态 golden 测试**

```python
# tests/golden/test_dynamic_golden.py
from pathlib import Path

from reviewagent.review.context import render_cross_file_section
from reviewagent.scenario.profile import load_profile


def test_empty_refs_section_byte_equal():
    profile = load_profile(None)
    text = render_cross_file_section([], profile=profile, evidence=None)
    golden = Path("tests/golden/prompts/ssd-python-autotest/chunk_full.txt").read_text()
    assert text in golden


def test_refs_section_byte_equal():
    profile = load_profile(None)
    refs = [{"file": "tests/test_core.py", "line": 12, "ident": "f", "content": "assert f() == 2"}]
    text = render_cross_file_section(refs, profile=profile, evidence=None)
    golden = Path("tests/golden/prompts/ssd-python-autotest/chunk_partial_refs.txt").read_text()
    assert text in golden


def test_degraded_uses_empty_section_plus_reason():
    profile = load_profile(None)
    text = render_cross_file_section([], profile=profile, evidence=None)
    assert "Python 端 rg 没找到" in text
```

- [ ] **Step 4: 运行全量测试**

Run: `.venv/bin/pytest tests/test_cross_file_provider.py tests/golden -q && .venv/bin/pytest -q`
Expected: 全绿。

- [ ] **Step 5: 提交**

```bash
git add reviewagent/review/dynamic/providers reviewagent/review/context.py reviewagent/commands/improve.py tests/test_cross_file_provider.py tests/golden/test_dynamic_golden.py
git commit -m "feat(dynamic): cross_file_refs provider 与三态证据渲染"
```

---

## Task 15: manifest + 启动顺序 + RQ 指纹 + /readyz

**Files:**
- Create: `reviewagent/review/manifest.py`
- Modify: `reviewagent/bootstrap.py`, `reviewagent/workers/tasks.py`, `reviewagent/main.py`, `reviewagent/telemetry/store.py`
- Test: `tests/test_manifest_job.py`, `tests/test_readyz.py`

**Interfaces:**
- Produces: `build_manifest(*, profile, rule_set, cfg, model, provider, request_ruleset=None) -> dict`。
- Produces: `verify_job(payload: dict) -> None`（不一致抛 `RuntimeError`）。
- job payload 新增 `profile` / `profile_fingerprint` / `config_fingerprint`。
- `/readyz` 200 仅当 profile/RuleSet/instance/SQLite/Redis 都通过。

- [ ] **Step 1: 写测试**

```python
# tests/test_manifest_job.py
import pytest

from reviewagent.config import current
from reviewagent.review.manifest import build_manifest, verify_job
from reviewagent.scenario.profile import load_profile
from reviewagent.scenario.rules.resolver import assemble_base


def test_verify_job_rejects_fingerprint_mismatch():
    payload = {"profile": "ssd-python-autotest", "profile_fingerprint": "old", "config_fingerprint": "x"}
    with pytest.raises(RuntimeError, match="fingerprint"):
        verify_job(payload)


def test_manifest_contains_required_fields():
    profile = load_profile(None)
    manifest = build_manifest(
        profile=profile, rule_set=assemble_base(profile), cfg=current(),
        model="m", provider="qoder-sdk",
    )
    assert set(manifest) >= {"service_version", "profile", "profile_fingerprint", "ruleset_fingerprint",
                             "config_fingerprint", "model", "provider"}
```

- [ ] **Step 2: 实现 manifest 与 job 校验**

`build_manifest` 中 `config_fingerprint = settings.binder.fingerprint({s.name: getattr(cfg, s.name) for s in SCHEMA})`；`ruleset_fingerprint` 取基础 RuleSet；`verify_job` 比对当前 `active()` 与 `current()`；worker `_run_command` 入口调用，失败 `logger.error("job.profile_mismatch ...")` 并抛错（job 失败，不重试）。

`bootstrap.startup` 在加载 dynamic rules 前先 `import reviewagent.review.dynamic.providers` 完成工厂注册，再对每条规则调用 `registry.get(rule.provider, fetch_callback=None)` 校验；未知 provider 启动失败（`ValueError`），不在运行期静默降级。

增量状态需要能感知场景/规则变化：`reviewed_file_state` 增加 `profile_fingerprint` / `ruleset_fingerprint` 两列（`_init_schema` 用现有幂等 `ALTER TABLE` 模式补齐），`record_reviewed_files` 写入当前指纹、`load_reviewed_file_state` 一并返回；`ImproveCommand` 把 manifest 的两个指纹传给 `planner.plan`，由 Task 8 的失效规则决定是否复用。旧库缺失列时按 `NULL` 处理（等价于「未知 → 不因指纹拒绝复用」，升级后第一次写入补全）。

- [ ] **Step 3: 接入 enqueue 与 readyz**

`_enqueue` 统一注入三个字段；describe/improve/weekly/管理 job 全走 `_run_command` 校验。`/readyz` 调用 `bootstrap.readiness()`，未 ready 返回 503 + 原因列表。

- [ ] **Step 4: 运行测试**

Run: `.venv/bin/pytest tests/test_manifest_job.py tests/test_readyz.py tests/test_worker_runtime.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add reviewagent/review/manifest.py reviewagent/bootstrap.py reviewagent/workers/tasks.py reviewagent/main.py reviewagent/telemetry/store.py tests/test_manifest_job.py tests/test_readyz.py
git commit -m "feat(runtime): 运行指纹、job 一致性校验与 readiness"
```

---

## Task 16: 安全边界（worktree containment / 不可信内容 / hardening）

**Files:**
- Create: `reviewagent/review/paths.py`, `reviewagent/review/untrusted.py`
- Modify: `reviewagent/review/context.py`, `reviewagent/scenario/rules/repo.py`
- Test: `tests/security/test_worktree_containment.py`, `tests/security/test_untrusted_content.py`

**Interfaces:**
- Produces: `resolve_within(root: Path, rel: str) -> Path`（拒绝绝对路径、`..`、符号链接逃逸）。
- Produces: `normalize_untrusted(text: str) -> str`、`fence_for(text: str) -> str`。
- `ReviewContext` 读取源码、`repo.py` 读取规则文件统一走 `resolve_within`。

- [ ] **Step 1: 写安全测试**

```python
# tests/security/test_worktree_containment.py
import os

import pytest

from reviewagent.review.paths import resolve_within


def test_rejects_absolute_and_parent(tmp_path):
    with pytest.raises(ValueError):
        resolve_within(tmp_path, "/etc/passwd")
    with pytest.raises(ValueError):
        resolve_within(tmp_path, "../outside.py")


def test_rejects_symlink_escape(tmp_path):
    outside = tmp_path.parent / "outside.py"
    outside.write_text("secret")
    (tmp_path / "link.py").symlink_to(outside)
    with pytest.raises(ValueError):
        resolve_within(tmp_path, "link.py")
```

```python
# tests/security/test_untrusted_content.py
from reviewagent.review.untrusted import fence_for, normalize_untrusted


def test_strips_control_chars_but_keeps_newline_tab():
    assert normalize_untrusted("a\x00b\n\tc") == "ab\n\tc"


def test_fence_longer_than_content_backticks():
    assert fence_for("````") == "`````"
```

- [ ] **Step 2: 实现并接入**

`resolve_within` 用 `Path.resolve(strict=True)` 后比较 `root.resolve()` 是否为父目录；失败抛 `ValueError`，调用侧记 `logger.warning("security.path_rejected ...")`。`normalize_untrusted` 仅在含控制字符时重建字符串，正常文本原样返回（保证 prompt 字节不变）。`context` 的所有读取先过 containment；`security.normalize_untrusted=false` 时跳过。`prompt_hardening=true` 时在 system prompt 追加硬化段（首期 false）。

- [ ] **Step 3: 运行测试**

Run: `.venv/bin/pytest tests/security -q && .venv/bin/pytest tests/golden -q`
Expected: PASS，golden 不变。

- [ ] **Step 4: 提交**

```bash
git add reviewagent/review/paths.py reviewagent/review/untrusted.py reviewagent/review/context.py reviewagent/scenario/rules/repo.py tests/security
git commit -m "feat(security): worktree 路径约束与不可信内容规范化"
```

---

## Task 17: 日志治理

**Files:**
- Modify: `reviewagent/logging_setup.py`
- Modify: `reviewagent/config.py` / `reviewagent/settings/schema.py`（新增 `log_format`、`log_rotation`）
- Test: `tests/test_logging_governance.py`

**Interfaces:**
- Produces: `setup_logging(*, fmt: str | None = None)`；`LOG_FORMAT=text|json`；`diagnose=False`。
- Produces: `bind_context(**kwargs)` / `clear_context()`；事件名 `<domain>.<verb>`。
- 脱敏：`gitlab_pat` / `qoder_sdk_pat` / `opencode_password` / `gitlab_webhook_secret` 值替换为 `***`。

- [ ] **Step 1: 写测试**

```python
# tests/test_logging_governance.py
import json

from reviewagent.logging_setup import JsonFormatter, redact


def test_json_formatter_emits_stable_fields():
    record = {"time": "2026-11-07T00:00:00Z", "level": {"name": "INFO"}, "message": "x",
              "extra": {"run_id": "r1"}}
    line = JsonFormatter().format(record)
    data = json.loads(line)
    assert data["level"] == "INFO"
    assert data["run_id"] == "r1"


def test_redact_secrets():
    assert redact("pat=abc123", secrets={"abc123"}) == "pat=***"
```

- [ ] **Step 2: 实现**

`JsonFormatter` 输出 `time/level/message/event/run_id/mr/project` 等稳定字段；`diagnose=False`；`LOG_ROTATION=single|none`，多进程部署默认 `none`（交给 journald/logrotate），避免双重轮转。`setup_logging` 保持幂等，保留现有 sink 行为（默认 text 与当前格式接近，测试不依赖颜色）。

新增两个实例字段（同时补 `Config` 字段与 `SCHEMA`）：

```python
FieldSpec("log_format", str, "text", env="LOG_FORMAT", scope="instance", choices=("text", "json")),
FieldSpec("log_rotation", str, "single", env="LOG_ROTATION", scope="instance", choices=("single", "none")),
```

- [ ] **Step 3: 运行测试**

Run: `.venv/bin/pytest tests/test_logging_governance.py -q`
Expected: PASS。

- [ ] **Step 4: 提交**

```bash
git add reviewagent/logging_setup.py reviewagent/config.py reviewagent/settings/schema.py tests/test_logging_governance.py
git commit -m "feat(logging): text/json 双格式、上下文绑定与脱敏"
```

---

## Task 18: 运行语义回归测试（不加新行为）

**Files:**
- Create: `tests/runtime/test_repeat_and_concurrency.py`, `test_incremental_invalidation.py`, `test_publish_crash_window.py`
- Test: 无生产代码改动（发现缺陷才修，且必须单独提交）

- [ ] **Step 1: 写重复 webhook / 并发 push**

用 `fakeredis` + 现有 `get_redis` patch，断言：同 MR 重复 webhook 只入队一次；并发 push 第二个等锁；锁超时后释放；Redis 故障时 cooldown fail-open、链锁 fail-closed。

- [ ] **Step 2: 写增量与指纹失效**

构造 `reviewed_file_state`，断言：blob 未变 → 复用；`profile_fingerprint` / `ruleset_fingerprint` 变化 → 清空并记录 `review.state_invalidated`；跨文件影响 → 强制重检。

- [ ] **Step 3: 写发布崩溃窗口与 cohort**

模拟 GitLab 发帖成功、telemetry 落库失败 → 下一轮按指纹去重；cohort 代际 supersede 顺序不变。

- [ ] **Step 4: 场景隔离**

用两个临时 `data_dir` 分别绑定不同 profile：第二次 `bind_instance` 抛 `RuntimeError`；周报 store 与 `reviewed_file_state` 只读当前 `data_dir`，断言跨 profile 不出现 cohort/增量复用；在途 job 的 `profile_fingerprint` 不匹配时 `verify_job` 拒绝。

- [ ] **Step 5: 运行**

Run: `.venv/bin/pytest tests/runtime -q`
Expected: PASS；若失败，说明重构破坏了运行语义，必须修生产代码而不是放宽断言。

- [ ] **Step 6: 提交**

```bash
git add tests/runtime
git commit -m "test(runtime): 并发/幂等/增量/崩溃窗口与场景隔离语义回归"
```

---

## Task 19: 架构守卫 + 探针场景 + 回放门禁

**Files:**
- Create: `tests/architecture/test_boundaries.py`, `test_no_language_literals.py`, `test_rule_namespace.py`
- Create: `tests/fixtures/profiles/probe-scenario/{profile.yaml,prompts/*,rules/*}`
- Create: `tests/test_probe_scenario.py`
- Test: `tests/replay/`（标注集 manifest + replay 脚本）

**Interfaces:**
- 守卫读取 `reviewagent/review/**`、`reviewagent/scenario/**` 的 AST；负向 fixture 放 `tests/fixtures/architecture_violations/`。

- [ ] **Step 1: 写四条边界守卫**

```python
# tests/architecture/test_boundaries.py
import ast
from pathlib import Path

import pytest

SHARED = Path("reviewagent/review")


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return out


def test_shared_review_does_not_import_policies():
    for path in SHARED.rglob("*.py"):
        if "policies" in path.parts:
            continue
        assert not any(m.startswith("reviewagent.review.policies") for m in _imports(path)), path


def test_scenario_does_not_import_review():
    for path in Path("reviewagent/scenario").rglob("*.py"):
        assert not any(m.startswith("reviewagent.review") for m in _imports(path)), path


def test_reporting_does_not_import_review():
    for path in Path("reviewagent/reporting").rglob("*.py"):
        assert not any(m.startswith("reviewagent.review") for m in _imports(path)), path


def test_config_boundary():
    # 兼容期唯一例外：reviewagent/config.py 仍是现网 env 读取入口（60+ 模块导入它的 config）。
    # 新代码只走 settings/**；config.py 的未迁移字段随 Task 20 逐步收敛后删除此例外。
    legacy_env_reader = Path("reviewagent/config.py")
    for path in Path("reviewagent").rglob("*.py"):
        if "settings" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        assert "os.environ" not in text or path == legacy_env_reader, path
```

例外只按**完整相对路径**放行，禁止 `path.name == "config.py"` 这种任意同名文件绕过。`settings/**` 之外新增 `os.environ`（含 `import os; os.getenv`、`__import__("os")`）一律失败。

`test_no_language_literals` 扫描 `review/**`（排除 `policies/**`）的 import 与可执行字符串中的 `.py`、`-t py`、`import ast`、`if language ==`；`test_rule_namespace` 断言 python profile 装配含 `CPP-R-*` 的包会失败。

负向 fixture：`tests/fixtures/architecture_violations/{bad_import.py,language_literal.py,cpp_pack.md}`，守卫函数接受 `root` 参数，测试用 fixture 断言返回违规列表非空。

- [ ] **Step 2: 探针场景端到端**

`tests/fixtures/profiles/probe-scenario/profile.yaml` 声明 `language: probe`、不同扩展名 `.probe`、规则键 `PROBE-R-*`、`render: structured`；测试用 `registry.register("probe", ProbePolicy)` 注入最小 policy，跑 fake LLM 全流程，断言产生 suggestion 且共享代码零改动。

- [ ] **Step 3: 回放门禁**

`tests/replay/manifest.json` 记录 3 个历史 MR 的 `project_id/mr_iid/head_sha` 与 fake LLM 响应文件；`tests/replay/test_replay_baseline.py` 用 fake provider 回放，断言建议数偏差 ≤10%、severity 分布偏差 ≤2pp、high 零漏检。首期若无真实数据，使用 Task 0 golden 的 fake 响应作为基线，manifest 注明 `source=golden`。

- [ ] **Step 4: 运行**

Run: `.venv/bin/pytest tests/architecture tests/test_probe_scenario.py tests/replay -q`
Expected: PASS；负向 fixture 必须让守卫失败（测试内断言违规集合非空）。

- [ ] **Step 5: 性能烟测（只防数量级回归）**

`scripts/bench_review_assembly.py` 用回放样本测量 `assemble_base` + `build_chunk_prompt` 的耗时与峰值 RSS，写入 `tests/replay/perf_baseline.json`；测试断言单次装配 < 2s、峰值 < 200MB（宽松上限）。发布前人工对比基线，超过 1.5× 需说明原因。

- [ ] **Step 6: 提交**

```bash
git add tests/architecture tests/fixtures/profiles/probe-scenario tests/fixtures/architecture_violations tests/test_probe_scenario.py tests/replay scripts/bench_review_assembly.py
git commit -m "test(arch): 四条硬边界守卫、探针场景、回放与性能门禁"
```

---

## Task 20: 部署、数据与文档收尾

**Files:**
- Modify: `.env.example`, `README.md`, `docs/DEPLOYMENT.md`, `docs/QUICKSTART.md`
- Create: `docs/CONFIG.md`（由 `config show` 生成）、`docs/RULES.md`
- Modify: `deploy/*.service`（`Environment=REVIEW_PROFILE=...`、`ExecStartPre=reviewagent config check`）
- Create: `docs/BACKUP_RESTORE.md`, `CODEOWNERS`, `CHANGELOG.md`
- Test: `tests/test_config_docs.py`, `tests/test_rules_docs.py`, `tests/runtime/test_ops_drill.py`

- [ ] **Step 1: 收敛 `.env.example` 到实例平面约 15 项**

只保留：`GITLAB_URL`、`GITLAB_PERSONAL_ACCESS_TOKEN`、`GITLAB_WEBHOOK_SECRET`、`GITLAB_BOT_USERNAME`、`LLM_PROVIDER`、`QODER_SDK_PAT`、`QODER_SDK_MODEL`、`REDIS_URL`、`RQ_WORKER_COUNT`、`RQ_WORKER_TIMEOUT`、`REVIEW_PROFILE`、`REVIEWAGENT_DATA_DIR`、`REVIEWAGENT_LOG_LEVEL`、`LOG_FORMAT`、周报投递 webhook/secret。其余字段注释指向 `docs/CONFIG.md`。

- [ ] **Step 2: 生成 CONFIG.md**

```bash
.venv/bin/python -m reviewagent config show --diff-default --sources > docs/CONFIG.md
```

`tests/test_config_docs.py` 断言 `.env.example` 中每个键都在 `SCHEMA` 的 `env` 集合内，且 `docs/CONFIG.md` 含 `profile_fingerprint` 与 `config_fingerprint` 说明。

- [ ] **Step 3: 更新部署与运维**

systemd 增加 `Environment=REVIEW_PROFILE=ssd-python-autotest`、`Environment=REVIEWAGENT_DATA_DIR=/var/lib/reviewagent`、`ExecStartPre=/opt/reviewagent/.venv/bin/python -m reviewagent config check`；`docs/DEPLOYMENT.md` 写明「切换场景 = 新 data_dir + 同时重启 API 与 worker」；`docs/BACKUP_RESTORE.md` 给出 `VACUUM INTO` 备份、schema 版本门禁、恢复校验与 drain 步骤。

- [ ] **Step 3.5: 备份/恢复演练测试（只测现有机制，不新增运行时行为）**

```python
# tests/runtime/test_ops_drill.py
import sqlite3

from reviewagent.telemetry.models import ReviewRun
from reviewagent.telemetry.store import Store


def test_sqlite_online_backup_restore_roundtrip(tmp_path):
    src = tmp_path / "data" / "telemetry.db"
    store = Store(src)
    store.insert_run(ReviewRun(project_id=34, mr_iid=1, command="improve", triggered_by="webhook"))
    backup = tmp_path / "backup.db"
    with sqlite3.connect(src) as conn:
        conn.execute("VACUUM INTO ?", (str(backup),))
    restored = tmp_path / "restored" / "telemetry.db"
    restored.parent.mkdir()
    restored.write_bytes(backup.read_bytes())
    Store(restored)  # 迁移幂等：重复打开不报错
    with sqlite3.connect(restored) as conn:
        assert conn.execute("SELECT COUNT(*) FROM review_runs").fetchone()[0] == 1
```

`tests/runtime/test_ops_drill.py` 另含一条孤儿 run 清扫演练：插入 `status='running'` 且 `started_at` 早于 TTL 的记录，调用现有 `sweep_orphaned_runs`，断言被标终态（复用 `tests/test_telemetry_store.py` 的 fixture 写法）。

首期**明确不做**（避免在默认路径外新增运行时行为，与「本次不改变检视效果」冲突；记入 `CHANGELOG.md` 的 Known limits 与后续计划）：磁盘水位拒绝（`E_STORAGE`）、API drain 端点、secret 轮换后自动重投。现有 SIGTERM 处理、worktree prune、孤儿 run 清扫保持不变，只加演练测试与 runbook 说明。

- [ ] **Step 4: `docs/RULES.md` 最小规则包模板**

给出 `verbatim` 与 `structured` 两个示例、正反例围栏写法、`reviewagent rules check` 用法、命名空间与共享包语言声明要求。示例整包用**四个反引号**围栏（内部正反例仍用三个），否则内层围栏会提前截断文档解析。

示例必须被 CI 实际解析，不能只做人读文档：

```python
# tests/test_rules_docs.py
import re
from pathlib import Path

from reviewagent.scenario.rules.parser import parse_pack

FENCE4 = "`" * 4


def test_rules_md_examples_parse(tmp_path):
    text = Path("docs/RULES.md").read_text(encoding="utf-8")
    blocks = re.findall(FENCE4 + r"markdown\n(.*?)\n" + FENCE4, text, re.S)
    assert len(blocks) >= 2  # verbatim + structured 各一个
    for i, block in enumerate(blocks):
        path = tmp_path / f"example_{i}.md"
        path.write_text(block, encoding="utf-8")
        assert parse_pack(path, language="python", root=tmp_path).rules  # 解析通过且至少一条规则
```

- [ ] **Step 5: 全量验收**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check reviewagent tests && .venv/bin/python -m reviewagent config check && .venv/bin/python -m reviewagent rules check`
Expected: 全部通过。

- [ ] **Step 6: 提交**

```bash
git add .env.example README.md docs deploy CODEOWNERS CHANGELOG.md tests/test_config_docs.py tests/test_rules_docs.py tests/runtime/test_ops_drill.py
git commit -m "docs(deploy): 实例/场景/规则三平面文档与运维 runbook"
```

---

## 执行顺序与检查点

- Task 0 必须先于所有重构提交；Task 9/10 是兼容性关键检查点（golden + 全量测试）。
- Task 4–6（规则层）与 Task 7–8（policy/planner）可并行评审，但合并顺序必须按编号，避免门面引用未定义模块。
- Task 13–14（动态规则）依赖 Task 10 的 runner；Task 15 依赖 Task 5 的 RuleSet 与 Task 3 的 bootstrap。
- Task 16–18 只做加固与测试；若发现行为差异，回到对应模块修复，禁止放宽 golden 或既有断言。
- 每个任务提交前运行 `git status`，确认未把工作区既有未跟踪产物加入暂存区。
