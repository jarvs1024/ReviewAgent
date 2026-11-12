"""配置生效性审计：witness 目录 → 静态引用校验 + 快速档 delta + 全量档可观测断言."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.e2e import catalog, quick_witnesses
from scripts.e2e.report import REPO_ROOT, ConfigRow, FeatureResult

_EVIDENCE_RE = re.compile(r"^([\w./-]+\.(?:py|sh|md|ya?ml|toml|env))(?::([A-Za-z0-9_]+))?")
_RULE_KEY_RE = re.compile(r"\b[A-Z][A-Z0-9]*(?:-[A-Z0-9_]+)+\b")

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
    if anchor and anchor.isdigit():
        # 数字锚点至少要落在**非空行**上 —— 行号越界/指向空行说明代码已漂移.
        # Why 不是万能防线: 行号漂到另一段**非空**代码仍发现不了,
        # 所以 catalog 里的关键条目一律用符号锚点（上面那条会真校验）。
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        try:
            idx = int(anchor) - 1
        except ValueError:
            return f"证据行号非法: {rel}:{anchor}"
        if idx < 0 or idx >= len(lines):
            return f"证据行号越界: {rel}:{anchor} (文件共 {len(lines)} 行)"
        if not lines[idx].strip():
            return f"证据行号指向空行: {rel}:{anchor}"
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
    """快速档：静态目录校验 + 21 组行为检查（覆盖 34 条 behavior witness，进程内 delta）."""
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


def scenario_config_snapshot() -> Any:
    """加载当前 `REVIEW_PROFILE` 的场景覆盖并返回快照，随后恢复进程内配置.

    全量档用它判断「worker 实际生效的值」；不改变本进程后续行为。
    """
    from reviewagent import config as config_mod
    from reviewagent.scenario.profile import load_profile

    previous = config_mod._current
    try:
        load_profile()
        return config_mod.current()
    finally:
        config_mod._current = previous


def _paths_skipped(files: list[Any], skip_paths: tuple[str, ...]) -> bool:
    """这些文件是否命中了 skip_paths（目录前缀 / 路径片段匹配）."""
    if not skip_paths:
        return False
    for path in files:
        norm = str(path or "").replace("\\", "/")
        for pattern in skip_paths:
            pat = str(pattern).replace("\\", "/")
            if not pat:
                continue
            if pat.endswith("/"):
                if pat.rstrip("/") in norm.split("/")[:-1]:
                    return True
            elif pat in norm:
                return True
    return False


def observable_checks(suggestions: list[dict], cfg: Any) -> list[tuple[str, bool, str]]:
    """全量档：用真实 suggestions 验证「配置确实作用到了产物」（可单测）."""
    checks: list[tuple[str, bool, str]] = [
        ("suggestions_present", bool(suggestions), f"{len(suggestions)} suggestions published")
    ]

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

    # conftest.py: 是否参与检视由**配置**决定，不写死「一定不检视」。
    # Why: 场景包策略已改为「conftest.py 不跳过 —— fixture 是测试业务的真实契约面，
    # 需要参与检视」（config/.../review.yaml 的 file_filter 注释 + priority_paths 含
    # /conftest.py），而 improve_skip_test_paths 也从「文件名黑名单」改成了「目录黑名单」。
    # 旧断言把「conftest 一律不检视」写死，跟随策略变更后就成了假阳性。
    # 现在按 skip_paths 判定：被策略跳过的文件不该有建议；没被跳过的应当有建议。
    conftest = [s.get("file") for s in suggestions if str(s.get("file", "")).endswith("conftest.py")]
    skip_paths = tuple(cfg.improve_skip_test_paths or ())
    # 逐文件判定：只有「命中 skip_paths 却仍出了建议」才算违规；
    # 未命中 skip_paths 的 conftest 有建议是正常结果（策略允许检视）。
    violations = [f for f in conftest if _paths_skipped([f], skip_paths)]
    if violations:
        checks.append(
            ("conftest_skip_policy_respected", False,
             f"conftest 命中 skip_paths 却仍有建议: {violations[:3]}；skip_paths={list(skip_paths)[:5]}")
        )
    else:
        checks.append(
            ("conftest_skip_policy_respected", True,
             f"conftest 未命中 skip_paths → 允许参与检视；实际 conftest 建议={conftest[:3]}")
        )

    cited = [
        s.get("file")
        for s in suggestions
        if _RULE_KEY_RE.search(str(s.get("rationale") or ""))
    ]
    checks.append(
        ("rule_key_cited", bool(cited), f"{len(cited)}/{len(suggestions)} cite rule keys")
    )

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
            "SELECT file_path, target_line, severity FROM suggestions "
            "WHERE project_id=? AND mr_iid=? AND state='open'",
            (target, mr_iid),
        ).fetchall()
    return [
        {
            "file": row["file_path"],
            "line": row["target_line"],
            "severity": row["severity"],
            "rationale": "",
        }
        for row in rows
    ]
