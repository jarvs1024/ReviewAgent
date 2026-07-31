"""Unit tests for reviewagent/commands/improve.py alignment logic.

Specifically covers the 3 degraded cases from MR !133:
- L37: `return open(path).read()` → multi-line `with ... as f: return f.read()`
- L43: `except:` → `except (json.JSONDecodeError, ValueError):`
- L56: `return pickle.load(open(...))` → multi-line `with open(...): return pickle.load(...)`
"""
from __future__ import annotations

import pytest

from reviewagent.commands.improve import _code_first_line_matches, ImproveCommand


# ---------- _code_first_line_matches ----------

def test_exact_match():
    assert _code_first_line_matches("x = 1", "x = 1") is True


def test_except_bare_to_typed():
    """L43: `except:` → `except (json.JSONDecodeError, ValueError):`"""
    assert _code_first_line_matches("except:", "except (json.JSONDecodeError, ValueError):") is True


def test_except_no_args_to_with_args():
    """broader case: bare except to typed except on multiline content"""
    assert _code_first_line_matches(
        "    except:",
        "    except (KeyError, ValueError):",
    ) is True


def test_return_open_to_with_block_first_line():
    """L37: first line of improved is `with open(...) as f:` — totally different op.

    Should still be accepted because the model is doing a 1→2 line replacement,
    where the first line of improved REPLACES the original `return ...` line.
    Caller must pre-mark this as multi-line replacement.
    """
    # The function alone can't tell this is multi-line — the caller must handle
    # it. But prefix matching should be relaxed for known multi-line replacements.
    assert _code_first_line_matches("return open(path).read()", "with open(path) as f:") is False
    # (false from the strict 4-char prefix; caller will bypass via multi-line check)


def test_different_first_line_completely_unrelated():
    """Truly different lines should NOT match."""
    assert _code_first_line_matches("x = 1", "y = 2") is False
    assert _code_first_line_matches("def foo():", "import os") is False


def test_def_line_different_body():
    assert _code_first_line_matches("def foo():", "def foo(x):") is True


def test_assignment_same_lhs():
    assert _code_first_line_matches("x = compute()", "x = compute(2)") is True


def test_return_different_expr():
    """return with same first identifier should match."""
    assert _code_first_line_matches("return result", "return result.value") is True


# ---------- _normalise_suggestion: suggestion:-N dynamic ----------

def _make_suggestion_dict(file, start_line, existing, improved):
    return {
        "file": file,
        "start_line": start_line,
        "existing_code": existing,
        "improved_code": improved,
        "header": "test",
        "rationale": "test",
        "label": "test",
        "severity": "low",
    }


def test_normalise_one_to_one():
    """1 line existing → 1 line improved → suggestion:-0+0.

    新逻辑 (8d439cb): GitLab +N = N lines AFTER comment line, 替换 N+1 行
    (注释行 + N 行后续). 替换 N 行 existing → +N-1. 单行替换 → suggestion:-0+0.
    """
    out = ImproveCommand._normalise_suggestion(_make_suggestion_dict(
        "x.py", 10, "x = 1", "x = 2",
    ))
    assert "suggestion:-0+0" in out["body"], f"got: {out['body']!r}"
    assert "x = 2" in out["body"]


def test_normalise_one_to_two():
    """1 line existing → 2 lines improved → suggestion:-0+0 (头部替换, GitLab 自动插入后续行).

    新逻辑: existing 1 行 → n_replace = 0. 头部 suggestion 替换注释行,
    后续 improved 行由 GitLab 在该位置后插入. 这是 L37 / L56 等 1→N 替换的标准格式.
    """
    out = ImproveCommand._normalise_suggestion(_make_suggestion_dict(
        "x.py", 10,
        "return open(p).read()",
        "with open(p) as f:\n    return f.read()",
    ))
    assert "suggestion:-0+0" in out["body"], f"got: {out['body']!r}"
    assert "with open(p) as f:" in out["body"]


def test_normalise_three_to_four():
    """3 line existing → 4 lines improved → suggestion:-0+2.

    新逻辑: existing 3 行 → n_replace = max(0, 3-1) = 2. 头部替换 3 行
    (注释 + 2 行后续), 第 4 行 improved 由 GitLab 插入.
    """
    out = ImproveCommand._normalise_suggestion(_make_suggestion_dict(
        "x.py", 10,
        "def f():\n    x = 1\n    return x",
        "def f():\n    x = 1\n    y = 2\n    return x + y",
    ))
    assert "suggestion:-0+2" in out["body"], f"got: {out['body']!r}"


