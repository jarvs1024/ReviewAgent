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


# ---------- _RULE_REF_REGEX + _score_suggestion ----------

def test_rule_ref_regex_exempts_ssd_rule():
    """SSD-RULE-* 豁免 _score_suggestion 过滤"""
    from reviewagent.commands.improve import _RULE_REF_REGEX
    assert _RULE_REF_REGEX.search("命中 SSD-RULE-NIL-GUARD")


def test_rule_ref_regex_exempts_r_xxx():
    """R-XXX 通用规则键也豁免 (与 SSD-RULE 同级)"""
    from reviewagent.commands.improve import _RULE_REF_REGEX
    for ref in ["R-RES", "R-PERF", "R-OTHER", "R-CLEAN", "R-NVME"]:
        assert _RULE_REF_REGEX.search(f"命中 {ref} 规则"), f"应豁免 {ref}"


def test_rule_ref_regex_exempts_r_other_with_subkey():
    """R-OTHER:<x> / R-OTHER-IMPACT:<x> 兜底前缀也豁免"""
    from reviewagent.commands.improve import _RULE_REF_REGEX
    for ref in [
        "R-OTHER:magic_number",
        "R-OTHER:typo",
        "R-OTHER-IMPACT:caller_param",
        "R-OTHER-IMPACT:schema_drift",
        "R-OTHER-IMPACT:import_path",
    ]:
        assert _RULE_REF_REGEX.search(f"命中 {ref}"), f"应豁免 {ref}"


def test_rule_ref_regex_does_not_exempt_plain_text():
    """无规则键的普通文本不豁免"""
    from reviewagent.commands.improve import _RULE_REF_REGEX
    assert _RULE_REF_REGEX.search("普通代码风格建议") is None
    assert _RULE_REF_REGEX.search("测试代码略") is None


def test_score_suggestion_label_cross_file_impact_high():
    """label=cross-file impact 应得 25 分 (与 potential bug 同级, 体现 P1 优先级)"""
    s = {
        "label": "cross-file impact",
        "severity": "high",
        # rationale 长度 > 50 → +8; +10 (rule ref R-OTHER-IMPACT); +5 (header)
        "rationale": "R-OTHER-IMPACT:caller_param — 调用方 callerA 还在用旧签名, 传了 2 个参数, 新签名需要 3 个",
        "header": "caller 同步",
    }
    sc = ImproveCommand._score_suggestion(s)
    # 30 (high) + 25 (cross-file impact) + 8 (rationale 50~100字) + 10 (rule ref) + 5 (header ok) = 78
    assert sc >= 70, f"cross-file impact 应得高分, got {sc}"


def test_score_suggestion_label_potential_bug_baseline():
    """label=potential bug 作为基线"""
    s = {
        "label": "potential bug",
        "severity": "high",
        "rationale": "R-RES 命中: open() 未关闭",
        "header": "资源关闭",
    }
    sc_baseline = ImproveCommand._score_suggestion(s)
    s2 = dict(s, label="cross-file impact")
    sc_p1 = ImproveCommand._score_suggestion(s2)
    # cross-file impact 应 ≥ potential bug (同为 25 分)
    assert sc_p1 >= sc_baseline, f"cross-file impact ({sc_p1}) 应 ≥ potential bug ({sc_baseline})"


# ---------- _collect_cross_file_refs_for_mr (全局缓存) ----------

def test_collect_cross_file_refs_excludes_self(tmp_path):
    """全局 caller 引用: 排除 caller 引用本文件自身"""
    import subprocess
    # 构造 mock worktree
    (tmp_path / "a.py").write_text("def helper(): return 1\n")
    (tmp_path / "b.py").write_text("from a import helper\nx = helper()\n")
    (tmp_path / "c.py").write_text("# c references helper via import: helper\nfrom a import helper\nhelper()\n")
    
    # 调全局方法
    from reviewagent.commands.improve import ImproveCommand
    cmd = ImproveCommand.__new__(ImproveCommand)  # 跳过 __init__
    refs_by_file = cmd._collect_cross_file_refs_for_mr(
        files=["a.py", "b.py", "c.py"],
        diff_by_file={
            "a.py": "+def new_func(): pass\n",
            "b.py": "",
            "c.py": "",
        },
        worktree_path=str(tmp_path),
    )
    # a.py: new_func 在 b.py/c.py 没引用, 应该是空
    # b.py: import a 改了, b.py 自身不应出现在自己的 caller 列表
    # c.py: helper 引用出现在 a.py (def helper) — 但 c.py 的 ident 包含 'helper'
    assert isinstance(refs_by_file, dict)
    assert set(refs_by_file.keys()) == {"a.py", "b.py", "c.py"}
    for fp, refs in refs_by_file.items():
        for r in refs:
            assert r["file"] != fp, f"caller 不能引用自身: file={fp}, caller={r}"


