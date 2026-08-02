"""规则 key -> 直观问题类别名的翻译.

周报里 suggestion 的 rule_keys 是 LLM 在 improvement 时自由引用的
(如 ``SSD-RULE-TYPEHINTS``、``R-LOOP``、``R-OTHER-IMPACT:caller_param``),
原始 key 对人很不直观。这里把它们翻译成一眼能看懂的问题类别名。

设计: 不硬编码任何规则字典, 跟随规则定义动态解析 ——

- **SSD-RULE-* (自定义/仓库规则)**: 动态解析被扫描仓库的 ``.agents/rules`` 目录
  (路径来自 ``config.repo_context_rules_dir``, 前缀来自 ``config.rule_key_prefix``),
  优先取 ``index.md`` 的 One-line summary 列作友好描述; 取不到则对后缀做尽力可读化。
  规则集随仓库变化自动发现, 新增自定义规则不会再露出原始机器 key。

- **R-* / R-OTHER-IMPACT:* / R-OTHER:* (通用 / 跨文件规则)**: 这些规则定义在
  提示词模板里 (``_general_rules_block.md`` / ``improve.md``), 这里直接解析对应
  模板的表格/列表得到中文名, 不在代码里重复维护一份字典。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from reviewagent.config import config

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


# --------------------------------------------------------------------------
# 通用规则 (R-*) + 跨文件 (R-OTHER-IMPACT:*) + 兜底 (R-OTHER:*)
# 全部从提示词模板动态解析 (避免与 prompt 定义重复维护)
# --------------------------------------------------------------------------
_GENERIC_MAP_CACHE: dict[str, str] | None = None


def _load_generic_map() -> dict[str, str]:
    """解析 _general_rules_block.md (R-*) 与 improve.md (R-OTHER-IMPACT:/R-OTHER:) 得到中文名映射."""
    global _GENERIC_MAP_CACHE
    if _GENERIC_MAP_CACHE is not None:
        return _GENERIC_MAP_CACHE
    m: dict[str, str] = {}

    # 1) R-XXX: _general_rules_block.md 表格 `| `R-RES` | 资源句柄 | ... |`
    gpath = _PROMPTS_DIR / "_general_rules_block.md"
    if gpath.exists():
        text = gpath.read_text(encoding="utf-8")
        for mm in re.finditer(r"`(R-[A-Z]+)`\s*\|\s*([^|\n]+?)\s*\|", text):
            m.setdefault(mm.group(1), mm.group(2).strip())

    # 2) R-OTHER-IMPACT:xxx / R-OTHER:xxx: improve.md 列表
    #    `R-OTHER-IMPACT:caller_param` — caller 没传新参数
    ipath = _PROMPTS_DIR / "improve.md"
    if ipath.exists():
        text = ipath.read_text(encoding="utf-8")
        for pat in (
            r"`(R-OTHER-IMPACT:[a-z_]+)`\s*[—\-]\s*([^\n,]+)",
            r"`(R-OTHER:[a-z_]+)`\s*[—\-]\s*([^\n,]+)",
        ):
            for mm in re.finditer(pat, text):
                m.setdefault(mm.group(1), mm.group(2).strip())

    _GENERIC_MAP_CACHE = m
    return m


# --------------------------------------------------------------------------
# SSD-RULE-* (自定义规则) —— 动态解析被扫描仓库 .agents/rules 目录
# --------------------------------------------------------------------------
def _load_ssd_map(project_id: int) -> dict[str, str]:
    """从被扫描仓库的 .agents/rules 动态解析规则键 -> 友好描述.

    优先读 index.md 的 One-line summary 列; 取不到则用规则文件名后缀做可读化兜底。
    规则集随仓库变化自动发现, 无需在代码里登记。
    """
    from reviewagent.gitlab.client import client as gitlab_client
    from reviewagent.logging_setup import logger
    from reviewagent.repo_context import fetch_rule_files

    prefix = config.rule_key_prefix or "SSD"
    result: dict[str, str] = {}
    try:
        files = fetch_rule_files(gitlab_client, project_id)
    except Exception as e:
        logger.warning("rule_translate.load_ssd_map failed project={}: {}", project_id, e)
        return result
    if not files:
        return result

    # 优先: index.md 的 One-line summary 列
    index_text = ""
    for path, content in files.items():
        if path.rstrip("/").endswith("index.md"):
            index_text = content
            break
    if index_text:
        pat = re.compile(
            rf"`({re.escape(prefix)}-RULE-[A-Z0-9-]+)`\s*\|[^\n|]*\|[^\n|]*\|\s*([^\n|]+?)\s*\|"
        )
        for mm in pat.finditer(index_text):
            key, summary = mm.group(1), mm.group(2).strip().rstrip(".")
            if key not in result and summary:
                result[key] = summary

    # 兜底: 规则文件名后缀做可读化 (覆盖 index.md 未列出的规则文件)
    for path in files:
        stem = Path(path).stem
        mm = re.match(rf"{re.escape(prefix)}-RULE-(.+)", stem)
        if mm and stem not in result:
            result[stem] = _humanize(mm.group(1))
    return result


# --------------------------------------------------------------------------
# 公开 API
# --------------------------------------------------------------------------
class RuleNameResolver:
    """规则 key -> 可读类别名 的解析器 (组合: 仓库 SSD 动态表 + 提示词模板通用表)."""

    def __init__(
        self,
        ssd_map: dict[str, str] | None = None,
        generic_map: dict[str, str] | None = None,
    ):
        self.ssd_map = ssd_map or {}
        self.generic_map = generic_map if generic_map is not None else _load_generic_map()

    @classmethod
    def from_repo(cls, project_id: int | None) -> "RuleNameResolver":
        """为某个被扫描仓库构建解析器; project_id 为空时只加载通用模板表."""
        generic = _load_generic_map()
        ssd: dict[str, str] = {}
        if project_id:
            try:
                ssd = _load_ssd_map(int(project_id))
            except Exception:
                ssd = {}
        return cls(ssd_map=ssd, generic_map=generic)

    def translate(self, key: str) -> str:
        """把规则 key 翻译成直观类别名."""
        if not key:
            return "其他"
        k = key.strip()
        prefix = config.rule_key_prefix or "SSD"

        # 跨文件影响
        if k.startswith("R-OTHER-IMPACT:"):
            if k in self.generic_map:
                return self.generic_map[k]
            suf = k.split(":", 1)[1].strip()
            return f"跨文件影响({_humanize(suf)})"
        # 自定义规则 SSD-RULE-*
        if k.startswith(f"{prefix}-RULE-"):
            if k in self.ssd_map:
                return self.ssd_map[k]
            suf = k.split("-RULE-", 1)[1].strip()
            return _humanize(suf)
        # 通用规则 R-XXX / R-OTHER:*
        if k.startswith("R-"):
            if k in self.generic_map:
                return self.generic_map[k]
            suf = k.split(":", 1)[1].strip() if ":" in k else k[2:].strip()
            return _humanize(suf)
        # 其他前缀: 尽力可读化
        return _humanize(k)


def translate_rule_key(key: str) -> str:
    """纯函数兜底 (无仓库上下文时): 只做尽力可读化, 不依赖模板/规则目录."""
    return _humanize(key)


def _humanize(token: str) -> str:
    """把 MY_RULE_NAME 这类 token 尽力变成可读中文/英文混合标签."""
    parts = re.split(r"[-_]", token)
    word_map = {
        "no": "缺少", "not": "未", "missing": "缺失", "required": "必填",
        "default": "默认", "mutable": "可变", "log": "日志", "exc": "异常",
        "exception": "异常", "res": "资源", "resource": "资源", "shell": "命令",
        "sql": "SQL", "inject": "注入", "injection": "注入", "loop": "循环",
        "err": "错误", "error": "错误", "type": "类型", "hint": "注解",
        "doc": "文档", "docstring": "文档字符串", "import": "导入",
        "wildcard": "通配符", "naming": "命名", "leak": "泄漏", "memory": "内存",
        "security": "安全", "unsafe": "不安全", "race": "竞态", "thread": "线程",
        "lock": "锁", "null": "空值", "none": "空值", "param": "参数",
        "schema": "结构", "drift": "漂移", "fixture": "测试夹具", "test": "测试",
        "caller": "调用方", "auth": "鉴权", "timeout": "超时", "retry": "重试",
        "bare": "裸", "print": "打印", "comment": "注释", "const": "常量",
        "magic": "魔法数", "typo": "拼写", "dead": "死", "duplicate": "重复",
        "stale": "过期", "unused": "未用", "state": "状态", "data": "数据",
        "clean": "清理", "repro": "可复现", "skip": "跳过", "ci": "CI",
        "dep": "依赖", "perf": "性能", "assert": "断言", "time": "时序",
        "lock": "锁", "mem": "内存", "fix": "夹具",
    }
    out: list[str] = []
    for p in parts:
        pl = p.lower()
        if pl in word_map:
            out.append(word_map[pl])
        elif p:
            out.append(p)
    text = "".join(out)
    return text if text else token