def test_normalise_no_existing_defaults_one():
    """No existing_code → 默认 1 行替换 → suggestion:-0+0 (新逻辑: n_replace = max(0, len-1) = 0)."""
    out = ImproveCommand._normalise_suggestion({
        "file": "x.py",
        "start_line": 10,
        "improved_code": "x = 2",
        "header": "t", "rationale": "t", "label": "t", "severity": "low",
    })
    assert "suggestion:-0+0" in out["body"]


# ---------- _validate_suggestion (multi-line replacement) ----------

class _FakeWS:
    """Minimal worktree stub for _validate_suggestion tests."""
    def __init__(self, worktree: str):
        self.worktree = worktree
        self.diff_file = ""


def _make_command_with_source(file_path: str, content: str):
    """Return a ImproveCommand instance with `file_sources` pre-populated."""
    cmd = ImproveCommand.__new__(ImproveCommand)
    cmd.ws = _FakeWS("/tmp/fake")
    cmd.project_id = 1
    cmd.mr_iid = 1
    return cmd, {file_path: content.splitlines()}


def test_validate_one_line_to_multi_line():
    """L37: 1-line existing → 2-line improved (with open + return)."""
    cmd, file_sources = _make_command_with_source(
        "x.py",
        "def f(path):\n    return open(path).read()\n",
    )
    line_map = {"x.py": {2}}  # line 2 is the only diff-valid line
    decision = cmd._validate_suggestion(
        file_path="x.py",
        start_line=2,
        improved_code="with open(path) as f:\n    return f.read()",
        existing_code="return open(path).read()",
        line_map=line_map,
        file_sources=file_sources,
    )
    assert decision["action"] == "post", f"got: {decision}"
    assert decision["new_line"] == 2


def test_validate_except_bare_to_typed():
    """L43: `except:` → `except (json.JSONDecodeError, ValueError):`."""
    cmd, file_sources = _make_command_with_source(
        "x.py",
        "def f():\n    try:\n        x = 1\n    except:\n        x = 0\n    return x\n",
    )
    line_map = {"x.py": {5}}  # except: is line 5 (the diff-introduced line)
    decision = cmd._validate_suggestion(
        file_path="x.py",
        start_line=5,
        improved_code="    except (json.JSONDecodeError, ValueError):",
        existing_code="    except:",
        line_map=line_map,
        file_sources=file_sources,
    )
    assert decision["action"] == "post", f"got: {decision}"


def test_validate_pickle_load_to_with_block():
    """L56: `return pickle.load(open(...))` → `with open(...) as f: return pickle.load(f)`."""
    cmd, file_sources = _make_command_with_source(
        "x.py",
        "def load(path):\n    return pickle.load(open(path, 'rb'))\n",
    )
    line_map = {"x.py": {2}}
    decision = cmd._validate_suggestion(
        file_path="x.py",
        start_line=2,
        improved_code="    with open(path, 'rb') as f:\n        return pickle.load(f)",
        existing_code="    return pickle.load(open(path, 'rb'))",
        line_map=line_map,
        file_sources=file_sources,
    )
    assert decision["action"] == "post", f"got: {decision}"


def test_validate_unrelated_line_still_rejected():
    """N→N 等行数 + existing_code 反查命中 → 跳过对齐检查 (信任 agent 视图).

    8d439cb 后的新行为: 即使 improved 与原行"看起来无关", 只要现有代码
    反查命中 + 行数一致, validate 直接 post. 设计取舍: 反查命中代表 agent
    至少正确锁定了行, 进一步对齐会过度拦截真正合法的 N→N 替换
    (如 print→logger 这种语义改写).
    """
    cmd, file_sources = _make_command_with_source(
        "x.py",
        "def f():\n    x = 1\n    return x\n",
    )
    line_map = {"x.py": {2}}
    decision = cmd._validate_suggestion(
        file_path="x.py",
        start_line=2,
        improved_code="import os",  # 反例: 即使首行无关, 反查命中 → post
        existing_code="x = 1",
        line_map=line_map,
        file_sources=file_sources,
    )
    assert decision["action"] == "post", f"got: {decision}"
    assert decision["reason"] == "ok"


# ---------- _fix_indent ----------

def test_fix_indent_missing_first_line():
    """L27 案例: improved 第一行漏了 4 空格缩进."""
    result = ImproveCommand._fix_indent(
        "    q = f\"SELECT * FROM users WHERE email = '{email}'\"",
        'q = "SELECT * FROM users WHERE email = ?"\n    return conn.execute(q, (email,)).fetchall()',
    )
    lines = result.split('\n')
    assert lines[0].startswith('    '), f"first line should have 4-space indent: {lines[0]!r}"
    assert lines[0] == '    q = "SELECT * FROM users WHERE email = ?"'


