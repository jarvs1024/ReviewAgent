"""语言字面量守卫（spec §7.5 第 2 条）.

扫描范围：``reviewagent/review/**``（排除 ``review/policies/**``——它是语言差异
的唯一出口）PLUS ``reviewagent/scenario/**``。``profiles/**`` 是场景数据
（prompt 文案里出现语言名是合法的），不扫描。

命中的三类可执行信号：

1. import 节点（含相对 import）文本里出现 ``.py`` / ``-t py`` /
   ``import ast``，或导入标准库 ``ast``（Python 专属语法树）；
2. 可执行字符串常量（含 f-string 的常量片段）包含上述字面量；
3. ``ast.Compare`` 左侧为名字 ``language`` 且比较对象是字符串常量
   （即 ``language == "python"`` 之类的语言分支）。

注释与模块 / 类 / 函数 docstring 不参与扫描——它们是文档，不是分支。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
REVIEW_DIR = REPO_ROOT / "reviewagent" / "review"
SCENARIO_DIR = REPO_ROOT / "reviewagent" / "scenario"
REVIEW_POLICIES_DIR = REVIEW_DIR / "policies"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "architecture_violations"

#: 被禁止的「语言字面量」形态（逐字对应 spec §7.5 第 2 条）。
#: ``\b`` / ``\s`` 边界避免把 ``.python`` 误判成 ``.py`` 后缀。
LANGUAGE_LITERALS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (".py", re.compile(r"\.py\b")),
    ("-t py", re.compile(r"-t\s+py\b")),
    ("import ast", re.compile(r"\bimport\s+ast\b")),
    ("if language ==", re.compile(r"\bif\s+language\s*==")),
)

_AST_MODULE = "ast"


def _python_files(root: Path) -> list[Path]:
    root = Path(root)
    if root.is_file():
        return [root]
    if not root.is_dir():
        return []
    return sorted(path for path in root.rglob("*.py") if path.is_file())


def _rel_posix(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return resolved.name


def _is_under(path: Path, directory: Path) -> bool:
    try:
        path.resolve().relative_to(directory.resolve())
    except ValueError:
        return False
    return True


def _docstring_nodes(tree: ast.Module) -> set[int]:
    """模块 / 类 / 函数首条 docstring 的常量节点 id（扫描时跳过）."""
    ids: set[int] = set()
    owners = (
        ast.Module,
        ast.ClassDef,
        ast.FunctionDef,
        ast.AsyncFunctionDef,
    )
    for node in ast.walk(tree):
        if not isinstance(node, owners) or not node.body:
            continue
        first = node.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            ids.add(id(first.value))
    return ids


def _matched_literal(text: str) -> str | None:
    """返回命中的字面量标签；未命中返回 None."""
    for label, pattern in LANGUAGE_LITERALS:
        if pattern.search(text):
            return label
    return None


def _import_text(node: ast.Import | ast.ImportFrom) -> str:
    if isinstance(node, ast.Import):
        return "import " + ", ".join(alias.name for alias in node.names)
    module = "." * (node.level or 0) + (node.module or "")
    names = ", ".join(alias.name for alias in node.names)
    return f"from {module} import {names}"


def _is_stdlib_ast_import(node: ast.Import | ast.ImportFrom) -> bool:
    if isinstance(node, ast.Import):
        return any(
            alias.name.split(".", 1)[0] == _AST_MODULE for alias in node.names
        )
    if node.level:
        return False
    return (node.module or "").split(".", 1)[0] == _AST_MODULE


def language_literal_violations(root: Path) -> list[str]:
    """返回 ``root`` 下所有语言字面量违规（``path: kind: 片段``）."""
    violations: list[str] = []
    for path in _python_files(root):
        if _is_under(path, REVIEW_POLICIES_DIR):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        docstrings = _docstring_nodes(tree)
        rel = _rel_posix(path)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                text = _import_text(node)
                label = _matched_literal(text)
                if _is_stdlib_ast_import(node):
                    label = "import ast"
                if label:
                    violations.append(f"{rel}: import ({label}): {text}")
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) in docstrings:
                    continue
                label = _matched_literal(node.value)
                if label:
                    violations.append(
                        f"{rel}: string ({label}): {node.value[:80]!r}"
                    )
            elif isinstance(node, ast.JoinedStr):
                for part in node.values:
                    if isinstance(part, ast.Constant) and isinstance(part.value, str):
                        label = _matched_literal(part.value)
                        if label:
                            violations.append(
                                f"{rel}: f-string ({label}): {part.value[:80]!r}"
                            )
            elif isinstance(node, ast.Compare):
                if not (isinstance(node.left, ast.Name) and node.left.id == "language"):
                    continue
                for comparator in node.comparators:
                    if isinstance(comparator, ast.Constant) and isinstance(
                        comparator.value, str
                    ):
                        violations.append(
                            f"{rel}: compare: language == {comparator.value!r}"
                        )
    return violations


# ---------------------------------------------------------------------------
# 正向
# ---------------------------------------------------------------------------


def test_review_shared_has_no_language_literals():
    assert language_literal_violations(REVIEW_DIR) == []


def test_scenario_has_no_language_literals():
    assert language_literal_violations(SCENARIO_DIR) == []


def test_policies_are_the_language_outlet():
    """policies 目录确实含语言字面量，而共享 review 扫描把它们排除在外."""
    literal_files = [
        path
        for path in sorted(REVIEW_POLICIES_DIR.rglob("*.py"))
        if "import ast" in path.read_text(encoding="utf-8")
    ]
    assert literal_files, "expected at least one policy file to contain an 'import ast' literal"
    assert language_literal_violations(REVIEW_DIR) == []


# ---------------------------------------------------------------------------
# 负向
# ---------------------------------------------------------------------------


def test_language_literal_fixture_fails_guard():
    violations = language_literal_violations(FIXTURES / "language_literal.py")
    assert violations
    joined = "\n".join(violations)
    assert "-t py" in joined
    assert ".py" in joined
    assert "language ==" in joined


def test_docstrings_and_comments_are_ignored(tmp_path):
    sample = tmp_path / "sample.py"
    sample.write_text(
        '"""Docstring mentions import ast and endswith(".py")."""\n'
        "\n"
        "# 注释: -t py 与 if language == 'python'\n"
        "\n"
        "\n"
        "def f(language):\n"
        '    """Docstring 里 import ast 也要忽略."""\n'
        "    return language\n",
        encoding="utf-8",
    )
    assert language_literal_violations(sample) == []


def test_executable_literal_inside_function_is_flagged(tmp_path):
    sample = tmp_path / "sample.py"
    sample.write_text(
        "def suffix(path):\n"
        '    return path.endswith(".py")\n',
        encoding="utf-8",
    )
    violations = language_literal_violations(sample)
    assert violations
    assert ".py" in violations[0]
