"""配置生效性审计：witness 目录 → 静态引用校验 + 快速档 delta + 全量档可观测断言."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.e2e import catalog, quick_witnesses
from scripts.e2e.report import REPO_ROOT, ConfigRow, FeatureResult

_EVIDENCE_RE = re.compile(r"^([\w./-]+\.(?:py|sh|md|ya?ml|toml|env))(?::([A-Za-z0-9_]+))?")

SEVERITY_LEVELS = {"low": 1, "medium": 2, "high": 3, "critical": 4}


@dataclass
class FastAudit:
    rows: list[ConfigRow]
    results: list[FeatureResult]

    @property
    def ok(self) -> bool:
        return all(row.ok for row in self.rows) and all(r.passed for r in self.results)


def evidence_ref(evidence: str) -> tuple[str, str] | None:
    """从 evidence 开头解析 `(相对路径, 行号或符号)`；无路径返回 None."""
    match = _EVIDENCE_RE.match(evidence.strip())
    if not match:
        return None
    return match.group(1), match.group(2) or ""


def _resolve_evidence_path(rel: str) -> Path | None:
    """目录里的证据路径可能相对仓库根，也可能相对 `reviewagent/`（模块内写法）."""
    for candidate in (REPO_ROOT / rel, REPO_ROOT / "reviewagent" / rel):
        if candidate.is_file():
            return candidate
    return None


def _evidence_problem(evidence: str) -> str:
    ref = evidence_ref(evidence)
    if ref is None:
        return "证据缺少 file[:line|symbol] 引用"
    rel, anchor = ref
    path = _resolve_evidence_path(rel)
    if path is None:
        return f"证据文件不存在: {rel}"
    if anchor and not anchor.isdigit():
        source = path.read_text(encoding="utf-8", errors="ignore")
        if anchor not in source:
            return f"证据符号已失效: {rel}:{anchor}"
    return ""


def static_rows() -> list[ConfigRow]:
    """schema 全覆盖 + 每条 live/script 证据引用都可解析."""
    rows: list[ConfigRow] = []
    for field_name in sorted(catalog.CATALOG):
        witness = catalog.CATALOG[field_name]
        problem = _evidence_problem(witness.evidence)
        rows.append(
            ConfigRow(
                field=field_name,
                kind=witness.kind,
                status="missing" if problem else "declared",
                evidence=witness.evidence,
                detail=problem,
            )
        )
    return rows


def run_fast(cfg: Any = None) -> FastAudit:
    """快速档：静态目录校验 + 18 个行为 witness（进程内 delta）."""
    if cfg is None:
        from reviewagent.config import current

        cfg = current()

    by_field = {row.field: row for row in static_rows()}
    results: list[FeatureResult] = []
    for check in sorted(quick_witnesses.QUICK):
        try:
            evidence = quick_witnesses.QUICK[check](cfg)
            ok, detail = True, evidence
        except AssertionError as exc:
            ok, detail = False, str(exc) or "assertion failed"
        except Exception as exc:  # noqa: BLE001 - 审计要汇总所有失败
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        results.append(FeatureResult(f"config:{check}", 1, 0, ok, detail, []))
        if ok:
            for field_name, witness in catalog.CATALOG.items():
                if witness.kind == "behavior" and witness.check == check:
                    by_field[field_name] = ConfigRow(
                        field=field_name,
                        kind="behavior",
                        status="verified",
                        evidence=witness.evidence,
                        detail=evidence,
                    )
    return FastAudit(rows=[by_field[name] for name in sorted(by_field)], results=results)


def observable_checks(suggestions: list[dict], cfg: Any) -> list[tuple[str, bool, str]]:
    """全量档：用真实 suggestions 验证「配置确实作用到了产物」（可单测）."""
    checks: list[tuple[str, bool, str]] = []

    limit = cfg.improve_max_suggestions
    if limit > 0:
        checks.append(
            ("suggestions_within_limit", len(suggestions) <= limit, f"{len(suggestions)} <= {limit}")
        )

    min_severity = cfg.improve_min_severity
    if min_severity in SEVERITY_LEVELS:
        floor = SEVERITY_LEVELS[min_severity]
        below = [
            s.get("file")
            for s in suggestions
            if SEVERITY_LEVELS.get(str(s.get("severity") or "medium").lower(), 2) < floor
        ]
        checks.append(("no_below_min_severity", not below, f"min={min_severity} below={below[:3]}"))

    conftest = [s.get("file") for s in suggestions if str(s.get("file", "")).endswith("conftest.py")]
    checks.append(("conftest_not_reviewed", not conftest, f"conftest suggestions={conftest[:3]}"))

    prefix = cfg.rule_key_prefix
    if prefix:
        hits = [
            s.get("file")
            for s in suggestions
            if f"{prefix}-RULE-" in str(s.get("rationale") or "")
        ]
        checks.append(("rule_key_hits", bool(hits), f"{prefix}-RULE hits={hits[:3]}"))

    return checks


def write_config_summary(rows: list[ConfigRow]) -> str:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1
    return ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))


def load_suggestions_from_telemetry(mr_iid: int, *, pid: int | None = None) -> list[dict]:
    """全量档取该 MR 已发布的 suggestions（状态 open）."""
    from reviewagent.telemetry.store import get_store
    from scripts.e2e.stack import project_id as _project_id

    target = pid if pid is not None else _project_id()
    store = get_store()
    with store._conn() as conn:
        rows = conn.execute(
            "SELECT file_path, target_line, severity, rationale FROM suggestions "
            "WHERE project_id=? AND mr_iid=? AND state='open'",
            (target, mr_iid),
        ).fetchall()
    return [
        {
            "file": row["file_path"],
            "line": row["target_line"],
            "severity": row["severity"],
            "rationale": row["rationale"],
        }
        for row in rows
    ]
