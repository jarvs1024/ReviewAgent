"""Python 语言策略 + 从 improve.py 迁出的纯逻辑.

本模块是 Python 场景的「唯一实现」：`ImproveCommand` 的同名成员退化为薄委托
（保持 staticmethod / instance method 签名不变），`PythonPolicy` 把 profile /
config / read_lines 装配后调用同一批模块级函数——不存在第二份活逻辑.

纯函数保持无全局可变状态：规则前缀、测试特征关键字、文件读取都从参数注入，
由调用方在**调用时**解析（测试 monkeypatch `reviewagent.commands.improve.config`
时门面读到的是补丁值）.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Any

from reviewagent.review.policy import score_by_table
from reviewagent.review.types import FileContext, FileKind, RiskVerdict, Suggestion

# Python 关键字 / 内建名 — token 采纳判定与标识符提取的排除集.
# `reviewagent.commands.suggestion_actions._KEYWORDS` 复用同一份（None 默认值语义）.
PYTHON_KEYWORDS: frozenset[str] = frozenset(
    {
        "if", "else", "elif", "for", "while", "return", "def", "class",
        "import", "from", "as", "with", "try", "except", "finally",
        "raise", "pass", "in", "is", "not", "and", "or",
        "True", "False", "None", "lambda", "yield", "global", "nonlocal",
        "assert", "del", "break", "continue", "self", "cls",
    }
)

# 每条 suggestion 末尾追加 /adopt /dismiss 帮助文本 (与 pr-agent 一致)
# 末段提醒用户用 MR 评论 + 命令标记处理, 避免直接点「解决主题」或 ✓ 图标关闭 thread —
# 这两条都会被 GitLab 归类为「已关闭(未分类)」, /adopt /dismiss 检测不到, telemetry 缺失处理数据.
HELP_TEXT_FOOTER = (
    "\n\n✅ 接受建议\n"
    "   • 直接用：点上方「应用建议」按钮\n"
    "   • 自己改：请先提交修改，再回复 `/adopt [理由]`\n"
    "\n❌ 关闭建议\n"
    "   • 回复 `/dismiss [理由]`\n"
    "\n💡 为保证数据采集的正确性，请优先使用以上方式来处理MR建议; "
    "非必要不要直接点「解决主题」或 ✓ 图标来关闭主题."
)


# 首行对齐启发式的 stop token 集（关键字 + 常用内建 + 通用 dict key / 短变量名）.
# 不同行共用这些 token 纯属巧合，不能作为 "同一行" 的证据.
# 原实现把该集合内联在函数里且有重复条目；集合语义相同，这里去重并集中定义.
PYTHON_STOP_TOKENS: frozenset[str] = frozenset(
    {
        "def", "return", "self", "None", "True", "False", "and", "or",
        "not", "if", "else", "elif", "for", "while", "in", "is",
        "import", "from", "class", "try", "except", "finally",
        "with", "as", "raise", "pass", "lambda", "yield",
        # Python builtins
        "open", "close", "read", "write", "print", "len", "str", "int",
        "list", "dict", "set", "tuple", "sum", "min", "max", "abs",
        "range", "type", "isinstance", "getattr", "setattr", "delattr",
        "hasattr", "callable", "repr", "format", "hash", "id", "iter",
        "next", "map", "filter", "zip", "enumerate", "sorted", "reversed",
        "any", "all", "bool", "bytes", "bytearray", "complex", "float",
        "frozenset", "object", "property", "staticmethod", "classmethod",
        "super", "input", "eval", "exec", "compile", "globals", "locals",
        "vars", "dir", "chr", "ord", "hex", "oct", "bin", "round",
        "divmod", "pow", "slice", "memoryview", "ascii", "breakpoint",
        "__init__", "__name__", "__main__", "__file__", "__doc__",
        "json", "csv", "sys", "os", "sqlite3",
        # 跨 `os.environ.get(...)` 等通用调用模板
        "environ", "getenv", "get", "items", "keys", "values",
        "append", "extend", "update", "insert", "remove", "delete",
        "split", "join", "strip", "lower", "upper", "replace",
        "encode", "decode", "startswith", "endswith",
        "request", "response", "method", "args", "kwargs",
        # 通用 dict key / 属性名
        "name", "value", "price", "count", "total", "size", "length",
        "status", "level", "code", "message", "msg",
        "data", "result", "error", "info", "warning", "debug",
        "created", "updated", "deleted", "modified", "timestamp",
        "uuid", "key", "secret", "password", "username",
        "host", "port", "addr", "address", "ip", "domain",
        # 常见短变量名
        "path", "file", "val", "obj", "res", "tmp",
        "new", "old", "src", "dst", "err", "out", "cfg",
        "ctx", "item", "rows", "row", "conn", "cur",
        "fp", "fd", "buf", "tag", "ids", "env", "opts", "url", "uri",
        "ret",
        "token", "email", "user",
    }
)


# ---------------------------------------------------------------------------
# 标识符提取 / 测试特征密度
# ---------------------------------------------------------------------------

def extract_identifiers(diff: str) -> list[str]:
    """从 diff 的 `+` 行提取改动的标识符 (def/class/常量/import 名字)."""
    if not diff:
        return []
    # 只看 + 行
    added = "\n".join(ln[1:] for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++"))
    idents: set[str] = set()
    for m in re.finditer(r"^\s*def\s+([A-Za-z_]\w{2,})\s*\(", added, re.MULTILINE):
        idents.add(m.group(1))
    for m in re.finditer(r"^\s*class\s+([A-Za-z_]\w{2,})", added, re.MULTILINE):
        idents.add(m.group(1))
    for m in re.finditer(r"^\s*([A-Z][A-Z0-9_]{2,})\s*[:=]", added, re.MULTILINE):
        idents.add(m.group(1))
    # `from X import A` (单 ident)
    for m in re.finditer(r"^\s*from\s+\S+\s+import\s+([A-Za-z_]\w{2,})\s*(?:$|#)", added, re.MULTILINE):
        idents.add(m.group(1))
    # `from X import A, B, C` (多 ident, 不带括号)
    # 匹配 import 后第一个 ident 后所有逗号分隔 ident
    for m in re.finditer(r"^\s*from\s+\S+\s+import\s+([A-Za-z_]\w{2,})((?:\s*,\s*[A-Za-z_]\w{2,})*)", added, re.MULTILINE):
        idents.add(m.group(1))
        for sub in re.findall(r"\s*,\s*([A-Za-z_]\w{2,})", m.group(2)):
            idents.add(sub)
    # `from X import (A, B)` (括号多 ident)
    for m in re.finditer(r"^\s*from\s+\S+\s+import\s+\(([^)]+)\)", added, re.MULTILINE):
        for part in m.group(1).split(","):
            name = part.strip().split(" as ")[0].strip()
            if len(name) >= 3:
                idents.add(name)
    # 排除常见噪音
    stop = {"self", "cls", "None", "True", "False", "args", "kwargs"}
    return sorted(idents - stop)[:15]


def test_feature_density(
    file_path: str,
    file_size: int,
    *,
    read_lines: Callable[[str], list[str]],
    keywords: Sequence[str],
) -> float:
    """V7 测试特征密度 (0~1): 绝对命中数 + 密度组合.

    高分 → 高风险测试文件 (含 fixture/mock/parametrize/assert/setup-teardown).
    用绝对数 + 密度组合, 避免大文件被稀释 (2000行100assert ≠ 50行10assert).
    """
    if file_size <= 0:
        return 0.0
    lines = read_lines(file_path)
    if not lines:
        return 0.0
    hits = sum(1 for ln in lines if any(kw in ln for kw in keywords))
    if hits == 0:
        return 0.0
    # 绝对数: 30 命中即满分 0.5
    abs_score = min(hits, 30) / 30 * 0.5
    # 密度: 5% 命中率即满分 0.5
    density = hits / len(lines)
    density_score = min(density / 0.05, 1.0) * 0.5
    return abs_score + density_score


# ---------------------------------------------------------------------------
# 行号定位 / 缩进 / 首行对齐 / body 构造
# ---------------------------------------------------------------------------

def find_line_by_existing_code(
    file_lines: list[str],
    existing_code: str,
    *,
    hint_line: int = 0,
    max_window: int = 5,
) -> int | None:
    """在 file_lines 中搜索 existing_code 块，返回匹配首行行号（1-based）.

    匹配策略（按优先级）:
      1. 从 hint_line 附近 ±max_window 找最接近的完整块匹配
      2. 整文件从头到尾找第一个完整块匹配
      3. 找首行精确匹配（fallback — 处理 existing_code 只有一行的情况）

    返回 None 表示没找到.
    """
    if not file_lines or not existing_code.strip():
        return None
    target_lines = existing_code.strip("\n").split("\n")
    # 裁掉 leading trivial 行，避免定位到非代码行
    # 策略：只裁掉明确的非代码行，保留注释/关键字/运算符行
    while target_lines:
        first = target_lines[0].strip()
        if not first:
            # 空行 → 裁掉
            target_lines.pop(0)
            continue
        if first in ('"""', "'''"):
            # 单行 docstring 标记 → 裁掉
            target_lines.pop(0)
            continue
        # 保留注释行
        if first.startswith('#'):
            break
        # 保留关键字开头的行
        if first.startswith(('def ', 'class ', 'if ', 'for ', 'while ', 'return ',
                              'import ', 'from ', 'try', 'except', 'with ', 'yield ',
                              'else', 'elif ', 'case ', 'raise ', 'pass', 'break', 'continue')):
            break
        # 保留包含运算符/括号的行
        if any(c in first for c in '()[]{}=+-*/<>!&|'):
            break
        # 保留以冒号结尾的行（块开始，如 else:）
        if first.endswith(':'):
            break
        # 保留看起来像变量/函数调用的行（包含点号或下划线）
        if '.' in first or '_' in first:
            break
        # 纯文本行（docstring 内容）→ 裁掉
        target_lines.pop(0)
    target_first = target_lines[0].strip() if target_lines else ""
    if not target_first:
        return None

    n = len(file_lines)
    m = len(target_lines)

    def _block_matches_at(start_idx: int) -> bool:
        if start_idx + m > n:
            return False
        for j in range(m):
            if file_lines[start_idx + j].strip() != target_lines[j].strip():
                return False
        return True

    # 1. hint_line 附近（±max_window）
    if hint_line >= 1:
        lo = max(1, hint_line - max_window)
        hi = min(n - m + 1, hint_line + max_window)
        best: int | None = None
        best_dist = max_window + 1
        for s in range(lo, hi + 1):
            if _block_matches_at(s - 1):
                dist = abs(s - hint_line)
                if dist < best_dist:
                    best = s
                    best_dist = dist
        if best is not None:
            return best

    # 2. 整文件从头找
    for s in range(1, n - m + 2):
        if _block_matches_at(s - 1):
            return s

    # 3. 首行精确匹配（fallback）
    for i, line in enumerate(file_lines, start=1):
        if line.strip() == target_first:
            return i
    return None