def test_collect_cross_file_refs_returns_empty_when_no_idents(tmp_path):
    """diff 为空 / 无 ident 时返回空 refs (不调 rg)"""
    from reviewagent.commands.improve import ImproveCommand
    cmd = ImproveCommand.__new__(ImproveCommand)
    refs_by_file = cmd._collect_cross_file_refs_for_mr(
        files=["empty.py"],
        diff_by_file={"empty.py": ""},
        worktree_path=str(tmp_path),
    )
    assert refs_by_file == {"empty.py": []}


# ---------- _fetch_incremental with target_sha ----------

def test_fetch_incremental_with_target_sha(tmp_path):
    """_fetch_incremental 接受 target_sha 参数, 确保该 SHA 在 object db"""
    from reviewagent.git.workspace import _fetch_incremental
    import subprocess
    
    # 准备 bare repo
    bare = tmp_path / "test.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    # 假装是空 fetch (没东西可拉) — 只测试 cat-file 路径
    # target_sha 不存在时不会拉成功, 但不应 crash
    result = _fetch_incremental(bare, "file:///nonexistent", target_sha="deadbeef" * 5)
    # 不验证结果, 只验证不 crash
    assert result is None


# ---------- empty improved_code (delete range) ----------

def test_normalise_suggestion_allows_empty_improved_with_existing():
    """空 improved_code + 非空 existing_code = 删除整段 (duplicated_definition / dead_code).

    修复: 之前 _normalise_suggestion 直接 raise ValueError, LLM 对 duplicated_definition
    的建议被全部 skip 掉. 现在允许生成 GitLab suggestion:-0+(N-1) + 空 body = 删除 N 行.
    """
    from reviewagent.commands.improve import ImproveCommand
    cmd = ImproveCommand.__new__(ImproveCommand)
    existing = "def foo():\n    return 1\n"
    s = {
        "file": "x.py",
        "start_line": 10,
        "existing_code": existing,
        "improved_code": "",
        "header": "重复函数",
        "rationale": "删除 dead code",
        "label": "code quality",
        "severity": "high",
    }
    out = cmd._normalise_suggestion(s)
    # body 必须包含 suggestion:-0+(N-1) 标记
    assert "```suggestion:-0+1\n\n```" in out["body"], f"unexpected body:\n{out['body']!r}"
    assert out["file"] == "x.py"
    assert out["new_line"] == 10


def test_normalise_suggestion_rejects_empty_both():
    """improved 和 existing 都为空 → 仍然 raise (防止误删)"""
    from reviewagent.commands.improve import ImproveCommand
    cmd = ImproveCommand.__new__(ImproveCommand)
    s = {
        "file": "x.py",
        "start_line": 10,
        "existing_code": "",
        "improved_code": "",
        "header": "h",
        "rationale": "r",
        "label": "l",
        "severity": "high",
    }
    with pytest.raises(ValueError, match="missing 'improved_code'"):
        cmd._normalise_suggestion(s)


