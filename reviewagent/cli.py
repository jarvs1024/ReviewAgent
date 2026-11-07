"""`reviewagent` 命令行入口（Task 3）：`config check` / `config show`.

设计约束：

* `config check` 面向 systemd `ExecStartPre`：fail-fast、单行可读错误到 stderr、
  退出码 2；成功时输出 profile / name / fingerprint / data_dir。
* `config show` 供人工核对生效配置：`key: value`（稳定按字段名排序），
  `--sources` 追加 `# source=env|profile|preset|default`，`--diff-default`
  只显示与 schema 默认不同的字段；secret 一律 `***`。
* 模块顶层只 import `settings.schema`（纯数据）。`reviewagent.config` 在 import
  时就会解析 env 并可能抛 "missing required env var"，因此配置/profile 相关
  import 全部放在函数内，缺 env 时也能被收敛成一行错误 + 退出码 2。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from reviewagent.settings.schema import SCHEMA, FieldSpec

_EXIT_CHECK_FAILED = 2
_ENV_SOURCE = "env"


def _resolve_config() -> Any:
    """按当前进程 env 重新解析配置（含必填项/类型/取值范围校验）.

    与启动时同一个入口（`settings.binder.from_env`），因此 `config check`
    能拦住 `OPENCODE_TIMEOUT=abc` 这类启动期才炸的非法值。
    """
    from reviewagent.settings.binder import from_env

    return from_env()


def _load(args: argparse.Namespace) -> tuple[Any, Any]:
    """解析 env → 加载 profile（含场景字段应用）→ 绑定 data_dir.

    全程使用本次解析出的 `cfg`（而不是 import 期的 `current()`），这样
    `REVIEWAGENT_DATA_DIR` / `REVIEW_PROFILE` 等实例字段与本次命令行一致。
    任何一步失败都以异常上抛，由子命令转成一行错误。
    """
    from reviewagent.scenario.profile import bind_instance, load_profile

    cfg = _resolve_config()
    # `args.profile` 缺省时取本次 env 解析出的 REVIEW_PROFILE；空串交给
    # load_profile 处理（回退 DEFAULT_PROFILE 并打 WARNING）。
    name = args.profile if args.profile is not None else cfg.review_profile
    profile = load_profile(name, root=Path(cfg.review_profile_root))
    bind_instance(profile, Path(cfg.data_dir))
    return cfg, profile


def _effective_value(spec: FieldSpec, cfg: Any, profile: Any) -> Any:
    """取该字段实际生效的值.

    profile 以 `profile`/`preset` 来源写入的字段，值在 `config.current()` 上
    （load_profile 已经 `apply_scenario`）；其余字段（含被 env 覆盖的场景字段、
    以及全部 instance 字段）取本次 env 解析结果，避免读到 import 期的陈旧值。
    """
    if profile.sources.get(spec.name) in ("profile", "preset"):
        from reviewagent.config import current

        return getattr(current(), spec.name)
    return getattr(cfg, spec.name)


def _source(spec: FieldSpec, profile: Any, explicit: frozenset[str]) -> str:
    """`env` / `profile` / `preset` / `default` — profile.sources 已按 env 优先级判定."""
    if spec.name in profile.sources:
        return profile.sources[spec.name]
    names = ((spec.env,) if spec.env else ()) + tuple(spec.aliases)
    if any(name in explicit for name in names):
        return _ENV_SOURCE
    return "default"


def _format_value(value: Any) -> str:
    """把 tuple/Path 摊平成与 env 写法一致的字符串，便于 diff 与人工核对."""
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return ",".join(str(item) for item in value)
    return str(value)


def _cmd_config_check(args: argparse.Namespace) -> int:
    try:
        cfg, profile = _load(args)
    except Exception as exc:  # noqa: BLE001 — 唯一目的是把失败收敛成一行 + 退出码 2
        print(f"config check failed: {exc}", file=sys.stderr)
        return _EXIT_CHECK_FAILED
    print("ok")
    print(f"profile: {profile.root.name}")
    print(f"name: {profile.name}")
    print(f"fingerprint: {profile.fingerprint}")
    print(f"data_dir: {Path(cfg.data_dir)}")
    return 0


def _cmd_config_show(args: argparse.Namespace) -> int:
    try:
        cfg, profile = _load(args)
    except Exception as exc:  # noqa: BLE001 — 同上，show 也不吐 traceback
        print(f"config show failed: {exc}", file=sys.stderr)
        return _EXIT_CHECK_FAILED
    from reviewagent.settings.binder import explicit_env

    explicit = explicit_env()
    for spec in sorted(SCHEMA, key=lambda item: item.name):
        value = _effective_value(spec, cfg, profile)
        if args.diff_default and value == spec.default:
            continue
        line = f"{spec.name}: {'***' if spec.secret else _format_value(value)}"
        if args.sources:
            line += f"  # source={_source(spec, profile, explicit)}"
        print(line)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="reviewagent", description="ReviewAgent 运维命令")
    sub = parser.add_subparsers(dest="cmd", required=True)
    config_parser = sub.add_parser("config", help="配置校验与展示")
    actions = config_parser.add_subparsers(dest="action", required=True)

    check = actions.add_parser(
        "check",
        help="启动前校验（缺必填 env / profile 非法 / instance.json 冲突 → 退出码 2）",
    )
    check.add_argument("--profile", default=None, help="场景包名；默认取 REVIEW_PROFILE")
    check.set_defaults(func=_cmd_config_check)

    show = actions.add_parser("show", help="打印生效配置")
    show.add_argument("--profile", default=None, help="场景包名；默认取 REVIEW_PROFILE")
    show.add_argument(
        "--sources", action="store_true", help="标注来源 env|profile|preset|default"
    )
    show.add_argument(
        "--diff-default", action="store_true", help="只显示与 schema 默认值不同的字段"
    )
    show.set_defaults(func=_cmd_config_show)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
