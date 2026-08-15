"""Unit tests for ImproveCommand._safe_parse_defs + _detect_apply_risk
when the target file itself has a SyntaxError.

Regression: MR299 big_service.py L63 cart} 故意 SyntaxError → 旧代码
except SyntaxError: pass 让 local_defs 留空, 所有 file-level symbols
(import requests / def fetch_orders 等) 全被误判为 missing, 触发
"目标文件未定义 (requests, user_id)" 误报.
"""
from __future__ import annotations

import ast

import pytest

from reviewagent.commands.improve import ImproveCommand


@pytest.fixture
def cmd() -> ImproveCommand:
    """Unbound ImproveCommand 实例 (只调用静态/独立方法不依赖 self.ws)."""
    return ImproveCommand.__new__(ImproveCommand)


# ---------- _safe_parse_defs: helper 单测 ----------

class TestSafeParseDefs:
    def test_full_parse_ok(self, cmd):
        defs, err = cmd._safe_parse_defs(
            "import requests\ndef f(x): return x\n", ast,
        )
        assert err is None
        assert defs == {"requests", "f", "x"}

    def test_target_file_ok(self, cmd):
        """文件 OK 时 error 必须是 None."""
        defs, err = cmd._safe_parse_defs(
            "import json\nclass C: pass\n", ast,
        )
        assert err is None
        assert defs == {"json", "C"}

    def test_syntax_error_near_end_keeps_defs(self, cmd):
        """文件末段 SyntaxError, 但前段 (含 import + def) 能 partial parse."""
        src = (
            "import requests\n"                                  # L1
            "import json\n"                                       # L2
            "def fetch_user(user_id: int) -> dict:\n"            # L3
            "    return requests.get(...)\n"                     # L4
            "\n"                                                  # L5
            "def fetch_orders(user_id: int) -> dict:\n"          # L6
            "    return requests.get(...)\n"                     # L7
            "\n"                                                  # L8
            "def broken():\n"                                    # L9
            '    return f"x}\n'                                   # L10 SyntaxError
        )
        defs, err = cmd._safe_parse_defs(src, ast)
        assert err is not None
        assert err.lineno == 10
        # partial 拿到 L1-L8 的 defs, 但跳过 dangling L9 def broken(): header
        assert {"requests", "json", "fetch_user", "user_id", "fetch_orders"} <= defs
        assert "broken" not in defs

    def test_syntax_error_with_dangling_def_header(self, cmd):
        """错误行紧跟 dangling def 头, partial 回退跳过 dangling 整 def."""
        src = (
            "import requests\n"                     # L1
            "import json\n"                          # L2
            "def fetch_user(user_id: int):\n"        # L3
            "    return user_id\n"                   # L4
            "def broken():\n"                        # L5 dangling header
            '    return f"x}\n'                      # L6 SyntaxError
        )
        defs, err = cmd._safe_parse_defs(src, ast)
        assert err is not None
        assert err.lineno == 6
        # partial 切到 L4, 跳过 L5 dangling
        assert {"requests", "json", "fetch_user", "user_id"} <= defs
        assert "broken" not in defs

    def test_first_line_syntax_error(self, cmd):
        """首行 SyntaxError → defs 空, err 返回."""
        defs, err = cmd._safe_parse_defs("def broken(:\n    pass\n", ast)
        assert not defs
        assert err is not None
        assert err.lineno == 1

    def test_empty_source(self, cmd):
        """空源 → 无 err, 返回空 set."""
        defs, err = cmd._safe_parse_defs("", ast)
        assert defs == set()
        assert err is None


# ---------- _detect_apply_risk: 集成单测 (回归 MR299) ----------