def fix_indent(target_line: str, improved_code: str) -> str:
    """确保 improved_code 第一行的缩进 == target_line 的缩进.

    模型有时会忘记给 improved 第一行加缩进, 导致 Apply 后格式错乱.
    例如: target=`    q = f"..."`, improved=`q = "..."\n    return ...`
    → 修正为 `    q = "..."\n    return ...`
    """
    lines = improved_code.split("\n")
    if not lines or not target_line:
        return improved_code
    # 提取 target 的前导空白
    target_indent = target_line[: len(target_line) - len(target_line.lstrip())]
    # 第一行当前前导空白
    first = lines[0]
    first_indent_len = len(first) - len(first.lstrip())
    target_indent_len = len(target_indent)
    if first_indent_len < target_indent_len:
        # 补齐缺失的缩进
        pad = target_indent[first_indent_len:]
        lines[0] = pad + first
        return "\n".join(lines)
    return improved_code


def normalise_suggestion(s: dict[str, Any]) -> dict[str, Any]:
    """校验 + 构造 GitLab "Apply suggestion" inline comment body."""
    if not isinstance(s, dict):
        raise ValueError(f"suggestion must be dict, got {type(s).__name__}")
    file_path = s.get("file")
    if not file_path or not isinstance(file_path, str):
        raise ValueError("missing 'file' (str)")
    start_line = s.get("start_line")
    if not isinstance(start_line, int) or start_line <= 0:
        raise ValueError("missing 'start_line' (int > 0)")
    existing = (s.get("existing_code") or "").strip("\n")
    improved = (s.get("improved_code") or "").strip("\n")
    # 空 improved_code 表示删除整段 existing (GitLab suggestion:-0+(N-1) + 空 body)
    # 典型场景: duplicated_definition / dead_code, agent 不知怎么 "删除 N 行"
    # 要求: existing 必须非空, 否则 raise 防止误删
    if not improved and not existing:
        raise ValueError("missing 'improved_code' (non-empty) and empty 'existing_code'")

    header = (s.get("header") or "建议改进").strip()
    rationale = (s.get("rationale") or "").strip()
    label = (s.get("label") or "enhancement").strip()
    severity = (s.get("severity") or "medium").strip().lower()

    # GitLab suggestion 格式: ```suggestion:-A+B
    # - A = 注释行往上删除的行数 (0 = 不删上方行)
    # - B = 注释行往下 (含注释行自身) 替换的行数
    #   GitLab 语义: +B = B lines AFTER the commented line
    #   总替换 = A + 1 (注释行) + B = A + B + 1
    #   要替换 N 行 existing → B = N - 1
    # 例: 替换 line 59~62 (4行) → suggestion:-0+3
    # 注意: -M 会删除 new_line 上方的 M 行，绝对不能用 existing 行数做 M!
    existing_lines = existing.split("\n") if existing else []
    n_replace = max(0, len(existing_lines) - 1)

    body = (
        f"**[{severity.upper()}]** **{header}** — {label}\n\n"
        f"{rationale}\n\n"
        f"```suggestion:-0+{n_replace}\n{improved}\n```"
        + HELP_TEXT_FOOTER
    )
    return {
        "file": file_path,
        "new_line": start_line,
        "improved_code": improved,
        "header": header,
        "rationale": rationale,
        "label": label,
        "severity": severity,
        "body": body,
    }


