"""四条硬边界守卫（spec §7.5）：import 方向 + 配置边界.

守卫全部基于 AST（不匹配注释 / docstring 文本），并接受 ``root`` 参数，
负向 fixture（``tests/fixtures/architecture_violations/``）直接传文件即可
断言「守卫确实会失败」，避免守卫长期空转。

import 方向（§7.5 第 1 条）：

* ``review`` 共享模块（排除 ``review/policies/**``）不得 import
  ``reviewagent.review.policies.*`` / ``reviewagent.commands.*``；
* ``scenario`` 不得 import ``reviewagent.review.*``；
* ``reporting`` 不得 import ``reviewagent.review.*``；
* ``settings`` 不得 import ``reviewagent.review`` / ``reviewagent.reporting``；
* ``reviewagent/config.py`` 不得 import ``reviewagent.scenario``。

配置边界（§7.5 第 4 条）：``settings/**`` 之外直接读环境变量一律失败。
兼容期例外按**完整相对路径**放行（禁止 ``path.name == "config.py"`` 这类
同名绕过），且只剩 3 条：``reviewagent/config.py``（现网 env 入口）、
``reviewagent/llm/qoder_sdk_provider.py``（代理透传）、
``reviewagent/workers/rq_worker.py``（RQ 子进程环境传播）。
周报配置（原第 4 条例外）已在 T20 并入 ``settings/schema.py``。
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
REVIEW_DIR = REPO_ROOT / "reviewagent" / "review"
SCENARIO_DIR = REPO_ROOT / "reviewagent" / "scenario"
REPORTING_DIR = REPO_ROOT / "reviewagent" / "reporting"
SETTINGS_DIR = REPO_ROOT / "reviewagent" / "settings"
CONFIG_MODULE = REPO_ROOT / "reviewagent" / "config.py"
REVIEWAGENT_DIR = REPO_ROOT / "reviewagent"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "architecture_violations"

#: 允许直接读环境变量的文件（完整相对路径，逐条显式声明）。
ENV_READER_ALLOWLIST: frozenset[str] = frozenset(
    {
        "reviewagent/config.py",
        "reviewagent/llm/qoder_sdk_provider.py",
        "reviewagent/workers/rq_worker.py",
    }
)

_OS_ENV_MODULE = "os"


# ---------------------------------------------------------------------------
# 通用：文件枚举 / import 解析 / 相对路径
# ---------------------------------------------------------------------------


def _python_files(root: Path) -> list[Path]:
    """``root`` 是文件时只扫它；是目录时递归扫 ``*.py``（负向 fixture 复用）."""
    root = Path(root)
    if root.is_file():
        return [root]
    if not root.is_dir():
        return []
    return sorted(path for path in root.rglob("*.py") if path.is_file())


def _rel_posix(path: Path) -> str:
    """仓库内相对 POSIX 路径；仓库外（fixture 复制件）退回文件名."""
    resolved = path.resolve()
    try:
        return resolved.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return resolved.name


def _package_parts(path: Path) -> tuple[str, ...] | None:
    """按仓库根解析 ``path`` 所在包（用于相对 import 求值）."""
    try:
        rel = path.resolve().relative_to(REPO_ROOT)
    except ValueError:
        return None
    parts = rel.with_suffix("").parts
    return parts[:-1]


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _import_modules(path: Path) -> set[str]:
    """收集文件里所有 import 的目标模块（相对 import 解析成绝对形式）."""
    package = _package_parts(path)
    modules: set[str] = set()
    for node in ast.walk(_parse(path)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            level = node.level or 0
            module = node.module or ""
            if level and package is not None:
                base = package[: len(package) - (level - 1)] if level > 1 else package
                modules.add(".".join((*base, module)) if module else ".".join(base))
            elif module:
                modules.add(module)
    return modules


def _is_within(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(prefix + ".")


def _import_violations(
    root: Path,
    *,
    forbidden: tuple[str, ...],
    exclude_parts: tuple[str, ...] = (),
) -> list[str]:
    violations: list[str] = []
    for path in _python_files(root):
        if any(part in path.parts for part in exclude_parts):
            continue
        hits = sorted(
            module
            for module in _import_modules(path)
            if any(_is_within(module, prefix) for prefix in forbidden)
        )
        violations.extend(f"{_rel_posix(path)} imports {module}" for module in hits)
    return violations


# ---------------------------------------------------------------------------
# import 方向守卫
# ---------------------------------------------------------------------------


def shared_review_violations(root: Path) -> list[str]:
    """``review`` 共享模块不得 import 策略实现或命令层（排除 policies/）."""
    return _import_violations(
        root,
        forbidden=("reviewagent.review.policies", "reviewagent.commands"),
        exclude_parts=("policies",),
    )


def scenario_import_violations(root: Path) -> list[str]:
    """``scenario`` 数据层不得 import ``reviewagent.review``."""
    return _import_violations(root, forbidden=("reviewagent.review",))


def reporting_import_violations(root: Path) -> list[str]:
    """``reporting`` 不得 import ``reviewagent.review``."""
    return _import_violations(root, forbidden=("reviewagent.review",))


def settings_import_violations(root: Path) -> list[str]:
    """``settings`` 不得 import ``reviewagent.review`` / ``reviewagent.reporting``."""
    return _import_violations(
        root, forbidden=("reviewagent.review", "reviewagent.reporting")
    )


def config_module_import_violations(root: Path) -> list[str]:
    """``reviewagent/config.py`` 不得 import ``reviewagent.scenario``."""
    return _import_violations(root, forbidden=("reviewagent.scenario",))


# ---------------------------------------------------------------------------
# 配置边界守卫
# ---------------------------------------------------------------------------


def _env_accesses(path: Path) -> list[str]:
    """AST 里出现的环境变量直接读取（文档字符串 / 注释天然不在 AST 中）."""
    hits: list[str] = []
    for node in ast.walk(_parse(path)):
        if isinstance(node, ast.Attribute) and node.attr == "environ":
            if isinstance(node.value, ast.Name) and node.value.id == _OS_ENV_MODULE:
                hits.append("os.environ")
        elif isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "getenv"
                and isinstance(func.value, ast.Name)
                and func.value.id == _OS_ENV_MODULE
            ):
                hits.append("os.getenv")
            elif (
                isinstance(func, ast.Name)
                and func.id == "__import__"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == _OS_ENV_MODULE
            ):
                hits.append("__import__('os')")
        elif isinstance(node, ast.ImportFrom) and node.module == _OS_ENV_MODULE:
            for alias in node.names:
                if alias.name in {"environ", "getenv"}:
                    hits.append(f"from os import {alias.name}")
    return hits


def env_reader_violations(root: Path) -> list[str]:
    """``settings/**`` + 显式 allowlist 之外禁止直接读环境变量."""
    violations: list[str] = []
    for path in _python_files(root):
        if "settings" in path.parts:
            continue
        rel = _rel_posix(path)
        if rel in ENV_READER_ALLOWLIST:
            continue
        violations.extend(f"{rel}: {hit}" for hit in _env_accesses(path))
    return violations


# ---------------------------------------------------------------------------
# 正向：仓库当前状态必须干净
# ---------------------------------------------------------------------------


def test_env_reader_allowlist_is_exact():
    """例外只按完整相对路径放行，新增同名 config.py 不得绕过."""
    assert ENV_READER_ALLOWLIST == frozenset(
        {
            "reviewagent/config.py",
            "reviewagent/llm/qoder_sdk_provider.py",
            "reviewagent/workers/rq_worker.py",
        }
    )
    for rel in ENV_READER_ALLOWLIST:
        assert (REPO_ROOT / rel).is_file(), rel


def test_shared_review_does_not_import_policies_or_commands():
    assert shared_review_violations(REVIEW_DIR) == []


def test_scenario_does_not_import_review():
    assert scenario_import_violations(SCENARIO_DIR) == []


def test_reporting_does_not_import_review():
    assert reporting_import_violations(REPORTING_DIR) == []


def test_settings_does_not_import_review_or_reporting():
    assert settings_import_violations(SETTINGS_DIR) == []


def test_config_module_does_not_import_scenario():
    assert config_module_import_violations(CONFIG_MODULE) == []


def test_env_readers_outside_settings_are_allowlisted():
    assert env_reader_violations(REVIEWAGENT_DIR) == []


# ---------------------------------------------------------------------------
# 负向：fixture 必须让守卫失败
# ---------------------------------------------------------------------------


def test_bad_import_fixture_fails_shared_review_guard():
    violations = shared_review_violations(FIXTURES / "bad_import.py")
    assert violations
    assert any("reviewagent.review.policies" in item for item in violations)
    assert any("reviewagent.commands" in item for item in violations)


def test_bad_import_fixture_fails_scenario_and_reporting_guards():
    assert scenario_import_violations(FIXTURES / "bad_import.py")
    assert reporting_import_violations(FIXTURES / "bad_import.py")


def test_bad_import_fixture_fails_settings_and_config_guards():
    assert settings_import_violations(FIXTURES / "bad_import.py")
    assert config_module_import_violations(FIXTURES / "bad_import.py")


def test_bad_env_fixture_fails_config_boundary_guard():
    violations = env_reader_violations(FIXTURES / "bad_env.py")
    assert violations
    assert any("os.environ" in item for item in violations)
    assert any("os.getenv" in item for item in violations)


def test_env_guard_rejects_arbitrary_same_named_config(tmp_path):
    """同名 ``config.py`` 不得因文件名相同被放行（只认完整相对路径）."""
    fake = tmp_path / "config.py"
    fake.write_text(
        "import os\n\nVALUE = os.environ.get('X')\n", encoding="utf-8"
    )
    assert env_reader_violations(fake)
