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


def test_ok_when_all_symbols_defined():
    """所有符号都定义了 → ok."""
    file_src = ["import os\n", "\n", "RETRY_PORT = 8080\n", "\n", "def run():\n", "    pass\n"]
    improved = "import os\nRETRY_PORT = 8080\n"
    level, msgs = _cmd()._detect_apply_risk(
        file_path="svc.py", target_line=1, improved_code=improved,
        file_sources={"svc.py": file_src},
    )
    assert level == "ok", f"expected ok, got {level}: {msgs}"
    assert msgs == []


def test_warn_when_undefined_constant():
    """引用未定义常量 → warn + 列出符号."""
    file_src = ["def run():\n", "    pass\n"]
    improved = "def run():\n    port = RETRY_PORT\n"
    level, msgs = _cmd()._detect_apply_risk(
        file_path="svc.py", target_line=1, improved_code=improved,
        file_sources={"svc.py": file_src},
    )
    assert level == "warn"
    assert any("RETRY_PORT" in m for m in msgs), f"missing RETRY_PORT in {msgs}"


def test_ok_when_syntax_error_in_partial_code():
    """片段代码 AST 解析必然 fail (def 缺 body 等) → ok.

    Why: improved_code 是 LLM 给的片段 (部分函数), 本身不完整, AST 解析必然失败,
    但这不代表 missing symbol. 视为 ok, 让 prompt 治本.
    """
    file_src = ["def run():\n", "    pass\n"]
    improved = 'def run():\n    """Run."""\n    while True:'
    level, msgs = _cmd()._detect_apply_risk(
        file_path="svc.py", target_line=1, improved_code=improved,
        file_sources={"svc.py": file_src},
    )
    assert level == "ok", f"partial code syntax error should be ok, got {level}: {msgs}"


def test_ok_when_symbol_defined_in_improved():
    """improved_code 自己定义 + 使用 → ok (避免自引用 false positive)."""
    file_src = ["def run():\n", "    pass\n"]
    improved = "MAX = 10\nvalue = MAX\n"
    level, msgs = _cmd()._detect_apply_risk(
        file_path="svc.py", target_line=1, improved_code=improved,
        file_sources={"svc.py": file_src},
    )
    assert level == "ok", f"expected ok (MAX defined in improved), got {level}: {msgs}"


def test_skip_builtins():
    """builtins / 关键字 → ok, 不报."""
    file_src = ["def run():\n", "    pass\n"]
    improved = "def run():\n    return True if None else False\n"
    level, msgs = _cmd()._detect_apply_risk(
        file_path="svc.py", target_line=1, improved_code=improved,
        file_sources={"svc.py": file_src},
    )
    assert level == "ok", f"expected ok (only builtins), got {level}: {msgs}"


def test_skip_unimported_module_name():
    """已 import 的名字应当 ok."""
    file_src = ["def run():\n", "    pass\n"]
    improved = "import os\nport = os.getenv('X', 0)\n"
    level, msgs = _cmd()._detect_apply_risk(
        file_path="svc.py", target_line=1, improved_code=improved,
        file_sources={"svc.py": file_src},
    )
    assert level == "ok", f"os imported should be ok, got {level}: {msgs}"


def test_missing_file_returns_ok():
    """file_path 不在 file_sources 中 → ok (no false positive, 没数据不警告)."""
    improved = "x = SOMETHING\n"
    level, msgs = _cmd()._detect_apply_risk(
        file_path="svc.py", target_line=1, improved_code=improved,
        file_sources={},
    )
    assert level == "ok", f"no file_sources → ok, got {level}: {msgs}"


def test_argument_name_marks_defined():
    """函数参数 / lambda 参数 算已定义, 不报."""
    file_src = ["def run(arg):\n", "    return arg\n"]
    improved = "def run(arg):\n    return arg\n"
    level, msgs = _cmd()._detect_apply_risk(
        file_path="svc.py", target_line=1, improved_code=improved,
        file_sources={"svc.py": file_src},
    )
    assert level == "ok", f"arg defined, got {level}: {msgs}"


def test_truly_missing_has_actionable_hint():
    """truly_missing (大写常量) 给出 actionable 提示."""
    file_src = ["def run():\n", "    pass\n"]
    improved = "port = RETRY_PORT\n"
    level, msgs = _cmd()._detect_apply_risk(
        file_path="svc.py", target_line=1, improved_code=improved,
        file_sources={"svc.py": file_src},
    )
    assert level == "warn"
    assert any("RETRY_PORT" in m and "NameError" in m for m in msgs)
    assert any("add RETRY_PORT" in m or "import RETRY_PORT" in m for m in msgs)


def test_likely_module_classified_separately():
    """小写无下划线 → likely_module 分支 (温和提示)."""
    file_src = ["def run():\n", "    pass\n"]
    improved = "val = somemodule.func()\n"
    level, msgs = _cmd()._detect_apply_risk(
        file_path="svc.py", target_line=1, improved_code=improved,
        file_sources={"svc.py": file_src},
    )
    assert level == "warn"
    assert any("疑似" in m or "模块" in m for m in msgs), f"got: {msgs}"


def test_mixed_truly_missing_and_likely():
    """同时有常量缺失 + 模块未 import → 两条 msg."""
    file_src = ["def run():\n", "    pass\n"]
    improved = "import somelib\nport = somelib.get(RETRY_PORT)\n"
    level, msgs = _cmd()._detect_apply_risk(
        file_path="svc.py", target_line=1, improved_code=improved,
        file_sources={"svc.py": file_src},
    )
    assert level == "warn"
    assert len(msgs) >= 1
    has_truly = any("RETRY_PORT" in m and "NameError" in m for m in msgs)
    assert has_truly, f"expected RETRY_PORT truly_missing, got {msgs}"


def test_self_cls_excluded():
    """self / cls 不算 missing."""
    file_src = ["class Svc:\n", "    def __init__(self):\n", "        self.x = 1\n"]
    improved = "class Svc:\n    def method(self):\n        return self.x\n"
    level, msgs = _cmd()._detect_apply_risk(
        file_path="svc.py", target_line=1, improved_code=improved,
        file_sources={"svc.py": file_src},
    )
    assert level == "ok", f"self.x should be ok, got {level}: {msgs}"


def test_kwonly_args_recognized():
    """kwonly args (e.g. *, timeout) 也算已定义."""
    file_src = ["def run(timeout=10, *, retries):\n", "    pass\n"]
    improved = "def run(timeout=10, *, retries):\n    if retries > 5:\n        print(timeout)\n"
    level, msgs = _cmd()._detect_apply_risk(
        file_path="svc.py", target_line=1, improved_code=improved,
        file_sources={"svc.py": file_src},
    )
    assert level == "ok", f"kwonly args should be ok, got {level}: {msgs}"
