"""`reviewagent` 命令行入口：`config check/show` / `rules check` / `profile list/switch`.

设计约束：

* `config check` 面向 systemd `ExecStartPre`：fail-fast、单行可读错误到 stderr、
  退出码 2；成功时输出 profile / name / fingerprint / data_dir。
* `config show` 供人工核对生效配置：`key: value`（稳定按字段名排序），
  `--sources` 追加 `# source=env|config|profile|default`，`--diff-default`
  只显示与 schema 默认不同的字段；secret 一律 `***`，URL 内嵌密码与
  query 凭证（`access_token` 等）同样脱敏。
* `rules check` 面向规则作者：逐条打印 `key\tseverity\ttitle`，末尾输出
  `ruleset_fingerprint=` / `render_checksum=`；装配期校验失败同样收敛成
  一行 stderr + 退出码 2（校验逻辑只在 `assemble_base` 里，不在 CLI 复制）。
* `profile list` / `profile switch` 面向换场景：list 只读不解析 env；
  switch 自动 source 实例 env 文件后校验目标场景包，再原位改写
  `REVIEW_PROFILE` / `REVIEWAGENT_DATA_DIR`（原子写、保留注释与顺序）。
* 模块顶层只 import `settings.schema`（纯数据）。`reviewagent.config` 在 import
  时就会解析 env 并可能抛 "missing required env var"，因此配置/profile相关
  import 全部放在函数内，缺 env 时也能被收敛成一行错误 + 退出码 2。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path
from typing import Any

from reviewagent.settings.schema import SCHEMA, FieldSpec

_EXIT_CHECK_FAILED = 2
_ENV_SOURCE = "env"

# query 里按凭证处理的键名（`access_token=tok` → `access_token=***`）。
_QUERY_SECRET_KEYS = frozenset({"access_token", "token", "secret", "sign", "key", "password"})
_URL_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")
_URL_USERINFO_RE = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)(?P<user>[^/\s:@]*):(?P<password>[^/\s@]*)@"
)
_QUERY_PAIR_RE = re.compile(r"(?P<sep>[?&])(?P<key>[^=&#]+)=(?P<value>[^&#]*)")


def _resolve_config() -> Any:
    """按当前进程 env 重新解析配置（含必填项/类型/取值范围校验）.

    与启动时同一个入口（`settings.binder.from_env`），因此 `config check`
    能拦住 `OPENCODE_TIMEOUT=abc` 这类启动期才炸的非法值。
    """
    from reviewagent.settings.binder import from_env

    return from_env()


def _load(args: argparse.Namespace, bind: bool = True) -> tuple[Any, Any]:
    """解析 env → 加载 profile（含场景字段应用）→ 按需绑定 data_dir.

    全程使用本次解析出的 `cfg`（而不是 import 期的 `current()`），这样
    `REVIEWAGENT_DATA_DIR` / `REVIEW_PROFILE` 等实例字段与本次命令行一致。
    `bind=False` 时只读：`config show` 不得写 `instance.json`，否则探查别的
    profile 会把 data_dir 永久绑定、随后挡住真正的服务启动。
    任何一步失败都以异常上抛，由子命令转成一行错误。
    """
    from reviewagent.scenario.profile import bind_instance, load_profile

    cfg = _resolve_config()
    # `args.profile` 缺省时取本次 env 解析出的 REVIEW_PROFILE；空串交给
    # load_profile 处理（回退 DEFAULT_PROFILE 并打 WARNING）。
    name = args.profile if args.profile is not None else cfg.review_profile
    profile = load_profile(name, root=Path(cfg.review_profile_root))
    if bind:
        bind_instance(profile, Path(cfg.data_dir))
    return cfg, profile


def _effective_value(spec: FieldSpec, cfg: Any, profile: Any) -> Any:
    """取该字段实际生效的值.

    profile 以 `config` 来源写入的字段，值在 `config.current()` 上
    （load_profile 已经 `apply_scenario`）；其余字段（含被 env 覆盖的场景字段、
    以及全部 instance 字段）取本次 env 解析结果，避免读到 import 期的陈旧值。
    """
    if profile.sources.get(spec.name) in ("config", "profile"):
        from reviewagent.config import current

        return getattr(current(), spec.name)
    return getattr(cfg, spec.name)


def _source(spec: FieldSpec, profile: Any, explicit: frozenset[str]) -> str:
    """`env` / `config` / `profile` / `default` — profile.sources 已按 env 优先级判定."""
    if spec.name in profile.sources:
        return profile.sources[spec.name]
    # binder.from_env() 只记录 canonical env 名（spec.env or spec.name）。
    if spec.env and spec.env in explicit:
        return _ENV_SOURCE
    return "default"


def _format_value(value: Any) -> str:
    """把 tuple/Path 摊平成与 env 写法一致的字符串，便于 diff 与人工核对."""
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return ",".join(str(item) for item in value)
    return str(value)


def _mask_url_query(value: str) -> str:
    """对 URL query 里的凭证键脱敏（只动 query，保留原始分隔符/顺序）."""

    def _replace(match: re.Match[str]) -> str:
        if match.group("key").lower() in _QUERY_SECRET_KEYS and match.group("value"):
            return f"{match.group('sep')}{match.group('key')}=***"
        return match.group(0)

    head, sep, tail = value.partition("?")
    if not sep:
        return value
    query, hash_sep, fragment = tail.partition("#")
    # 正则要求键名前有 `?`/`&`；query 首对没有前缀，临时补一个再剥掉。
    masked_query = _QUERY_PAIR_RE.sub(_replace, "?" + query)[1:]
    return f"{head}?{masked_query}{hash_sep}{fragment}"


def _mask_display_value(spec: FieldSpec, value: Any) -> Any:
    """`config show` 展示值脱敏：secret 整值替换，URL 内嵌凭据/query 凭证遮盖.

    * `spec.secret` → `***`（与旧行为一致）；
    * 其它字符串：`scheme://...` 时把 userinfo 里的密码改成 `user:***@host`；
      query 中 `access_token` / `token` / `secret` / `sign` / `key` /
      `password` 的值一律改成 `***`（不要求带 scheme —— 少写 `https://`
      的 webhook 地址同样不得泄露凭证）；没有敏感成分时原样返回；
    * 非字符串原样返回（由 `_format_value` 负责渲染）。
    """
    if spec.secret:
        return "***"
    if not isinstance(value, str):
        return value
    masked = value
    if _URL_SCHEME_RE.match(value):
        masked = _URL_USERINFO_RE.sub(
            lambda match: f"{match.group('scheme')}{match.group('user')}:***@", value
        )
    return _mask_url_query(masked)


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
        cfg, profile = _load(args, bind=False)
    except Exception as exc:  # noqa: BLE001 — 同上，show 也不吐 traceback
        print(f"config show failed: {exc}", file=sys.stderr)
        return _EXIT_CHECK_FAILED
    from reviewagent.settings.binder import explicit_env

    explicit = explicit_env()
    for spec in sorted(SCHEMA, key=lambda item: item.name):
        value = _effective_value(spec, cfg, profile)
        if args.diff_default and value == spec.default:
            continue
        line = f"{spec.name}: {_format_value(_mask_display_value(spec, value))}"
        if args.sources:
            line += f"  # source={_source(spec, profile, explicit)}"
        print(line)
    return 0


def _cmd_rules_check(args: argparse.Namespace) -> int:
    """打印装配后的规则清单 + 指纹/正文校验和；装配失败退 2.

    只读：不绑定 instance（`bind=False`），避免探查别的 profile 时写
    `instance.json`。profile 解析与 `config check` 一致（缺省取 `REVIEW_PROFILE`）。
    """
    try:
        from reviewagent.scenario.rules.resolver import assemble_base

        _, profile = _load(args, bind=False)
        rule_set = assemble_base(profile)
    except Exception as exc:  # noqa: BLE001 — 同上，装配期校验错误收敛成一行
        print(f"rules check failed: {exc}", file=sys.stderr)
        return _EXIT_CHECK_FAILED
    for rule in rule_set.rules:
        print(f"{rule.key}\t{rule.severity}\t{rule.title}")
    print(f"ruleset_fingerprint={rule_set.fingerprint}")
    print(f"render_checksum={hashlib.sha256(rule_set.verbatim_body.encode()).hexdigest()}")
    return 0


# ---------------- profile list / profile switch ----------------


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _profile_root() -> Path:
    """场景包根：`REVIEW_PROFILE_ROOT` 显式指定，否则仓库 `profiles/`."""
    override = os.environ.get("REVIEW_PROFILE_ROOT", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    from reviewagent.settings.schema import profiles_root

    return Path(profiles_root())


def _resolve_env_file() -> Path:
    """与 scripts/_env.sh 同序：REVIEWAGENT_ENV_FILE → config/instance.env → .env."""
    explicit = os.environ.get("REVIEWAGENT_ENV_FILE", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    for candidate in (_repo_root() / "config" / "instance.env", _repo_root() / ".env"):
        if candidate.is_file():
            return candidate
    return _repo_root() / "config" / "instance.env"


def _source_env_file(path: Path) -> None:
    """把 env 文件灌进进程环境（文件值优先，与 `set -a; source` 一致）."""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value


def _rewrite_env_file(path: Path, updates: dict[str, str]) -> None:
    """原位改写 KEY=VALUE（保留注释/顺序/权限；缺的键追加到末尾，原子替换）."""
    lines = path.read_text(encoding="utf-8").splitlines()
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            key = stripped.partition("=")[0].strip()
            if key in updates:
                seen.add(key)
                out.append(f"{key}={updates[key]}")
                continue
        out.append(line)
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}")
    mode = path.stat().st_mode & 0o777
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _switch_data_dir(name: str, current: str) -> str:
    """目标数据目录：相对沿用 `data/<name>`；绝对目录取同级兄弟目录."""
    current = current.strip()
    if current and Path(current).is_absolute():
        return str(Path(current).parent / name)
    return f"data/{name}"


def _restart_hint() -> str:
    if sys.platform == "darwin":
        return "bash scripts/restart_local.sh"
    return (
        "sudo systemctl restart 'reviewagent-worker@*' reviewagent-weekly-worker && "
        "sudo systemctl restart reviewagent-webhook  # 先 worker 后 API"
    )


def _cmd_profile_list(_args: argparse.Namespace) -> int:
    env_file = _resolve_env_file()
    saved_env = dict(os.environ)
    try:
        # 先 source 再 import：reviewagent.config 在 import 期解析必填 env，
        # bare shell（没 source 过 env 文件）里顺序反了会直接崩。
        if env_file.is_file():
            _source_env_file(env_file)
        from reviewagent.scenario.profile import DEFAULT_PROFILE, available_profiles

        current = os.environ.get("REVIEW_PROFILE", "").strip() or DEFAULT_PROFILE
        entries = available_profiles(_profile_root())
    except Exception as exc:  # noqa: BLE001 — 一行错误 + 退出码 2
        print(f"profile list failed: {exc}", file=sys.stderr)
        return _EXIT_CHECK_FAILED
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
    if not entries:
        print(f"no profiles found under {_profile_root()}", file=sys.stderr)
        return _EXIT_CHECK_FAILED
    for entry in entries:
        marker = "*" if entry.split(" ")[0] == current else " "
        print(f"{marker} {entry}")
    print(f"(current: {current}; switch with: reviewagent profile switch <name>)")
    return 0


def _cmd_profile_switch(args: argparse.Namespace) -> int:
    try:
        env_file = _resolve_env_file()
        if not env_file.is_file():
            raise ValueError(f"instance env file not found: {env_file}")
        saved_env = dict(os.environ)
        try:
            # bare shell 下没有 source 过 env：先灌入再 import/校验目标场景包
            # （import 顺序同理：config 在 import 期解析必填 env）。
            _source_env_file(env_file)
            from reviewagent.scenario import profile as profile_module

            profile = profile_module.load_profile(args.name, root=_profile_root())
            current_dir = os.environ.get("REVIEWAGENT_DATA_DIR", "").strip()
            if os.environ.get("REVIEW_PROFILE", "").strip() == args.name:
                # 切到当前场景 = 幂等操作：数据目录保持原值
                # （可能未设置 → schema 默认 ./data），不得被推导改写。
                new_dir = current_dir
            else:
                new_dir = _switch_data_dir(args.name, current_dir)
            target = Path(new_dir)
            if not target.is_absolute():
                target = _repo_root() / target
            bound = profile_module.bound_profile(target)
            if bound is not None and bound != profile.name:
                raise ValueError(
                    f"data dir {new_dir} is bound to profile {bound!r}; "
                    "pick a different directory"
                )
        finally:
            os.environ.clear()
            os.environ.update(saved_env)
        updates = {"REVIEW_PROFILE": args.name}
        if new_dir:
            updates["REVIEWAGENT_DATA_DIR"] = new_dir
        _rewrite_env_file(env_file, updates)
    except Exception as exc:  # noqa: BLE001 — 同 config check：一行错误 + 退出码 2
        print(f"profile switch failed: {exc}", file=sys.stderr)
        return _EXIT_CHECK_FAILED
    print(f"profile: {args.name}")
    print(f"data_dir: {new_dir or '(schema 默认 ./data)'}")
    print(f"env_file: {env_file}")
    print(f"restart: {_restart_hint()}")
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
        "--sources", action="store_true", help="标注来源 env|config|profile|default"
    )
    show.add_argument(
        "--diff-default", action="store_true", help="只显示与 schema 默认值不同的字段"
    )
    show.set_defaults(func=_cmd_config_show)

    rules_parser = sub.add_parser("rules", help="规则装配校验与展示")
    rules_actions = rules_parser.add_subparsers(dest="action", required=True)
    rules_check = rules_actions.add_parser(
        "check",
        help="装配规则包并打印 key/severity/title + 指纹（装配失败 → 退出码 2）",
    )
    rules_check.add_argument("--profile", default=None, help="场景包名；默认取 REVIEW_PROFILE")
    rules_check.set_defaults(func=_cmd_rules_check)

    profile_parser = sub.add_parser("profile", help="场景包查看与切换")
    profile_actions = profile_parser.add_subparsers(dest="action", required=True)
    profile_list = profile_actions.add_parser(
        "list", help="列出可用场景包（* 标记当前 REVIEW_PROFILE）"
    )
    profile_list.set_defaults(func=_cmd_profile_list)
    profile_switch = profile_actions.add_parser(
        "switch",
        help="切换场景包：校验后改写 instance.env 的 REVIEW_PROFILE/REVIEWAGENT_DATA_DIR",
    )
    profile_switch.add_argument("name", help="目标场景包名（profile list 可查）")
    profile_switch.set_defaults(func=_cmd_profile_switch)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