def code_first_line_matches(target_line: str, improved_first: str) -> bool:
    """启发式: target / improved 第一行是否同一行（容忍空格/标点差异）.

    规则（任一命中即视为同一行）:
      1. 字符串相等（strip 后）
      2. 都是 def 行且函数名一致
      3. 都是 class 行且类名一致
      4. 都是赋值行且左侧变量名一致（`q = ...` vs `q = ...`）
      5. 都是 return 行且第一个 token 一致
      6. 共享 ≥3 字符标识符（去掉通用 stop token）
    """
    if not target_line or not improved_first:
        return False
    if target_line == improved_first:
        return True

    # 0. 前缀必须一致（去除前导空白后前 4 字符一致）— 防止 "sys.stderr.write" vs "def log_event"
    # 这种整行重写的情况
    # 注：调用方应在调用前已经判定 "这是多行替换" (improved 行数 > existing 行数),
    # 在那种情况下 improved_first 不需要和 target_line 是同类操作 (e.g. return → with),
    # 所以多行替换应当从外部直接放行, 不进此函数
    t_pref = target_line.lstrip()[:4]
    f_pref = improved_first.lstrip()[:4]
    if t_pref != f_pref:
        return False

    def _lstrip(s: str) -> str:
        return s.lstrip()

    t = _lstrip(target_line)
    f = _lstrip(improved_first)

    # 同 def 行（函数名一致）
    if t.startswith("def ") and f.startswith("def "):
        m_t = re.match(r"def\s+(\w+)", t)
        m_f = re.match(r"def\s+(\w+)", f)
        if m_t and m_f and m_t.group(1) == m_f.group(1):
            return True

    # 同 class 行
    if t.startswith("class ") and f.startswith("class "):
        m_t = re.match(r"class\s+(\w+)", t)
        m_f = re.match(r"class\s+(\w+)", f)
        if m_t and m_f and m_t.group(1) == m_f.group(1):
            return True

    # except 行（bare except → typed except）— 两者都以 except: 开头
    if t.startswith("except") and f.startswith("except"):
        # 都是 except 关键字开头, 即便 f 加了 (X, Y) 参数也算同一行
        return True

    # 赋值 / 调用同名前缀
    m_t = re.match(r"([A-Za-z_]\w*)\s*[=(.]", t)
    m_f = re.match(r"([A-Za-z_]\w*)\s*[=(.]", f)
    if m_t and m_f and m_t.group(1) == m_f.group(1):
        return True

    # return 行（共享 ≥5 字符非 stop 标识符 — 同一行 return ... 但内部计算表达式不同）
    # 停用词只过滤关键字，不过滤数据键名（price/items 是判断"同一行"的关键信号）
    if t.startswith("return ") and f.startswith("return "):
        _stop = {"def", "return", "self", "for", "while", "in", "is", "and", "or", "not",
                 "if", "else", "elif", "import", "from", "class", "try", "except",
                 "finally", "with", "as", "raise", "pass", "lambda", "yield"}
        t_toks = set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{4,}\b", t)) - _stop
        f_toks = set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{4,}\b", f)) - _stop
        return bool(t_toks & f_toks)

    # token overlap（≥3 字符，去 stop — 包括关键字 + Python 内置函数）
    # 集合内去重（原实现有重复条目，集合语义下等价）.
    stop = set(PYTHON_STOP_TOKENS)
    t_tokens = set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b", t))
    f_tokens = set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b", f))
    overlap = (t_tokens & f_tokens) - stop
    if overlap:
        # 至少有一个 shared token 长度 ≥ 5（排除短缩写）
        return any(len(tok) >= 5 for tok in overlap)

    return False


