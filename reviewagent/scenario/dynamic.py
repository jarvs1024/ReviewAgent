"""动态规则（context-deep）模型与 `dynamic_rules.yaml` 加载器.

动态规则不进入静态 RuleSet：它们需要检视期的请求级证据（跨文件引用、调用链
等），由 `review/dynamic/**` 的 provider 采集，失败只降级不阻塞 MR。

分层边界（spec §3.3）：本模块属 scenario 数据层，**不得** import
`reviewagent.review.*`；`review/dynamic/model.py` 反向 re-export 这里的模型，
provider 注册与执行骨架在 `review/dynamic/`。

加载期硬失败（配置错误必须启动即暴露）：越出 profile 目录、不支持的
`schema_version`、未知顶层/规则字段、非法或重复的 `DYN-*` 键、以及声明的
模板文件缺失或越出 profile 目录。模板字段 `template` / `empty_template` /
`items_template` 均只做路径规范化并校验存在性。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

_KEY_RE = re.compile(r"^DYN-[A-Z0-9-]+$")
_SCHEMA_VERSION = 1
_ALLOWED_TOP = frozenset({"schema_version", "rules"})
_ALLOWED_RULE = frozenset({
    "key", "title", "provider", "enabled", "template", "empty_template",
    "items_template", "params", "limits",
})
_TEMPLATE_FIELDS = ("template", "empty_template", "items_template")


@dataclass(frozen=True)
class DynamicRule:
    """一条动态规则声明（`DYN-*` 管理键，只用于配置 / telemetry / 归属）."""

    key: str
    title: str
    provider: str
    enabled: bool = True
    template: str | None = None
    empty_template: str | None = None
    items_template: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    limits: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DynamicEvidence:
    """一条动态规则本次请求的证据；`degraded` 时 refs 恒为空元组."""

    rule_key: str
    provider: str
    refs: tuple[dict[str, Any], ...] = ()
    degraded: bool = False
    reason: str = ""


@dataclass(frozen=True)
class DynamicRuleSet:
    """某 profile 装配出的不可变动态规则集合."""

    rules: tuple[DynamicRule, ...]
    fingerprint: str

    def with_rule(self, rule: DynamicRule) -> DynamicRuleSet:
        """替换同键规则并推进指纹（不新增规则；未知键时集合不变）."""
        merged = tuple(rule if r.key == rule.key else r for r in self.rules)
        return replace(self, rules=merged, fingerprint=f"{self.fingerprint}:{rule.key}")


def _read_mapping(path: Path) -> tuple[dict[str, Any], bytes]:
    """读取并解析 dynamic_rules（原始字节一并返回，供内容指纹使用）."""
    raw = path.read_bytes()
    data = yaml.safe_load(raw.decode("utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != _SCHEMA_VERSION:
        raise ValueError(f"unsupported dynamic_rules schema in {path}")
    return data, raw


def _template_path(raw: Any, field_name: str, root: Path) -> str | None:
    """规范化模板路径并校验：必须存在且位于 profile 根目录内."""
    if raw is None:
        return None
    rel = str(raw)
    path = (root / rel).resolve()
    if root not in path.parents:
        raise ValueError(
            f"dynamic rule field {field_name} escapes profile root: {rel}"
        )
    if not path.is_file():
        raise ValueError(
            f"dynamic rule field {field_name} references missing template: {rel}"
        )
    return rel


def _parse_rule(raw: Any, seen: set[str], root: Path) -> DynamicRule:
    if not isinstance(raw, dict):
        raise ValueError(f"dynamic rule must be a mapping: {raw!r}")
    unknown = set(raw) - _ALLOWED_RULE
    if unknown:
        raise ValueError(f"unknown dynamic rule fields {sorted(unknown)}")
    key = raw.get("key", "")
    if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
        raise ValueError(f"invalid dynamic rule key: {key!r}")
    if key in seen:
        raise ValueError(f"duplicate dynamic rule key: {key}")
    seen.add(key)
    provider = raw.get("provider")
    if not provider:
        raise ValueError(f"dynamic rule missing provider: {key}")
    templates = {
        name: _template_path(raw.get(name), name, root) for name in _TEMPLATE_FIELDS
    }
    return DynamicRule(
        key=key,
        title=str(raw.get("title") or key),
        provider=str(provider),
        enabled=bool(raw.get("enabled", True)),
        template=templates["template"],
        empty_template=templates["empty_template"],
        items_template=templates["items_template"],
        params=dict(raw.get("params") or {}),
        limits=dict(raw.get("limits") or {}),
    )


def load_dynamic_rules(profile) -> DynamicRuleSet:
    """按 profile 加载动态规则；未声明时返回空集合（指纹为空字符串）."""
    rel = profile.dynamic_rules
    if not rel:
        return DynamicRuleSet(rules=(), fingerprint="")
    root = Path(profile.root).resolve()
    path = (root / str(rel)).resolve()
    if root not in path.parents:
        raise ValueError(f"dynamic_rules escapes profile root: {rel}")
    data, raw = _read_mapping(path)
    unknown = set(data) - _ALLOWED_TOP
    if unknown:
        raise ValueError(f"unknown dynamic_rules fields {sorted(unknown)}")
    raw_rules = data.get("rules") or []
    if not isinstance(raw_rules, list):
        raise ValueError(f"dynamic_rules field rules must be a list: {path}")
    seen: set[str] = set()
    rules = tuple(_parse_rule(item, seen, root) for item in raw_rules)
    # 内容指纹（不是 mtime）：相同内容 → 相同指纹，与文件修改时间无关.
    fingerprint = hashlib.sha256(raw).hexdigest()[:12]
    return DynamicRuleSet(rules=rules, fingerprint=fingerprint)
