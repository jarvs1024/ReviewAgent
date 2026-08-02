"""测试 _detect_apply_risk: 静态分析 improved_code 引用未定义符号.

不依赖 GitLab / network, 直接 new ImproveCommand + 调用.
"""
from __future__ import annotations

from reviewagent.commands.improve import ImproveCommand


def _cmd() -> ImproveCommand:
    """构造 ImproveCommand 但跳过 __init__ 的 GitLab 调用."""
    obj = ImproveCommand.__new__(ImproveCommand)
    obj.project_id = 34
    obj.mr_iid = 168
    return obj


def _risk(improved, file_src=None, file_path="svc.py"):
    if file_src is None:
        file_src = ["def run():\n", "    pass\n"]
    return _cmd()._detect_apply_risk(
        file_path=file_path,
        improved_code=improved,
        file_sources={file_path: file_src},
    )


# ---- 基本: ok 分支 ----

def test_ok_when_all_symbols_defined():
    """所有符号都定义了 → ok."""
    file_src = ["import os\n", "\n", "RETRY_PORT = 8080\n", "\n", "def run():\n", "    pass\n"]
    improved = "import os\nRETRY_PORT = 8080\n"
    level, msgs = _risk(improved, file_src)
    assert level == "ok", f"expected ok, got {level}: {msgs}"
    assert msgs == []


def test_ok_when_symbol_defined_in_improved():
    """improved_code 自己定义 + 使用 → ok (避免自引用 false positive)."""
    improved = "MAX = 10\nvalue = MAX\n"
    level, msgs = _risk(improved)
    assert level == "ok", f"expected ok (MAX defined in improved), got {level}: {msgs}"


def test_skip_builtins():
    """builtins / 关键字 → ok, 不报."""
    improved = "def run():\n    return True if None else False\n"
    level, msgs = _risk(improved)
    assert level == "ok", f"expected ok (only builtins), got {level}: {msgs}"


def test_skip_unimported_module_name():
    """已 import 的名字应当 ok."""
    file_src = ["import os\n", "def run():\n", "    pass\n"]
    improved = "import os\nport = os.getenv('X', 0)\n"
    level, msgs = _risk(improved, file_src)
    assert level == "ok", f"os imported should be ok, got {level}: {msgs}"


def test_missing_file_returns_ok():
    """file_path 不在 file_sources 中 → ok (no false positive, 没数据不警告)."""
    improved = "x = SOMETHING\n"
    level, msgs = _cmd()._detect_apply_risk(
        file_path="svc.py", improved_code=improved, file_sources={},
    )
    assert level == "ok", f"no file_sources → ok, got {level}: {msgs}"


def test_argument_name_marks_defined():
    """函数参数 / lambda 参数 算已定义, 不报."""
    file_src = ["def run(arg):\n", "    return arg\n"]
    improved = "def run(arg):\n    return arg\n"
    level, msgs = _risk(improved, file_src)
    assert level == "ok", f"arg defined, got {level}: {msgs}"


def test_self_cls_excluded():
    """self / cls 不算 missing."""
    file_src = ["class Svc:\n", "    def __init__(self):\n", "        self.x = 1\n"]
    improved = "class Svc:\n    def method(self):\n        return self.x\n"
    level, msgs = _risk(improved, file_src)
    assert level == "ok", f"self.x should be ok, got {level}: {msgs}"


def test_kwonly_args_recognized():
    """kwonly args (e.g. *, timeout) 也算已定义."""
    file_src = ["def run(timeout=10, *, retries):\n", "    pass\n"]
    improved = "def run(timeout=10, *, retries):\n    if retries > 5:\n        print(timeout)\n"
    level, msgs = _risk(improved, file_src)
    assert level == "ok", f"kwonly args should be ok, got {level}: {msgs}"


# ---- P0: for / with / AnnAssign / NamedExpr 不再误报 ----

def test_for_target_not_missing():
    """for 循环变量 (item) 不应报 missing."""
    improved = "for item in items:\n    process(item)\n"
    level, msgs = _risk(improved)
    # item 不应出现; items / process 是真正的外部依赖, 可报
    # item 不应作为独立 missing 符号; items/process 是真正外部依赖可报
    missing_symbols = msgs[0] if msgs else ""
    # 用括号内的符号列表精确匹配, 不用子串
    assert not any(s.strip() == "item" for s in missing_symbols.split("(", 1)[-1].split(")", 1)[0].split(",")), f"for-target .item. should not be missing: {msgs}"