# ---------------------------------------------------------------------------
# AST 定义收集 / apply 风险检测
# ---------------------------------------------------------------------------

def collect_defined_names(tree: Any, ast_module: Any) -> set[str]:
    """从 AST 中收集所有「定义」的符号名.

    覆盖: ClassDef / FunctionDef / AsyncFunctionDef / Lambda args /
    Import / ImportFrom / Assign / AnnAssign / NamedExpr /
    For target / With optional_vars / global / nonlocal / decorator names.
    """
    defs: set[str] = set()

    def _add_target(tgt):
        if isinstance(tgt, ast_module.Name):
            defs.add(tgt.id)
        elif isinstance(tgt, ast_module.Tuple):
            for el in tgt.elts:
                _add_target(el)
        elif isinstance(tgt, ast_module.Starred):
            _add_target(tgt.value)

    for node in ast_module.walk(tree):
        if isinstance(node, ast_module.ClassDef):
            defs.add(node.name)
        elif isinstance(node, (ast_module.FunctionDef, ast_module.AsyncFunctionDef)):
            defs.add(node.name)
            args = node.args
            for arg in (args.posonlyargs + args.args +
                        args.kwonlyargs + [args.vararg, args.kwarg]):
                if arg:
                    defs.add(arg.arg)
            for dec in node.decorator_list:
                if isinstance(dec, ast_module.Name):
                    defs.add(dec.id)
        elif isinstance(node, ast_module.Lambda):
            args = node.args
            for arg in (args.posonlyargs + args.args +
                        args.kwonlyargs + [args.vararg, args.kwarg]):
                if arg:
                    defs.add(arg.arg)
        elif isinstance(node, (ast_module.Import, ast_module.ImportFrom)):
            for alias in node.names:
                defs.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast_module.Assign):
            for tgt in node.targets:
                _add_target(tgt)
        elif isinstance(node, ast_module.AnnAssign):
            if node.target:
                _add_target(node.target)
        elif isinstance(node, ast_module.NamedExpr):
            _add_target(node.target)
        elif isinstance(node, ast_module.For):
            if node.target:
                _add_target(node.target)
        elif isinstance(node, ast_module.comprehension):
            for gen in [node]:
                if gen.target:
                    _add_target(gen.target)
        elif isinstance(node, ast_module.With):
            for item in node.items:
                if item.optional_vars:
                    _add_target(item.optional_vars)
        elif isinstance(node, ast_module.Global):
            for name in node.names:
                defs.add(name)
        elif isinstance(node, ast_module.Nonlocal):
            for name in node.names:
                defs.add(name)
    return defs


