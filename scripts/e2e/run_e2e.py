"""统一端到端入口：`--tier fast` 配置生效性门禁，`--tier full` 真实链路验证.

用法：
    python scripts/e2e/run_e2e.py                    # 快速档（进程内，无外部依赖）
    python scripts/e2e/run_e2e.py --tier full        # 全量档（临时 GitLab 项目 + LLM + 队列）
    python scripts/e2e/run_e2e.py --tier full --only describe,improve --round 1
    python scripts/e2e/run_e2e.py --tier full --mr 181,180 --keep

锁策略：只清 cooldown / max_review_calls / diff_head；`reviewagent:lock:*` 永不删除。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.e2e import config_audit, fixtures, flows, real_config, report, stack  # noqa: E402

_ENV_CANDIDATES = ("config/instance.env", ".env")


def _load_env_file() -> None:
    """本地直接运行时补全实例配置，顺序同 `scripts/_env.sh`；已有 env 不覆盖."""
    candidates = [os.environ.get("REVIEWAGENT_ENV_FILE")]
    candidates.extend(str(ROOT / name) for name in _ENV_CANDIDATES)
    for raw in candidates:
        if not raw:
            continue
        path = Path(raw)
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.removeprefix("export ").strip()
            os.environ.setdefault(key, value.strip().strip("'\""))
        return


def _parse_features(raw: str | None) -> list[str]:
    if not raw or raw == "all":
        return list(flows.FLOWS)
    names = [name.strip() for name in raw.split(",") if name.strip()]
    unknown = sorted(set(names) - set(flows.FLOWS))
    if unknown:
        raise SystemExit(f"unknown features: {unknown}; available={sorted(flows.FLOWS)}")
    return names


def _preflight_ok(preflight: list[tuple[str, bool, str]]) -> bool:
    for name, ok, detail in preflight:
        print(f"  {'✅' if ok else '❌'} preflight {name}: {detail}")
    return all(ok for _, ok, _ in preflight)


def _run_fast(logdir: Path, stamp: str) -> int:
    """快速档：按当前 `REVIEW_PROFILE` 的生效值跑 witness，不依赖外部服务."""
    try:
        cfg = config_audit.scenario_config_snapshot()
    except Exception as exc:  # noqa: BLE001 - 场景包错误要在报告里可见
        result = report.FeatureResult(
            "config:scenario_snapshot", 1, 0, False, f"{type(exc).__name__}: {exc}", []
        )
        report.print_result(result)
        path = report.write_report(logdir, stamp, [result], tier="fast")
        print(f"\n=== DONE tier=fast: scenario config failed. report={path}")
        return 1

    audit = config_audit.run_fast(cfg)
    for result in audit.results:
        report.print_result(result)
    path = report.write_report(
        logdir, stamp, audit.results, tier="fast", config_rows=audit.rows
    )
    passed, total = report.summarize(audit.results)
    print(f"  config witness: {config_audit.write_config_summary(audit.rows)}")
    print(f"\n=== DONE tier=fast: {passed}/{total} witnesses passed. report={path}")
    return 0 if audit.ok else 1


def _config_observations(
    mr_iid: int, round_idx: int, cfg: object, pid: int
) -> list[report.FeatureResult]:
    """用该轮真实 suggestions 断言配置确实作用到了产物."""
    suggestions = config_audit.load_suggestions_from_telemetry(mr_iid, pid=pid)
    bodies = fixtures.mr_discussion_bodies(pid, mr_iid)
    published = "\n".join(bodies)
    for suggestion in suggestions:
        suggestion["rationale"] = published
    log = [
        f"[{stack.now_iso()}] observable config checks pid={pid} mr={mr_iid} "
        f"suggestions={len(suggestions)} diff_notes={len(bodies)}"
    ]
    results: list[report.FeatureResult] = []
    for name, ok, detail in config_audit.observable_checks(suggestions, cfg):
        result = report.FeatureResult(f"config:{name}", round_idx, mr_iid, ok, detail, log)
        report.print_result(result)
        results.append(result)
    return results


def _round_mrs(args: argparse.Namespace, stamp: str) -> tuple[list[int], list[fixtures.GitlabFixture]]:
    """`--mr` 优先；否则每轮建一个临时真实项目/MR."""
    if args.mr:
        mrs = [int(x) for x in args.mr.split(",") if x.strip()]
        while len(mrs) < args.round:
            mrs.append(mrs[-1])
        return mrs, []

    mrs: list[int] = []
    created: list[fixtures.GitlabFixture] = []
    for round_idx in range(1, args.round + 1):
        log: list[str] = []
        fixture = fixtures.create_review_fixture(round_idx, log, suffix=stamp[:13])
        created.append(fixture)
        mrs.append(fixture.mr_iid)
        print(f"  fixture round={round_idx} project={fixture.project_id} mr=!{fixture.mr_iid}")
    return mrs, created


def _run_full(args: argparse.Namespace, logdir: Path, stamp: str) -> int:
    preflight = stack.probe()
    if not _preflight_ok(preflight):
        path = report.write_report(logdir, stamp, [], tier="full", preflight=preflight)
        print(f"\n=== DONE tier=full: preflight failed. report={path}")
        return 1

    audit = config_audit.run_fast()
    if not audit.ok:
        for result in audit.results:
            if not result.passed:
                report.print_result(result)
        path = report.write_report(
            logdir, stamp, audit.results, tier="full", config_rows=audit.rows, preflight=preflight
        )
        print(f"\n=== DONE tier=full: config witness failed. report={path}")
        return 1

    try:
        effective_cfg = config_audit.scenario_config_snapshot()
    except Exception as exc:  # noqa: BLE001 - 场景包错误要在报告里可见
        result = report.FeatureResult(
            "config:scenario_snapshot", 1, 0, False, f"{type(exc).__name__}: {exc}", []
        )
        report.print_result(result)
        path = report.write_report(
            logdir, stamp, [result], tier="full", config_rows=audit.rows, preflight=preflight
        )
        print(f"\n=== DONE tier=full: scenario config failed. report={path}")
        return 1

    features = _parse_features(args.only)
    try:
        mrs, created = _round_mrs(args, stamp)
    except Exception as exc:  # noqa: BLE001 - 夹具失败要落报告
        print(f"  ❌ fixture setup failed: {exc}")
        path = report.write_report(logdir, stamp, [], tier="full", preflight=preflight)
        print(f"\n=== DONE tier=full: fixture failed. report={path}")
        return 1

    original_pid = os.environ.get("REVIEWAGENT_WEEKLY_TARGET_PROJECT_ID")
    results: list[report.FeatureResult] = []
    print(f"=== E2E full stamp={stamp} features={features} rounds={args.round} mrs={mrs}")
    try:
        for round_idx in range(1, args.round + 1):
            mr_iid = mrs[round_idx - 1]
            fixture = next((f for f in created if f.mr_iid == mr_iid), None)
            if fixture is not None:
                os.environ["REVIEWAGENT_WEEKLY_TARGET_PROJECT_ID"] = str(fixture.project_id)
            for feature in features:
                log: list[str] = [
                    f"=== feature={feature} round={round_idx} stamp={stamp} mr={mr_iid}"
                ]
                try:
                    result = flows.FLOWS[feature](mr_iid, round_idx, log)
                except Exception as exc:  # noqa: BLE001 - 单 feature 失败不影响其余
                    log.append(f"[{stack.now_iso()}] EXC: {type(exc).__name__}: {exc}")
                    result = report.FeatureResult(
                        feature, round_idx, mr_iid, False, f"exc: {exc}", log
                    )
                report.print_result(result)
                results.append(result)
            pid = fixture.project_id if fixture is not None else stack.project_id()
            if {"improve", "auto_chain"} & set(features):
                results.extend(_config_observations(mr_iid, round_idx, effective_cfg, pid))
            if fixture is not None:
                real_log = [
                    f"=== config_real round={round_idx} stamp={stamp} "
                    f"mr={mr_iid} project={fixture.project_id}"
                ]
                extra = real_config.run(
                    mr_iid, round_idx, fixture.project_id, effective_cfg, real_log
                )
                for result in extra:
                    report.print_result(result)
                results.extend(extra)
    finally:
        if original_pid is None:
            os.environ.pop("REVIEWAGENT_WEEKLY_TARGET_PROJECT_ID", None)
        else:
            os.environ["REVIEWAGENT_WEEKLY_TARGET_PROJECT_ID"] = original_pid

        if created and not args.keep:
            for fixture in created:
                try:
                    fixtures.delete_project(fixture.project_id, [])
                except Exception as exc:  # noqa: BLE001 - 清理失败只告警
                    print(f"  ⚠️ cleanup project {fixture.project_id} failed: {exc}")
        elif created:
            print(f"  --keep: 保留临时项目 {[f.project_id for f in created]}")

    passed, total = report.summarize(results)
    path = report.write_report(
        logdir,
        stamp,
        results,
        tier="full",
        config_rows=audit.rows,
        preflight=preflight,
        extra={
            "fixtures": [
                {
                    "project_id": f.project_id,
                    "source_branch": f.source_branch,
                    "mr_iid": f.mr_iid,
                    "kept": args.keep,
                }
                for f in created
            ]
        },
    )
    print(f"\n=== DONE tier=full: {passed}/{total} features passed. report={path}")
    return 0 if passed == total else 1


def main(argv: list[str] | None = None, *, default_tier: str = "fast") -> int:
    _load_env_file()
    parser = argparse.ArgumentParser(description="ReviewAgent 统一端到端验证")
    parser.add_argument("--tier", choices=("fast", "full"), default=default_tier)
    parser.add_argument("--only", "--feature", dest="only", default="all",
                        help="逗号分隔的 feature 列表（默认 all）")
    parser.add_argument("--round", type=int, default=None, help="轮数（fast 固定 1）")
    parser.add_argument("--mr", default="", help="复用现有 MR（comma-separated），跳过夹具创建")
    parser.add_argument("--keep", action="store_true", help="保留临时 GitLab 项目")
    parser.add_argument("--logdir", type=Path, default=report.default_logdir())
    args = parser.parse_args(argv)

    stamp = report.stamp_now()
    args.logdir.mkdir(parents=True, exist_ok=True)
    if args.tier == "fast":
        return _run_fast(args.logdir, stamp)

    if args.round is None:
        args.round = 1
    if args.round < 1:
        raise SystemExit("--round must be >= 1")
    return _run_full(args, args.logdir, stamp)


if __name__ == "__main__":
    sys.exit(main())