class TestDetectApplyRiskTargetSyntaxError:
    """MR299 回归: big_service.py L63 cart} SyntaxError 时不应误报."""

    def test_mr299_big_service_l63_no_false_missing(self, cmd):
        """真实生产场景: 文件后段 (L63) 有 f-string SyntaxError,
        improved_code 引用了 file-level import (requests) + FunctionDef arg (user_id).
        旧逻辑会报 '目标文件未定义 (requests, user_id)'; 新逻辑应只发 SyntaxError hint.
        """
        # 模拟 big_service.py: 前面 L1-L62 全 OK, L63 cart} SyntaxError
        src = (
            '"""Big service."""\n'                               # L1
            'import json\n'                                       # L2
            'import time\n'                                       # L3
            'import requests\n'                                   # L4
            '\n'                                                  # L5
            'def fetch_user(user_id: int) -> dict:\n'             # L6
            '    return requests.get(f"https://api.example.com/users/{user_id}", timeout=30).json()\n'  # L7
            '\n'                                                  # L8
            'def fetch_orders(user_id: int) -> dict:\n'           # L9
            '    return requests.get(f"https://api.example.com/users/{user_id}/orders").json()\n'   # L10
            '\n'                                                  # L11
            'def fetch_cart(user_id: int) -> dict:\n'             # L12
            '    return requests.get(f"https://api.example.com/users/{user_id}/cart}").json()\n'   # L13 SyntaxError
        )
        improved = (
            '    return requests.get(f"https://api.example.com/users/{user_id}/orders", timeout=30).json()\n'
        )
        level, msgs = cmd._detect_apply_risk(
            file_path="services/big_service.py",
            improved_code=improved,
            file_sources={"services/big_service.py": src.splitlines()},
            suggestion_text="R-TIME: requests.get() 未设置 timeout",
        )
        # 关键断言: 不再有 "目标文件未定义 X" 误报
        for m in msgs:
            assert "目标文件未定义" not in m, (
                f"误报 - 不应有 '目标文件未定义' 提示, 实际 got: {msgs}"
            )
        # 期望 warn (有 SyntaxError hint)
        assert level == "warn"
        # 期望有 SyntaxError hint
        assert any("SyntaxError" in m for m in msgs), (
            f"应有 SyntaxError hint, 实际 got: {msgs}"
        )

    def test_target_first_line_syntax_error(self, cmd):
        """文件首行就 SyntaxError → 不要误报 missing, 应直接给根本 hint."""
        src = "def broken(:\n    pass\n"
        improved = "def f():\n    return 1\n"
        level, msgs = cmd._detect_apply_risk(
            file_path="x.py",
            improved_code=improved,
            file_sources={"x.py": src.splitlines()},
            suggestion_text="",
        )
        assert level == "warn"
        for m in msgs:
            assert "目标文件未定义" not in m, "首行 SyntaxError 不应误报 missing"
        assert any("目标文件本身有 SyntaxError" in m for m in msgs)
        assert any("apply 当前行可以" in m for m in msgs)

    def test_target_file_ok_no_hint(self, cmd):
        """正常文件 + improved 用未定义 symbol → 仍正常报 missing, 不发 SyntaxError hint."""
        ok_file = [
            "import os",
            "def main():",
            "    pass",
        ]
        improved = "def helper():\n    return SomeUnknownClass()\n"
        level, msgs = cmd._detect_apply_risk(
            file_path="x.py",
            improved_code=improved,
            file_sources={"x.py": ok_file},
            suggestion_text="",
        )
        assert level == "warn"
        assert any("SomeUnknownClass" in m and "目标文件未定义" in m for m in msgs)
        # 正常文件不应发 SyntaxError hint
        for m in msgs:
            assert "SyntaxError" not in m, f"正常文件不应发 SyntaxError hint, got: {m}"

    def test_improved_code_uses_defined_symbol(self, cmd):
        """improved_code 用 file-level 已定义 symbol → ok, 无任何提示."""
        ok_file = [
            "import requests",
            "def fetch_user(user_id: int):",
            "    return user_id",
        ]
        improved = "    return user_id\n"
        level, msgs = cmd._detect_apply_risk(
            file_path="x.py",
            improved_code=improved,
            file_sources={"x.py": ok_file},
            suggestion_text="",
        )
        assert level == "ok"
        assert not msgs

    def test_improved_code_self_syntax_error(self, cmd):
        """改进片段自身语法错 (def 缺 body) → silently ok, 不要发误导提示."""
        ok_file = ["import requests", "def f(): pass"]
        level, msgs = cmd._detect_apply_risk(
            file_path="x.py",
            improved_code="def broken(:\n",
            file_sources={"x.py": ok_file},
            suggestion_text="",
        )
        assert level == "ok"
        assert not msgs