def safe_parse_defs(src: str, _ast_module: Any) -> tuple[set[str], SyntaxError | None]:
    """解析 src 收集 defs, 带 partial fallback.

    返回 (defs, error):
      - 完整 parse 成功 → (full_defs, None)
      - 完整 parse 失败但 first error 之前能 parse → (partial_defs, error)
      - 第一行就错, partial 也失败 → (empty, error)

    partial 策略: 切到 first error 行之前再往前逐行回退 (避免 dangling def header),
    直到 partial 能 parse 或到达 src 起点. 这样能最大限度保留 file-level defs
    (import/def/class) 用于 missing 检测.

    Why: 旧逻辑裸 `except SyntaxError: pass` 让 local_defs 留空,
         file-level import/def/class 全部被误判为 missing
         (MR299 L58 scene: big_service.py L63 cart} SyntaxError
         引发对所有 fetch_* 改进误报 '目标文件未定义 requests/user_id').
    """
    if not src:
        return set(), None
    try:
        tree = _ast_module.parse(src)
        return collect_defined_names(tree, _ast_module), None
    except SyntaxError as e:
        if not e.lineno or e.lineno <= 1:
            return set(), e
        lines = src.splitlines()
        # 从 e.lineno - 1 行往前逐行回退, 直到 partial 能 parse 或归 0
        for end in range(e.lineno - 1, 0, -1):
            head_src = "\n".join(lines[:end])
            try:
                tree_p = _ast_module.parse(head_src)
                return collect_defined_names(tree_p, _ast_module), e
            except SyntaxError:
                continue
        return set(), e


