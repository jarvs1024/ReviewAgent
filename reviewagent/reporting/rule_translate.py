"""规则 key -> 直观问题类别名的翻译.

周报里 suggestion 的 rule_keys 是 LLM 在 improvement 时自由引用的
(如 ``SSD-RULE-TYPEHINTS``、``R-LOOP``、``R-OTHER-IMPACT:caller_param``),
原始 key 对人很不直观。这里把它们翻译成一眼能看懂的问题类别名。

设计: 不硬编码任何规则字典, 跟随规则定义动态解析 ——

- **SSD-RULE-* (自定义/仓库规则)**: 动态解析被扫描仓库里发现的规则文件
  (与检视链路同一套“配置规则目录 → 递归扫描”发现器, 不绑定目录/前缀/条数),
  优先取规则文件的表格摘要作友好描述; 取不到则对后缀做尽力可读化。
  规则集随仓库变化自动发现, 新增自定义规则不会再露出原始机器 key。

- **R-* / R-OTHER-IMPACT:* / R-OTHER:* (通用 / 跨文件规则)**: 这些规则定义在
  规则包 (``rules/general_rules.md``) 与提示词模板 (``improve.md``) 里, 这里直接
  解析对应的表格/列表得到中文名, 不在代码里重复维护一份字典。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# 任意大写前缀的仓库规则键（`SSD-RULE-X` / `ACME-RULE-X` 等），不绑定具体仓库。
_REPO_KEY_RE = re.compile(r"^(?P<prefix>[A-Z][A-Z0-9]*)[-_]RULE[-_](?P<suffix>.+)$")

# --------------------------------------------------------------------------
# 通用规则 (R-*) + 跨文件 (R-OTHER-IMPACT:*) + 兜底 (R-OTHER:*)
# 全部从已装配规则集 + profile improve.md 动态解析 (避免与规则定义重复维护)
# --------------------------------------------------------------------------
_GENERIC_MAP_CACHE: dict[tuple[str, str], dict[str, str]] = {}


def _profile_texts(profile: Any) -> list[str]:
    """profile 里所有可能定义 R-OTHER 键的文本（规则包 + prompt + blocks）.

    规则包是规则键的权威来源；prompt（尤其 improve.md / blocks/dynamic.md）
    补充 `R-OTHER-IMPACT:<key>` 这类跨文件兜底键。全部按 profile 根目录解析，
    不假设固定文件名或目录.
    """
    root = Path(getattr(profile, "root", Path(".")))
    rels: list[str] = [str(rel) for rel in (getattr(profile, "rules", ()) or ())]
    prompts = getattr(profile, "prompts", None) or {}
    rels.extend(str(rel) for rel in prompts.values())
    blocks = root / "prompts" / "blocks"
    if blocks.is_dir():
        rels.extend(path.relative_to(root).as_posix() for path in sorted(blocks.glob("*.md")))
    texts: list[str] = []
    for rel in rels:
        path = root / rel
        if path.is_file():
            try:
                texts.append(path.read_text(encoding="utf-8"))
            except OSError:
                continue
    return texts


def _load_generic_map() -> dict[str, str]:
    """解析规则集 (R-*) 与 profile 规则包/prompt (R-OTHER-IMPACT:/R-OTHER:) 得到中文名映射.

    缓存按 `RuleSet.fingerprint`（附带 profile 指纹，规则包 / prompt 变更同样失效）.
    """
    from reviewagent.scenario.profile import active_or_load
    from reviewagent.scenario.rules.resolver import active_rule_set

    profile = active_or_load()
    rule_set = active_rule_set()
    cache_key = (rule_set.fingerprint, profile.fingerprint)
    cached = _GENERIC_MAP_CACHE.get(cache_key)
    if cached is not None:
        return cached
    m: dict[str, str] = {}

    # 1) R-XXX: 规则集 index_by_key 的 title
    for key, rule in rule_set.index_by_key.items():
        if key.startswith("R-"):
            m.setdefault(key, rule.title)

    # 2) R-OTHER-IMPACT:xxx / R-OTHER:xxx: 规则包与 prompt 里的定义列表
    #    `R-OTHER:magic_number` — 硬编码魔法数（端口 / 超时 / 重试等）
    pat = re.compile(r"`(R-OTHER(?:-IMPACT)?:[a-z0-9_]+)`\s*[—\-–]\s*([^\n（(]+)")
    for text in _profile_texts(profile):
        for mm in pat.finditer(text):
            label = mm.group(2).strip().rstrip(".,;；。，")
            if label:
                m.setdefault(mm.group(1), label)

    # 3) 历史/幻觉规则兜底: LLM 偶尔会在 rule_keys 里塞不存在的键 (e.g.
    #    `R-XXX` 当占位符). 这种键如果不显式映射就会被 `_humanize` strip
    #    成 "XXX" 落到周报里很难看. 这里集中拦截所有 R-* 但不在前两步
    #    命中的键, 给一个有意义的"未分类"标签, 让翻阅周报的人知道是
    #    数据残留 / LLM 幻觉, 而不是新规则.
    # 显式兜底: 历史 telemetry.db 里发现过 `R-XXX` 大量残留, 直接给一条
    # "未分类 - 需人工核实" 标签, 比 strip 成 "XXX" 强.
    m.setdefault("R-XXX", "未分类违规 - 需人工核实规则归属")
    # 历史数据迁移: 78 条 R-XXX 已经 SQL UPDATE 成 R-OTHER:unmapped,
    # 这里同步给 R-OTHER:unmapped 一条翻译, 否则回落到 _humanize
    # 会输出 "ROTHERunmapped" 难看的字面. 同时给任何 R-OTHER:xxx 兜底.
    m.setdefault("R-OTHER:unmapped", "未明确归类 - 需人工核实规则归属")

    _GENERIC_MAP_CACHE[cache_key] = m
    return m


# --------------------------------------------------------------------------
# <PREFIX>-RULE-* (仓库自定义规则) —— 动态解析被扫描仓库发现的规则文件
# --------------------------------------------------------------------------
def _load_ssd_map(project_id: int) -> dict[str, str]:
    """从被扫描仓库发现的规则文件解析规则键 -> 友好描述.

    优先读 index.md 的 One-line summary 列; 取不到则用规则文件名后缀做可读化兜底。
    规则集随仓库变化自动发现（目录/前缀/条数均不假设），无需在代码里登记。
    """
    from reviewagent.gitlab.client import client as gitlab_client
    from reviewagent.logging_setup import logger
    from reviewagent.repo_context import fetch_rule_files

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
            r"`([A-Z][A-Z0-9]*-RULE-[A-Z0-9-]+)`\s*\|[^\n|]*\|[^\n|]*\|\s*([^\n|]+?)\s*\|"
        )
        for mm in pat.finditer(index_text):
            key, summary = mm.group(1), mm.group(2).strip().rstrip(".")
            if key not in result and summary:
                result[key] = summary

    # 兜底: 规则文件名后缀做可读化 (覆盖 index.md 未列出的规则文件)
    for path in files:
        stem = Path(path).stem
        mm = _REPO_KEY_RE.match(stem)
        if mm and stem not in result:
            result[stem] = _humanize(mm.group("suffix"))
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

        # 跨文件影响
        if k.startswith("R-OTHER-IMPACT:"):
            if k in self.generic_map:
                return self.generic_map[k]
            suf = k.split(":", 1)[1].strip()
            return f"跨文件影响({_humanize(suf)})"
        # 仓库自定义规则 <任意前缀>-RULE-*
        repo_match = _REPO_KEY_RE.match(k)
        if repo_match:
            if k in self.ssd_map:
                return self.ssd_map[k]
            return _humanize(repo_match.group("suffix").strip())
        # 通用规则 R-XXX / R-OTHER:*
        if k.startswith("R-"):
            if k in self.generic_map:
                return self.generic_map[k]
            suf = k.split(":", 1)[1].strip() if ":" in k else k[2:].strip()
            return _humanize(suf)
        # 其他前缀: 尽力可读化
        return _humanize(k)


def translate_rule_key(
    key: str,
    *,
    generic_map: dict[str, str] | None = None,
    ssd_map: dict[str, str] | None = None,
) -> str:
    """翻译规则键 -> 可读类别名.

    可选参数允许显式注入 generic_map / ssd_map; 缺省时回退到全局
    `_load_generic_map()` (从 .prompts/*.md 解析) + 不读 ssd_map
    (纯函数无仓库上下文).
    """
    if not key:
        return "其他"
    k = key.strip()
    gmap = generic_map if generic_map is not None else _load_generic_map()
    smap = ssd_map or {}

    # 1) 跨文件影响: R-OTHER-IMPACT:*
    if k.startswith("R-OTHER-IMPACT:"):
        if k in gmap:
            return gmap[k]
        suf = k.split(":", 1)[1].strip()
        return f"跨文件影响({_humanize(suf)})"
    # 2) 仓库自定义规则: 任意大写前缀 + -RULE-
    repo_match = _REPO_KEY_RE.match(k)
    if repo_match:
        if k in smap:
            return smap[k]
        return _humanize(repo_match.group("suffix").strip())
    # 3) 通用规则 R-* (含 R-XXX / R-OTHER:* / 标准 R-FOO)
    if k.startswith("R-"):
        if k in gmap:
            return gmap[k]
        if ":" in k:
            suf = k.split(":", 1)[1].strip()
            return _humanize(suf)
        # R-XXX / 标准 R-FOO 但未在 generic_map
        # 标准 R-FOO 走 _humanize fallback; 显式 generic_map 已在前面拦截
        return _humanize(k[2:].strip())  # strip 'R-' prefix
    return _humanize(k)


def _humanize(token: str) -> str:
    """把 MY_RULE_NAME 这类 token 尽力变成可读中文/英文混合标签."""
    composite = {
        "magic_number": "硬编码魔法数",
        "dead_code": "死代码",
        "naming_inconsistency": "命名不一致",
        "duplicated_definition": "重复定义",
        "stale_comment": "过期注释",
        "unused_param": "未使用参数",
        "caller_param": "调用方参数未同步",
        "schema_drift": "结构漂移",
        "import_path": "导入路径",
        "fixture_break": "夹具失配",
    }
    if token in composite:
        return composite[token]
    parts = re.split(r"[-_]", token)
    word_map = {
        "no": "缺少", "not": "未", "missing": "缺失", "required": "必填",
        "default": "默认", "mutable": "可变", "log": "日志", "exc": "异常",
        "exception": "异常", "res": "资源", "resource": "资源", "shell": "命令",
        "sql": "SQL", "inject": "注入", "injection": "注入", "loop": "循环",
        "err": "错误", "error": "错误", "type": "类型", "hint": "注解",
        "typehints": "类型注解", "doc": "文档", "docstring": "文档字符串", "import": "导入",
        "wildcard": "通配符", "naming": "命名", "leak": "泄漏", "memory": "内存",
        "security": "安全", "unsafe": "不安全", "race": "竞态", "thread": "线程",
        "lock": "锁", "null": "空值", "none": "空值", "param": "参数",
        "schema": "结构", "drift": "漂移", "fixture": "测试夹具", "test": "测试",
        "caller": "调用方", "auth": "鉴权", "timeout": "超时", "retry": "重试",
        "bare": "裸", "print": "打印", "comment": "注释", "const": "常量",
        "magic": "魔法数", "typo": "拼写", "dead": "死", "duplicate": "重复",
        "duplicated": "重复", "inconsistency": "不一致", "number": "数字",
        "code": "代码", "stale": "过期", "unused": "未用", "state": "状态",
        "data": "数据", "clean": "清理", "repro": "可复现", "skip": "跳过",
        "ci": "CI", "dep": "依赖", "perf": "性能", "assert": "断言",
        "time": "时序", "mem": "内存", "fix": "夹具",
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