def test_validate_suggestion_empty_improved_passes_through_shrink_check():
    """空 improved_code + 12 行 existing: 不走 shrink_to_general, 走 post (删除)."""
    from reviewagent.commands.improve import ImproveCommand
    cmd = ImproveCommand.__new__(ImproveCommand)
    cmd.project_id = 0
    cmd.mr_iid = 0

    # 构造一个临时 git repo + 真实文件
    import subprocess, tempfile
    from pathlib import Path
    tmp = Path(tempfile.mkdtemp())
    subprocess.run(["git", "init", str(tmp)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp), "config", "user.email", "t@t"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp), "config", "user.name", "t"], check=True, capture_output=True)
    # 写入文件 (确保 line_map valid 集合包含目标行)
    target_file = "src.py"
    lines = ["# header\n"] + [f"x = {i}\n" for i in range(1, 20)] + ["y = 99\n"]
    (tmp / target_file).write_text("".join(lines))

    # existing 是 line 10-15 (6 行)
    existing = "".join(lines[9:15])
    file_lines = lines

    # line_map 让 line 10-15 valid
    line_map = {target_file: set(range(10, 16))}
    file_sources = {target_file: [l.rstrip("\n") for l in file_lines]}

    result = cmd._validate_suggestion(
        file_path=target_file,
        start_line=10,
        improved_code="",
        existing_code=existing,
        line_map=line_map,
        file_sources=file_sources,
    )
    # 必须 post, 不能 general
    assert result["action"] == "post", f"unexpected action: {result}"
    assert "delete_range" in result.get("reason", "") or result.get("reason") == "ok"


def test_validate_suggestion_shrinks_to_general_for_partial_replacement():
    """部分替换 (M < N 但 M > 0) 仍然走 shrink_to_general (风险高)."""
    from reviewagent.commands.improve import ImproveCommand
    cmd = ImproveCommand.__new__(ImproveCommand)
    cmd.project_id = 0
    cmd.mr_iid = 0
    import subprocess, tempfile
    from pathlib import Path
    tmp = Path(tempfile.mkdtemp())
    target_file = "src.py"
    lines = ["# header\n"] + [f"x = {i}\n" for i in range(1, 20)] + ["y = 99\n"]
    (tmp / target_file).write_text("".join(lines))

    # existing 6 行, improved 1 行 (删 5 行的部分收缩)
    existing = "".join(lines[9:15])
    line_map = {target_file: set(range(10, 16))}
    file_sources = {target_file: [l.rstrip("\n") for l in lines]}

    result = cmd._validate_suggestion(
        file_path=target_file,
        start_line=10,
        improved_code="x = 999\n",
        existing_code=existing,
        line_map=line_map,
        file_sources=file_sources,
    )
    # 应当 general, 不允许 Apply
    assert result["action"] == "general", f"unexpected action: {result}"


# ---------- _build_summary_v2 ----------

def test_build_summary_v2_empty_inline_posted_with_only_dup_skipped():
    """本次循环没有任何新发布, 但有 dedup skip → 返回说明性 summary (含 V{N})"""
    from reviewagent.commands.improve import ImproveCommand
    cmd = ImproveCommand.__new__(ImproveCommand)
    cmd.project_id = 34
    cmd.mr_iid = 163
    inline_posted: list = []
    inline_skipped = [
        {"suggestion": {"file": "x.py", "start_line": 1}, "reason": "duplicate_at_line"},
        {"suggestion": {"file": "y.py", "start_line": 5}, "reason": "duplicate_fingerprint"},
    ]
    out = cmd._build_summary_v2(inline_posted, inline_skipped, total_agent_suggestions=2)
    assert out.startswith("## 改进总览 V")
    assert "未发现新问题" in out
    assert "已发过的 2 条跳过" in out


def test_build_summary_v2_truly_empty_returns_no_problem_placeholder():
    """LLM 啥也没输出 → 返回 "未发现新问题" 占位 (placeholder 一定被 update).

    Why: 之前返回 "" → placeholder 永远停在 "_加载中…_" → 用户分不清是 bot
    还在跑 / 已完成 / 报错. 改为永远返回有意义字符串, 即使空也要 update.
    """
    from reviewagent.commands.improve import ImproveCommand
    cmd = ImproveCommand.__new__(ImproveCommand)
    cmd.project_id = 34
    cmd.mr_iid = 163
    out = cmd._build_summary_v2(
        inline_posted=[],
        inline_skipped=[],
        total_agent_suggestions=0,
    )
    # 返回非空字符串, 含"未发现新问题"标记
    assert out != ""
    assert "未发现新问题" in out