def extract_remedied_symbols(text: str) -> set[str]:
    """从建议正文里提取"已声明补救"的符号.

    命中模式: `from X import A` / `import A` / `补 A` / `添加 A` / `定义 A` 等.
    这些符号虽然当前不在目标文件中, 但建议正文已经明确给出补救方式,
    静态检测不应再报 "apply 后会 NameError" 的错位警告.
    """
    found: set[str] = set()
    if not text:
        return found
    # from X import A, B as C / from X import (A, B)
    for m in re.finditer(r"\bfrom\s+[\w.]+\s+import\s+\(?([^()（）\n]+)\)?", text):
        for part in m.group(1).split(","):
            name = part.strip().split(" as ")[0].strip()
            if re.fullmatch(r"[A-Za-z_]\w*", name):
                found.add(name)
    # import X / import X.Y / import X as Y / import X, Y
    for m in re.finditer(
        r"\bimport\s+([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*(?:\s+as\s+[A-Za-z_]\w*)?"
        r"(?:\s*,\s*[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*(?:\s+as\s+[A-Za-z_]\w*)?)*)",
        text,
    ):
        for part in m.group(1).split(","):
            name = part.strip().split(" as ")[0].strip()
            base = name.split(".")[0]
            if re.fullmatch(r"[A-Za-z_]\w*", base):
                found.add(base)
    # 中文: 补/添加/定义/引入/新增/补充 X (可带反引号 / import 前缀)
    for m in re.finditer(
        r"(?:补|添加|定义|引入|新增|补充)\s+[`\s]*(?:(?:from\s+[\w.]+\s+import\s+)|(?:import\s+))?([A-Za-z_]\w*)",
        text,
    ):
        found.add(m.group(1))
    return found


