"""E2E Apply 模拟的补丁范围必须与 GitLab 真实 Apply 一致（N10 回归）。

Why:
`apply_closure` 特性要「真实模拟一次 Apply」：把建议的 improved_code 写进文件 →
commit → push → 期望 webhook 的 auto_detect 把这条建议判成 applied。

GitLab 的 `Apply suggestion` 按钮替换的是**建议块的旧侧**，而旧侧行数由
`reviewagent/review/policies/python.py` 决定：

    n_replace = max(0, len(existing_code.split("\\n")) - 1)

即：**替换 `existing_code` 实际覆盖的行数**（从 `target_line` 起）。

E2E 之前用 DB 的 `target_line_end` 当替换范围，而 `target_line_end` 存的是
**新代码跨度**（`publishing.py` 的 `n_lines = len(improved_code.split("\\n"))`）。
1→N 的多行替换里它比原代码跨度**大** → 补丁**多删**了原代码之外的行，
把文件改坏 → auto_detect 认不出「已落地」→ apply_closure 误报红
（实测：E2E 全量档 `apply_closure` 失败，state 停在 open / resolved）。

测试只跑纯函数 `_apply_suggestion_to_file`，不连 GitLab / Redis。
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "e2e"))

from flows import _apply_suggestion_to_file  # noqa: E402

_FILE = """def save(items):
    try:
        with open("out.txt", "w") as handle:
            handle.write(str(load(items)))
    except:
        pass


def cache_value(key, cache={}):
    return cache.get(key)
"""


def _row(**kw):
    base = {
        # _FILE 里 `    except:` 是第 5 行、`        pass` 是第 6 行
        "target_line": 5,
        "target_line_end": 5,
        "existing_code": "    except:\n        pass",
        "improved_code": "",
    }
    base.update(kw)
    return base


def test_multi_line_existing_is_replaced_exactly() -> None:
    """1→1：existing 2 行 → 替换那 2 行，其余不动."""
    improved = "    except OSError:\n        raise"
    out = _apply_suggestion_to_file(_row(improved_code=improved), _FILE)
    assert out is not None
    lines = out.splitlines()
    assert lines[4] == "    except OSError:"
    assert lines[5] == "        raise"
    # 后面的函数必须原样保留
    assert "def cache_value(key, cache={}):" in out
    assert out.count("\n") == _FILE.count("\n")


def test_one_to_many_replacement_does_not_delete_extra_lines() -> None:
    """1→N（核心回归）：existing 2 行、improved 4 行 → 只替换 2 行.

    旧实现按 `target_line_end`（= improved 行数）当范围 → 多删 2 行，
    把后面的函数删坏。
    """
    improved = (
        "    except OSError:\n"
        "        import logging\n"
        "\n"
        '        logging.exception("failed to save out.txt")'
    )
    out = _apply_suggestion_to_file(
        _row(target_line_end=8, existing_code="    except:\n        pass", improved_code=improved),
        _FILE,
    )
    assert out is not None
    lines = out.splitlines()
    assert lines[4] == "    except OSError:"
    assert lines[5] == "        import logging"
    assert lines[6] == ""
    assert lines[7] == '        logging.exception("failed to save out.txt")'
    # 原代码只占 2 行 → 第 6 行起必须还是原来的空行 + 下一个函数（没被多删）
    assert lines[8] == ""
    assert lines[9] == ""
    assert lines[10] == "def cache_value(key, cache={}):"
    assert "    return cache.get(key)" in out


def test_delete_removes_the_existing_block() -> None:
    """delete：improved 为空 → 删掉 existing 覆盖的行."""
    out = _apply_suggestion_to_file(
        _row(target_line_end=6, existing_code="    except:\n        pass", improved_code=""),
        _FILE,
    )
    assert out is not None
    assert "    except:" not in out
    assert "        pass" not in out
    assert "def cache_value(key, cache={}):" in out


def test_empty_existing_falls_back_to_target_line_end_span() -> None:
    """existing 为空（模型没给原文）→ 退回 target_line_end 跨度."""
    out = _apply_suggestion_to_file(
        _row(target_line=5, target_line_end=6, existing_code="", improved_code="x = 1"),
        _FILE,
    )
    assert out is not None
    lines = out.splitlines()
    assert lines[4] == "x = 1"
    assert lines[5] == ""


def test_target_line_out_of_range_returns_none() -> None:
    assert _apply_suggestion_to_file(_row(target_line=999), _FILE) is None
