"""规则 key -> 直观中文问题类别名的翻译.

周报里 suggestion 的 rule_keys 是 LLM 在 improvement 时自由引用的
(如 ``SSD-RULE-TYPEHINTS``、``R-LOOP``、``R-OTHER-IMPACT:caller_param``),
原始 key 对人很不直观。这里把它们翻译成一眼能看懂的中文问题类别,
供「本周检视汇总」润色使用 (避免出现 SSD-RULE-* / R-* 这类机器名).
"""
from __future__ import annotations

import re

# SSD-RULE-* 规范/风格类
_SSD_MAP: dict[str, str] = {
    "TYPEHINTS": "类型注解缺失",
    "DOCSTRING-REQUIRED": "缺少文档字符串",
    "NO-MUTABLE-DEFAULT": "可变默认参数",
    "NO-LOG-EXC": "异常未记录日志",
    "RESOURCE-CONTEXT-MANAGER": "资源未用 with 管理",
    "FORBIDDEN-WILDCARD-IMPORT": "通配符导入",
    "NAMING-CONVENTION": "命名不规范",
    "NO-TYPE-CHECK": "缺少类型校验",
    "NO-CONSTANTS": "魔法数字未抽常量",
    "NO-EARLY-RETURN": "嵌套过深(可早返回)",
    "DUPLICATE-CODE": "重复代码",
    "DEAD-CODE": "死代码",
}

# R-* 正确性 / 安全 / 资源类 (key 为去掉 R- 前缀的后缀)
_R_MAP: dict[str, str] = {
    "LOOP": "循环熔断缺失(无限循环风险)",
    "ERR": "错误处理不当",
    "RES": "资源句柄未释放(内存泄漏风险)",
    "SHELL": "Shell/命令注入风险",
    "OTHER": "其他代码问题",
}

# R-OTHER-IMPACT:<desc> 跨文件影响类, 映射已知后缀
_IMPACT_MAP: dict[str, str] = {
    "caller_param": "接口变更未同步调用方",
    "schema_drift": "Schema 漂移未同步",
    "import_path": "旧导入路径残留",
    "fixture_break": "测试 Fixture 失配",
}


def translate_rule_key(key: str) -> str:
    """把规则 key 翻译成直观中文问题类别名; 未知 key 做尽力可读化."""
    if not key:
        return "其他"
    k = key.strip()
    # R-OTHER-IMPACT:xxx
    if k.startswith("R-OTHER-IMPACT:"):
        suf = k.split(":", 1)[1].strip()
        if suf in _IMPACT_MAP:
            return _IMPACT_MAP[suf]
        return f"跨文件影响({_humanize(suf)})"
    # SSD-RULE-XXX
    if k.startswith("SSD-RULE-"):
        suf = k[len("SSD-RULE-"):].strip()
        if suf in _SSD_MAP:
            return _SSD_MAP[suf]
        return _humanize(suf)
    # R-XXX
    if k.startswith("R-"):
        suf = k[2:].strip()
        if suf in _R_MAP:
            return _R_MAP[suf]
        return _humanize(suf)
    # 其他前缀: 尽力可读化
    return _humanize(k)


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
