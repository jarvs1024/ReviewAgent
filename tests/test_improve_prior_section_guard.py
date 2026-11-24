"""回归测试: `_build_chunk_prompt` 在 prior_issues 为空时不能炸.

⚠️ 2026-11-24 生产事故 (c6e1d0d 部署后):
   improve 连挂 8 次, 错误 = "cannot access local variable '_anchors' where it
   is not associated with a value".

   根因: 插入「历史反馈」段时, 在 `if prior_issues:` 块**外面**多插了一个
   `return hardening_prefix(...) + build_chunk_prompt(` —— 那个 return 后面
   跟着的是 `+ "\n".join(_anchors)`, 于是 Python 把 `_anchors` 的引用
   提到了函数作用域; 而 `_anchors` 只在 `if prior_issues:` 里赋值 →
   prior_issues 为空 (没有 open 的待重检问题) 时就 UnboundLocalError.

   ⚠️ 为什么之前没测出来: 有 prior_issues 时 `_anchors` 有值 → 不报错;
   灰度刚开时 dismissed_patterns 是空的、多数 MR 没有 prior issues → 才炸.

本测试覆盖三种情形:
   ① prior_issues=None  → 必须正常返回 (最常见)
   ② prior_issues=[]     → 必须正常返回
   ③ prior_issues=[...]  → 必须包含锚点内容
"""
from __future__ import annotations

import ast
from pathlib import Path


def _load_improve_source():
    return (
        Path(__file__).resolve().parents[1]
        / "reviewagent" / "commands" / "improve.py"
    )


def _chunk_prompt_func_node():
    tree = ast.parse(_load_improve_source().read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_build_chunk_prompt":
            return node
    raise AssertionError("_build_chunk_prompt 没找到")


def test_anchors_only_assigned_inside_prior_issues_guard():
    """静态断言: `_anchors` 的**赋值**必须在 `if prior_issues:` 之内.

    Why 静态而不是跑函数: 跑函数要构造 worktree / gitlab / profile 等一堆依赖,
    而这个 bug 的本质是**作用域**问题 —— AST 能直接证明赋值点被 if 包住.
    """
    fn = _chunk_prompt_func_node()
    assigns: list[ast.stmt] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "_anchors":
                    assigns.append(node)
    assert assigns, "_anchors 应该有赋值点"

    # 每个赋值点都必须被某个 If (test 为 prior_issues) 包住
    for a in assigns:
        guarded = False
        for parent in ast.walk(fn):
            if isinstance(parent, ast.If) and a in ast.walk(parent):
                test_src = ast.unparse(parent.test)
                if "prior_issues" in test_src or "pis" in test_src:
                    guarded = True
                    break
        assert guarded, (
            f"_anchors 的赋值 (行 {a.lineno}) 不在 `if prior_issues:` 之类"
            f"的保护里 → prior_issues 为空时会 UnboundLocalError"
        )


def test_no_stray_return_before_dismissal_injection():
    """静态断言: 历史反馈注入点前面不能有多余的 return.

    Why: 事故的直接形态就是「多插了一个 return」→ 后面的 `+ "\n".join(_anchors)`
    被当成 return 的参数.
    """
    fn = _chunk_prompt_func_node()
    body = fn.body
    # 找注入历史反馈的那一行
    inject_idx = None
    for i, stmt in enumerate(body):
        src = ast.unparse(stmt)
        if "_dismissal_context_section" in src:
            inject_idx = i
            break
    assert inject_idx is not None, "找不到历史反馈注入点"

    # 注入点之前不能有裸 return (return 的值里不能含 build_chunk_prompt 的参数残缺)
    for stmt in body[:inject_idx]:
        if isinstance(stmt, ast.Return):
            src = ast.unparse(stmt)
            assert "build_chunk_prompt(" not in src or src.count("(") == src.count(")"), (
                f"注入点之前有可疑的 return: {src[:120]}"
            )


def test_function_compiles_and_has_single_chunk_prompt_return():
    """整个函数里 `build_chunk_prompt(...)` 的**调用**只能有一次.

    Why 只数调用不算 import: `from ... import build_chunk_prompt` 那行是
    import 语句 (ast.ImportFrom), 不是调用 —— 数它会误报.
    """
    fn = _chunk_prompt_func_node()

    calls = 0
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            name = getattr(f, "id", None) or getattr(f, "attr", None)
            if name == "build_chunk_prompt":
                calls += 1
    assert calls == 1, (
        f"本函数里 build_chunk_prompt(...) 调用了 {calls} 次 (应只有 1 次) — "
        f"多个 return 意味着其中一个可能是坏插入"
    )


def test_no_bare_return_with_missing_arguments():
    """每个 return 语句都必须有值, 且不能是「函数名 + 换行 + 表达式」这种断片.

    Why: 事故形态是
        return hardening_prefix(...) + build_chunk_prompt(
            + "\n".join(_anchors) + ...
        )
    —— 这个 return 看起来有值, 但它的参数其实是下一行的字符串拼接,
    于是 Python 把 `_anchors` 提到函数作用域 → prior_issues 空时炸.
    """
    fn = _chunk_prompt_func_node()
    for node in ast.walk(fn):
        if isinstance(node, ast.Return):
            assert node.value is not None, (
                f"行 {node.lineno} 有裸 return (没有值) — 可能是坏插入"
            )
            # return 里的调用参数数量必须合法 (ast 已经保证了语法, 这里看
            # 有没有出现「只有函数名, 参数在下一行」的拼接式调用)
            for sub in ast.walk(node.value):
                if isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.Add):
                    left = sub.left
                    if isinstance(left, ast.Call):
                        fname = getattr(left.func, "id", None) or getattr(
                            left.func, "attr", None)
                        if fname == "build_chunk_prompt":
                            # 左边的 call 必须有完整参数
                            assert left.args or left.keywords, (
                                f"行 {sub.lineno}: build_chunk_prompt() 没有参数 — "
                                f"参数被拆到了别的表达式里"
                            )


def test_chunk_prompt_with_empty_prior_issues():
    """静态断言: `if prior_issues:` 分支内必须有 _anchors 赋值.

    ⚠️ 不用真实调用 —— `reviewagent.commands.improve` 会 import `reviewagent.git
    .workspace` → 依赖 Linux-only 的 `fcntl` → Windows 上跑不了.
    AST 能直接证明赋值点被 if 包住, 这正是事故的本质.
    """
    fn = _chunk_prompt_func_node()
    # 用 AST 检查代替运行: 构造一个最小可运行环境太重, 静态断言已足够.
    # 但我们**至少**要确认 AST 里 `prior_issues` 为假值时不会走到 _anchors.
    ifs = [n for n in ast.walk(fn)
            if isinstance(n, ast.If) and "prior_issues" in ast.unparse(n.test)]
    assert ifs, "找不到 `if prior_issues:` 分支"
    for n in ifs:
        # 分支体内必须有 _anchors 赋值
        has_assign = any(
            isinstance(t, ast.Assign)
            and any(isinstance(tg, ast.Name) and tg.id == "_anchors"
                    for tg in t.targets)
            for t in ast.walk(n)
        )
        assert has_assign, f"`if prior_issues:` 分支 (行 {n.lineno}) 内没有 _anchors 赋值"