def test_with_as_not_missing():
    """with ... as fh: fh 不应报 missing."""
    improved = 'with open("f") as fh:\n    data = fh.read()\n'
    level, msgs = _risk(improved)
    assert "fh" not in (", ".join(msgs)), f"with-as 'fh' should not be missing: {msgs}"


def test_annassign_not_missing():
    """x: int = 5 中的 x 不应报 missing."""
    improved = "x: int = 5\nreturn x\n"
    level, msgs = _risk(improved)
    assert "x" not in (", ".join(msgs)), f"AnnAssign 'x' should not be missing: {msgs}"


def test_namedexpr_not_missing():
    """walrus (y := 10) 中的 y 不应报 missing."""
    improved = "if (y := 10) > 5:\n    print(y)\n"
    level, msgs = _risk(improved)
    assert "y" not in (", ".join(msgs)), f"NamedExpr 'y' should not be missing: {msgs}"


def test_comprehension_target_not_missing():
    """列表推导式变量不应报 missing."""
    improved = "result = [x for x in range(10)]\n"
    level, msgs = _risk(improved)
    assert "x" not in (", ".join(msgs)), f"comprehension 'x' should not be missing: {msgs}"


# ---- P1: 去掉 likely_module 启发式, 统一 missing ----

def test_truly_missing_has_actionable_hint():
    """未定义常量 → warn + NameError 提示."""
    improved = "port = RETRY_PORT\n"
    level, msgs = _risk(improved)
    assert level == "warn"
    assert any("RETRY_PORT" in m and "NameError" in m for m in msgs)
    assert any("add RETRY_PORT" in m or "import RETRY_PORT" in m for m in msgs)


def test_common_lowercase_var_also_warned():
    """data/config/result 等常见小写变量名也应标 missing (不再被误分类为 likely_module)."""
    improved = "result = data + config\n"
    level, msgs = _risk(improved)
    assert level == "warn"
    assert any("data" in m for m in msgs), f"'data' should be in missing: {msgs}"
    assert any("config" in m for m in msgs), f"'config' should be in missing: {msgs}"


def test_mixed_missing_symbols():
    """同时有常量 + 小写变量缺失 → 都在同一 msg 里."""
    improved = "import somelib\nport = somelib.get(RETRY_PORT)\n"
    level, msgs = _risk(improved)
    assert level == "warn"
    assert any("RETRY_PORT" in m and "NameError" in m for m in msgs), f"got: {msgs}"


# ---- P3: SyntaxError 补 pass 再 parse ----

def test_ok_when_syntax_error_in_partial_code():
    """片段代码 AST 解析必然 fail (def 缺 body 等) → 尝试补 pass, 仍失败 → ok."""
    improved = 'def run():\n    """Run."""\n    while True:'
    level, msgs = _risk(improved)
    assert level == "ok", f"partial code syntax error should be ok, got {level}: {msgs}"


def test_syntax_error_recoverable_detects_missing():
    """def 缺 body 但补 pass 后能 parse, 且确实引用了 missing → warn."""
    improved = "def run():\n    return MISSING_CONST"
    level, msgs = _risk(improved)
    assert level == "warn", f"should detect MISSING_CONST after pass-recovery, got {level}: {msgs}"
    assert any("MISSING_CONST" in m for m in msgs), f"got: {msgs}"


# ---- P2: global / nonlocal / decorator ----

def test_global_not_missing():
    """global 声明的变量不应报 missing."""
    file_src = ["count = 0\n", "\n", "def run():\n", "    pass\n"]
    improved = "def run():\n    global count\n    count += 1\n    return count\n"
    level, msgs = _risk(improved, file_src)
    assert "count" not in (", ".join(msgs)), f"global 'count' should not be missing: {msgs}"


def test_decorator_name_not_missing():
    """装饰器名 (如 dataclass) 如果在文件里 import 了 → 不报."""
    file_src = ["from dataclasses import dataclass\n", "\n", "@dataclass\nclass Svc:\n    x: int\n"]
    improved = "@dataclass\nclass Item:\n    y: int\n"
    level, msgs = _risk(improved, file_src)
    assert "dataclass" not in (", ".join(msgs)), f"decorator 'dataclass' should not be missing: {msgs}"