def detect_apply_risk(
    *,
    file_path: str,
    improved_code: str,
    file_sources: dict[str, list[str]],
    suggestion_text: str = "",
) -> tuple[str, list[str]]:
    """识别 improved_code 中引用了 file_sources 未定义的符号.

    返回 (level, msgs):
      - ("ok", [])              : 全部符号都能解析, apply 安全
      - ("warn", ["...", ...])  : 引用了 missing 符号, apply 后会 NameError / ImportError;
                                   review 中加 ⚠️ 提示, 但保留 Apply 按钮让 reviewer 自行处理

    排除: builtins / 关键字 / self / cls / 已定义 (def/class/import/赋值/函数参数/
    lambda/AnnAssign/NamedExpr/For target/With as/global/nonlocal/decorator).
    suggestion_text: 建议正文 (rationale/header). 若某个 missing 符号在建议正文里
    已声明补救方式 (补 import / 定义), 则降级为 "按建议手动补上" 的提示, 不再误报
    "apply 后会 NameError" (避免提醒与建议内容错位, MR178 4923 场景).
    改进片段本身 AST 解析 syntax error → 尝试 textwrap.dedent + 补 pass 再 parse,
    仍失败则视为 ok (治本靠 prompt 约束 8).
    """
    import ast as _ast
    import textwrap as _tw

    tree = None
    try:
        tree = _ast.parse(improved_code)
    except SyntaxError:
        # 改进片段常不完整 (def 缺 body / while 缺 body), 尝试 dedent + 补 pass
        for suffix in ("pass", "pass", "pass", "pass"):
            candidate = _tw.dedent(improved_code).rstrip() + "\n" + suffix + "\n"
            try:
                tree = _ast.parse(candidate)
                break
            except SyntaxError:
                continue
        if tree is None:
            return "ok", []

    file_lines_local = file_sources.get(file_path, [])
    if not file_lines_local:
        # 没源文件 → 不警告 (避免误报)
        return "ok", []

    file_src = "\n".join(file_lines_local)

    # === 收集本文件已定义符号 (含 SyntaxError partial fallback) ===
    local_defs, file_syntax_err = safe_parse_defs(file_src, _ast)
    # target_unparseable: partial 也没成功 (e.g. 首行即错) →
    # 不能可靠判断 missing, 后续走 file-level hint 路径不发 "目标文件未定义 X" 误报
    target_unparseable = (
        file_syntax_err is not None and not local_defs
    )

    import builtins as _bi
    builtin_names = set(dir(_bi))
    excluded = {"True", "False", "None", "self", "cls"}
    excluded.update(builtin_names)

    # === 收集 improved_code 自身定义的符号 ===
    defined_in_improved = collect_defined_names(tree, _ast)

    all_defined = local_defs | defined_in_improved | excluded

    # === 收集 missing Name + Attribute base ===
    # 规则:
    #   - Name: 必须已定义 (否则 missing)
    #   - Attribute: base 是 self/cls → 跳过 (实例属性动态)
    #              base 是已定义 Name (e.g. logger) → 跳过
    #              base 是未定义 Name → 标 base (避免重复标 attr)
    # 注解上下文豁免: 目标文件有 `from __future__ import annotations` 时,
    # 注解在运行时不被求值, 只出现在注解里的符号不会触发 NameError
    # (e.g. def f(x: Any) -> None, Any 未 import 也能正常运行).
    has_future_annotations = False
    if not target_unparseable and file_src:
        # SyntaxError 时 partial head 已 parse 过, 直接用 partial 段重新检查
        # __future__ import 通常在文件顶部, partial head 一定包含
        fa_src = (
            "\n".join(file_src.splitlines()[: file_syntax_err.lineno - 1])
            if file_syntax_err and file_syntax_err.lineno
            else file_src
        )
        try:
            mod_fa = _ast.parse(fa_src)
            for n in mod_fa.body:
                if isinstance(n, _ast.ImportFrom) and n.module == "__future__":
                    if any(alias.name == "annotations" for alias in n.names):
                        has_future_annotations = True
        except SyntaxError:
            pass

    annot_use: dict[str, int] = {}
    total_use: dict[str, int] = {}
    if has_future_annotations:
        for node in _ast.walk(tree):
            _ann_nodes: list[_ast.AST] = []
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                if node.returns:
                    _ann_nodes.append(node.returns)
                for arg in (
                    list(node.args.posonlyargs) + list(node.args.args)
                    + list(node.args.kwonlyargs)
                ):
                    if arg.annotation:
                        _ann_nodes.append(arg.annotation)
                if node.args.vararg and node.args.vararg.annotation:
                    _ann_nodes.append(node.args.vararg.annotation)
                if node.args.kwarg and node.args.kwarg.annotation:
                    _ann_nodes.append(node.args.kwarg.annotation)
            elif isinstance(node, _ast.AnnAssign) and node.annotation:
                _ann_nodes.append(node.annotation)
            else:
                continue
            for _an in _ann_nodes:
                for _n in _ast.walk(_an):
                    if isinstance(_n, _ast.Name) and not isinstance(_n.ctx, _ast.Store):
                        annot_use[_n.id] = annot_use.get(_n.id, 0) + 1
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Name) and not isinstance(node.ctx, _ast.Store):
                total_use[node.id] = total_use.get(node.id, 0) + 1

    def _is_annotation_only(name: str) -> bool:
        return (
            has_future_annotations
            and annot_use.get(name, 0) > 0
            and annot_use.get(name, 0) == total_use.get(name, 0)
        )

    missing: list[str] = []
    seen_attr_bases: set[int] = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Name):
            if isinstance(node.ctx, _ast.Store):
                continue
            if node.id in all_defined:
                continue
            if _is_annotation_only(node.id):
                continue
            missing.append(node.id)
        elif isinstance(node, _ast.Attribute):
            base = node.value
            if isinstance(base, _ast.Name) and base.id in all_defined:
                continue
            if isinstance(base, _ast.Name) and id(base) not in seen_attr_bases:
                seen_attr_bases.add(id(base))
                if base.id not in all_defined and not _is_annotation_only(base.id):
                    missing.append(base.id)

    # 去重保序
    seen: set[str] = set()
    uniq_missing: list[str] = []
    for n in missing:
        if n not in seen:
            seen.add(n)
            uniq_missing.append(n)

    # 完整文件 (file_syntax_err is None) 时, missing 为空 → ok
    # 文件本身有 SyntaxError 时, 即使 partial 拿到 defs 让 missing 为空,
    # 仍要继续往后走, 让用户看到根本 SyntaxError hint (而不是看似 ok 忽略)
    if not uniq_missing and not file_syntax_err:
        return "ok", []

    # 建议正文已声明补救的符号 (补 import / from-import / 定义 X): 降级提示,
    # 避免"目标文件未定义/NameError"与建议内容打架 (MR178 4923 错位场景).
    remedied = extract_remedied_symbols(suggestion_text)
    remedied_missing = [n for n in uniq_missing if n in remedied]
    hard_missing = [n for n in uniq_missing if n not in remedied]

    msgs: list[str] = []
    if target_unparseable:
        # 文件本身 SyntaxError 且 partial 也无法 parse → 不能可靠判断 missing,
        # 不发"目标文件未定义 X"误报; 改为告诉用户先修根本问题.
        line_no = file_syntax_err.lineno if file_syntax_err else 0
        err_msg = file_syntax_err.msg if file_syntax_err else "unknown syntax error"
        msgs.append(
            f"**目标文件本身有 SyntaxError** (line {line_no}: {err_msg}); "
            f"apply 当前行可以, 但请先修复根本 SyntaxError 再运行文件."
        )
        return "warn", msgs
    if hard_missing:
        symbols_str = ", ".join(hard_missing[:8])
        first = hard_missing[0]
        msgs.append(
            f"**目标文件未定义** ({symbols_str}) — "
            f"apply 后会 `NameError`；请先在文件里 `add {first} = <value>` "
            f"或 `import {first}` 后再 apply"
        )
    if remedied_missing:
        syms_str = ", ".join(remedied_missing[:8])
        msgs.append(
            f"**建议要求补充** ({syms_str}) — 建议正文已说明补救方式 "
            f"(补 import / 定义)；Apply 只替换当前行，请按建议手动补上后再运行"
        )
    # partial parse 成功 (target_unparseable=False) 时也要附加 file-level hint,
    # 让用户知道目标文件本身有错, missing 列表可能不完整 (例如 partial 后段
    # 还有 defs 导致 missing 漏报, 即使 uniq_missing 为空也建议先修根本).
    if file_syntax_err:
        line_no = file_syntax_err.lineno or 0
        err_msg = file_syntax_err.msg or "unknown"
        msgs.append(
            f"ℹ️ 目标文件本身另有 SyntaxError (line {line_no}: {err_msg}); "
            f"partial parse 后 missing 列表可能不完整, 请先修复根本问题."
        )

    return "warn", msgs