def test_fix_indent_correct_unchanged():
    """已正确的缩进不应被改动."""
    src = '    return open(path).read()'
    improved = '    with open(path) as f:\n        return f.read()'
    result = ImproveCommand._fix_indent(src, improved)
    assert result == improved


def test_fix_indent_partial_indent():
    """第一行只有部分缩进 (2 空格, target 4 空格) → 应补到 4."""
    result = ImproveCommand._fix_indent(
        '    x = 1',
        '  x = 2',  # 已有 2 空格, 缺 2 空格
    )
    assert result == '    x = 2', f"got: {result!r}"


def test_fix_indent_no_target_line():
    """target_line 为空 → 不修正."""
    result = ImproveCommand._fix_indent('', 'x = 2')
    assert result == 'x = 2'


def test_validate_missing_indent_gets_fixed():
    """端到端: L27 案例 (improved 第一行漏缩进) → validate 返回 normalised_code 修正后版本."""
    cmd, file_sources = _make_command_with_source(
        "x.py",
        "def f():\n    x = 1\n    y = 2\n",
    )
    line_map = {"x.py": {2}}
    decision = cmd._validate_suggestion(
        file_path="x.py",
        start_line=2,
        improved_code="x = 99",   # missing indent
        existing_code="    x = 1",
        line_map=line_map,
        file_sources=file_sources,
    )
    assert decision["action"] == "post", f"got: {decision}"
    assert decision.get("normalised_code", "").startswith("    "), \
        f"got normalised_code: {decision.get('normalised_code')!r}"


# ---------- 新增文件 + agent 输出不连贯: 反查失败 → drop ----------

def test_validate_existing_code_not_found_drops_when_start_in_valid():
    """MR #134 案例: 文件是新增 (start_line in valid), 但 agent 给的 existing_code
    在 worktree 文件 start_line 附近搜不到 (agent 视图过期或行号错位).
    期望: 直接 drop, 不走 snap → 不发 '改进补充' 汇总评论.
    """
    cmd, file_sources = _make_command_with_source(
        "x.py",
        "def f():\n    items = []\n    items.append(1)\n    return items\n",
    )
    # 整文件 valid (新增文件场景)
    line_map = {"x.py": {1, 2, 3, 4}}
    decision = cmd._validate_suggestion(
        file_path="x.py",
        start_line=4,  # valid 内
        improved_code="    except ValueError as e:\n        logger.error(e)",
        existing_code="    except:",  # 文件里没有这行
        line_map=line_map,
        file_sources=file_sources,
    )
    assert decision["action"] == "drop", f"expected drop, got: {decision}"
    assert "existing_code not found" in decision["reason"], f"reason: {decision['reason']}"


def test_validate_existing_code_not_found_snap_when_start_outside_valid():
    """start_line 不在 valid 但 existing_code 反查命中 → snap 后走 multi_line path.

    8d439cb 后: 尾行去重只在 tail_lines == after_lines 时裁, 这里
    '        logger.error(e)' vs '    return 1' 不一致, 不裁, 走 multi_line_replacement.
    """
    cmd, file_sources = _make_command_with_source(
        "x.py",
        "def f():\n    except:\n        pass\n    return 1\n",
    )
    line_map = {"x.py": {1, 4}}  # only line 1 and 4 are diff-introduced
    decision = cmd._validate_suggestion(
        file_path="x.py",
        start_line=2,  # not in valid (agent 给错行号)
        improved_code="    except ValueError as e:\n        logger.error(e)",
        existing_code="    except:",  # 实际在 line 2
        line_map=line_map,
        file_sources=file_sources,
    )
    assert decision["action"] == "post", f"got: {decision}"
    assert decision["reason"] == "multi_line_replacement"


def test_validate_no_existing_code_still_snaps():
    """agent 没给 existing_code (空字符串) → 走原 snap 行为, 不被新逻辑影响."""
    cmd, file_sources = _make_command_with_source(
        "x.py",
        "def f():\n    return items\n",
    )
    line_map = {"x.py": {1, 2}}
    decision = cmd._validate_suggestion(
        file_path="x.py",
        start_line=2,  # in valid
        improved_code="    return items or []",
        existing_code="",  # agent 没给
        line_map=line_map,
        file_sources=file_sources,
    )
    # snap 跳过 (start_line=2 已在 valid), step 4 对齐: target='return items' vs imp='return items or []', 共享 token 'items' (>=5字符) → post
    assert decision["action"] == "post", f"expected post, got: {decision}"