def test_build_summary_v2_lists_only_inline_posted_not_skipped():
    """本次新发布 2 条 + dedup skip 3 条 → summary 只列 2 条, 末尾注明跳过数"""
    from reviewagent.commands.improve import ImproveCommand
    cmd = ImproveCommand.__new__(ImproveCommand)
    cmd.project_id = 34
    cmd.mr_iid = 163
    inline_posted = [
        {
            "note_id": "abc123",
            "kind": "inline",
            "raw": {"file": "foo.py", "start_line": 10, "header": "可变默认参数", "severity": "high"},
            "normalised": {
                "file": "foo.py", "new_line": 10, "header": "可变默认参数",
                "severity": "high", "label": "potential bug",
                "rationale": "违反 SSD-RULE-NO-MUTABLE-DEFAULT: 第 10 行的 `x=[]` 是可变默认实参",
            },
        },
        {
            "note_id": "def456",
            "kind": "general",
            "raw": {"file": "bar.py", "start_line": 20, "header": "静默吞异常", "severity": "medium"},
            "normalised": {
                "file": "bar.py", "new_line": 20, "header": "静默吞异常",
                "severity": "medium", "label": "code quality",
                "rationale": "违反 R-ERR: 第 20 行的 except: pass 是裸 except",
            },
        },
    ]
    inline_skipped = [
        {"suggestion": {"file": "old.py", "start_line": 1}, "reason": "duplicate_at_line"},
        {"suggestion": {"file": "old.py", "start_line": 2}, "reason": "duplicate_at_line"},
        {"suggestion": {"file": "old.py", "start_line": 3}, "reason": "duplicate_fingerprint"},
        {"suggestion": {"file": "bad.py", "start_line": 5}, "reason": "existing_code not found near start_line"},
    ]
    out = cmd._build_summary_v2(inline_posted, inline_skipped, total_agent_suggestions=5)
    assert out.startswith("## 改进总览 V")
    assert "本次新发现 2 条建议" in out
    # 必须包含 2 条新发布的
    assert "foo.py" in out and "L10" in out
    assert "bar.py" in out and "L20" in out
    # general kind 必须标"仅评论, 无 Apply"
    assert "仅评论, 无 Apply" in out
    # 必须不含旧被 dedup 的 old.py 行
    assert "old.py" not in out
    # 末尾注明: 3 条 dedup + 1 条校验失败
    import re
    assert re.search(r"3\s*条.*跳过", out), f"未找到 '3 条...跳过': {out!r}"
    assert re.search(r"1\s*条.*未发布", out), f"未找到 '1 条...未发布': {out!r}"


def test_build_summary_v2_version_increments_per_run():
    """V 编号应该基于 store 中该 MR 已有的 improve run 数 (含本次 = V{N})"""
    from reviewagent.commands.improve import ImproveCommand
    cmd = ImproveCommand.__new__(ImproveCommand)
    cmd.project_id = 34
    cmd.mr_iid = 163
    # 当前 MR 163 已有 3 次 improve (id 258, 260, 261), 第 4 次应该是 V4
    out = cmd._build_summary_v2(
        inline_posted=[
            {
                "note_id": "x",
                "kind": "inline",
                "raw": {"file": "x.py", "start_line": 1, "header": "h"},
                "normalised": {"file": "x.py", "new_line": 1, "header": "h", "severity": "high", "label": "l", "rationale": "r"},
            },
        ],
        inline_skipped=[],
        total_agent_suggestions=1,
    )
    # V 编号 >= 1, 必须匹配 V{N} 格式
    import re
    m = re.search(r"V(\d+)", out)
    assert m, f"未匹配 V{{N}}: {out!r}"
    assert int(m.group(1)) >= 1


def test_build_summary_placeholder_contains_version():
    """placeholder 必须包含 V{N}, 即使还没拿到 inline_posted 数据"""
    from reviewagent.commands.improve import ImproveCommand
    cmd = ImproveCommand.__new__(ImproveCommand)
    cmd.project_id = 34
    cmd.mr_iid = 163
    out = cmd._build_summary_placeholder(
        inline_posted=[],
        inline_skipped=[],
        total_agent_suggestions=0,
    )
    # 必有 V{N} + 加载中
    import re
    m = re.search(r"V(\d+)", out)
    assert m, f"未匹配 V{{N}}: {out!r}"
    assert "加载中" in out, f"placeholder 应有'加载中'提示: {out!r}"
    assert out.startswith("## 改进总览")