# ---------------------------------------------------------------------------
# 源码片段渲染
# ---------------------------------------------------------------------------

def render_python_source_block(
    ctx: FileContext,
    strategy: str = "",
    budget: int = 0,
    cfg: Any = None,
) -> str:
    """把 `ctx.lines` 渲染成带绝对行号的编号片段.

    `ctx.lines` 是 context 层已切好的片段, `ctx.start_line` 是片段首行的
    1-based 行号. 块标题 / 分段合并 / 截断上限 / 段数上限全部由
    `review/context.py` 决定, 策略不感知 — `strategy` / `budget` / `cfg`
    仅为协议签名保留.
    """
    return "\n".join(
        f"{ctx.start_line + i:4d}| {line}" for i, line in enumerate(ctx.lines)
    )


# ---------------------------------------------------------------------------
# PythonPolicy
# ---------------------------------------------------------------------------

class PythonPolicy:
    """Python 场景的 LanguagePolicy 实现（纯参数装配，逻辑见模块级函数）."""

    language = "python"

    def __init__(
        self,
        *,
        profile: Any,
        cfg: Any,
        read_lines: Callable[[str], list[str]],
    ) -> None:
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

    def test_feature_density(self, path: str, size: int) -> float:
        return test_feature_density(
            path,
            size,
            read_lines=self._read_lines,
            keywords=self._cfg.improve_test_feature_keywords,
        )

    def extract_symbols(self, diff: str) -> list[str]:
        return extract_identifiers(diff)

    def render_source_block(self, ctx: FileContext, strategy: str, budget: int) -> str:
        return render_python_source_block(ctx, strategy, budget, cfg=self._cfg)

    def normalize_suggestion(self, sug: Suggestion, ctx: FileContext) -> Suggestion:
        return normalise_suggestion(sug)

    def validate_suggestion(
        self,
        sug: Suggestion,
        ctx: FileContext,
        *,
        rule_set: Any,
        file_sources: dict[str, list[str]],
        line_map: dict[str, set[int]],
    ) -> RiskVerdict:
        # 薄派发：通用校验顺序在 review/validation.py（Task 11），语言差异（AST/apply risk）
        # 经 policy 钩子；模块实现不得回调 policy.validate_suggestion，避免递归。
        from reviewagent.review.validation import validate_suggestion as _validate

        return _validate(
            sug,
            ctx=ctx,
            policy=self,
            rule_set=rule_set,
            cfg=self._cfg,
            find_line=find_line_by_existing_code,
            file_sources=file_sources,
            line_map=line_map,
        )

    def score_suggestion(self, sug: Suggestion) -> int:
        # profile 的 repo_prefix 优先；profile 未声明时回退运行配置（默认 "SSD"）。
        prefix = self._profile.rule_keys.get("repo_prefix") or self._cfg.rule_key_prefix
        return score_suggestion(sug, rule_prefix=prefix)

    def keywords(self) -> set[str]:
        return set(PYTHON_KEYWORDS)

    def prompt_sections(self) -> dict[str, str]:
        return {}


def score_suggestion(sug: Suggestion, *, rule_prefix: str | None = None) -> int:
    """Python 场景评分（数表与旧 ImproveCommand._score_suggestion 逐字一致）.

    `rule_prefix` 由调用方在调用时解析（门面读 `config.rule_key_prefix`，
    PythonPolicy 读 profile/运行配置），不做 import-time 绑定.
    """
    return score_by_table(sug, rule_prefix=rule_prefix)
