"""周报外发授权契约: WEEKLY_PUSH 环境变量 = 定时任务授权真发。

背景 (2026-11-16 生产排查发现):
    2026-11-12 起 runner 的真发条件收紧成 `push AND not dry_run`
    (见 tests/test_weekly_notify_gate.py —— 为了避免 11-11 手动跑一次脚本
     就把内部周报误推到团队钉钉群)。

    但 systemd 的 ExecStart 写死 `--week-offset -1 --enqueue` (**没有 --push**),
    而 scripts/run_weekly_report.sh 里的 WEEKLY_PUSH=true 开关只在**那条 shell
    路径**上生效 —— systemd 直接调了 weekly_report.py, 绕过了它。

    结果: 2026-11-12 ~ 11-16 **连续 6 周**的定时周报全部走 dry_run, 从没真发出去,
    而且 RQ 仍报 "Job OK" —— 静默失败, 没人发现。

修复:
    把 WEEKLY_PUSH 开关**下沉到 scripts/weekly_report.py** —— 由部署方
    (systemd Environment=WEEKLY_PUSH=true) 显式授权, 而不是把 --push 写死在命令行。
    安全语义不变: 手动跑 (无该 env) 仍然不外发。

本文件锁住三条契约:
    ① WEEKLY_PUSH=true → args.push 变成 True (定时任务会真发)
    ② 没有 WEEKLY_PUSH / =false → args.push 保持 False (手动跑不外发)
    ③ 显式 --push 仍然有效 (向后兼容)
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "weekly_report.py"


def _load_module():
    """按文件路径加载 scripts/weekly_report.py (它不是可导入的包模块)."""
    spec = importlib.util.spec_from_file_location("_weekly_report_cli", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def cli():
    return _load_module()


def _run_parse(cli, argv, env_push=None):
    """跑 parse_args, 返回 args.push。env_push=None 表示不设该 env。"""
    old_argv = sys.argv
    old_env = os.environ.pop("WEEKLY_PUSH", None)
    try:
        sys.argv = ["weekly_report.py", *argv]
        if env_push is not None:
            os.environ["WEEKLY_PUSH"] = env_push
        return cli.parse_args().push
    finally:
        sys.argv = old_argv
        os.environ.pop("WEEKLY_PUSH", None)
        if old_env is not None:
            os.environ["WEEKLY_PUSH"] = old_env


def test_weekly_push_env_enables_real_send(cli):
    """WEEKLY_PUSH=true → 启用真发 (定时任务模式)."""
    assert _run_parse(cli, ["--enqueue"], env_push="true") is True


def test_weekly_push_env_is_case_and_space_insensitive(cli):
    """大小写/空格容错 (systemd Environment 写法可能带空格)."""
    assert _run_parse(cli, ["--enqueue"], env_push="TRUE") is True
    assert _run_parse(cli, ["--enqueue"], env_push=" true ") is True


def test_no_weekly_push_env_keeps_manual_run_dry(cli):
    """⚠️ 安全契约: 没有 WEEKLY_PUSH → 手动跑仍然不外发 (11-11 误发的教训)."""
    assert _run_parse(cli, ["--enqueue"], env_push=None) is False


def test_weekly_push_env_false_keeps_manual_run_dry(cli):
    """WEEKLY_PUSH=false → 不外发."""
    assert _run_parse(cli, ["--enqueue"], env_push="false") is False


def test_explicit_push_flag_still_works(cli):
    """显式 --push 仍然有效 (向后兼容, 不依赖 env)."""
    assert _run_parse(cli, ["--push"], env_push=None) is True
    assert _run_parse(cli, ["--push"], env_push="false") is True
