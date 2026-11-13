"""部署单元契约：systemd units 与一键部署脚本的不变量.

为什么单独测：
  * unit 里的队列名 / Redis 地址曾经硬编码，与 `config/instance.env` 漂移后
    任务只入队、永不被消费（线上静默失效）。
  * `deploy.sh` 曾经的首次引导路径引用已删除的 `.env.example`，且成功路径因
    未初始化的数组在 `set -u` 下退出 1。
本文件把这两类回归钉成断言：unit 必须从 `EnvironmentFile` 取队列/Redis，
deploy.sh 必须以 `config/examples/instance.env.example` 引导并覆盖开机自启。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from reviewagent.settings.schema import SCHEMA_BY_NAME

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_DIR = REPO_ROOT / "deploy"

LONG_RUNNING_UNITS = (
    "reviewagent-webhook.service",
    "reviewagent-worker@.service",
    "reviewagent-weekly-worker.service",
)
TIMER_UNITS = (
    "reviewagent-weekly.timer",
    "reviewagent-reconciler.timer",
)
ONESHOT_UNITS = (
    "reviewagent-weekly-enqueue.service",
    "reviewagent-reconciler.service",
)
ALL_UNITS = LONG_RUNNING_UNITS + TIMER_UNITS + ONESHOT_UNITS


def _parse_unit(path: Path) -> dict[str, dict[str, list[str]]]:
    """解析 systemd unit（支持 `\\` 续行；注释整行忽略）→ {section: {key: [values]}}."""
    sections: dict[str, dict[str, list[str]]] = {}
    current: dict[str, list[str]] | None = None
    pending = ""

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not pending and (not line or line.startswith("#")):
            continue
        line = pending + line
        pending = ""
        if line.endswith("\\"):
            pending = line[:-1].strip() + " "
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1], {})
            continue
        if current is not None and "=" in line:
            key, _, value = line.partition("=")
            current.setdefault(key.strip(), []).append(value.strip())
    return sections


def _unit(name: str) -> dict[str, dict[str, list[str]]]:
    path = DEPLOY_DIR / name
    assert path.is_file(), f"missing deploy unit: {name}"
    return _parse_unit(path)


def _single(unit: dict[str, dict[str, list[str]]], section: str, key: str) -> str:
    values = unit.get(section, {}).get(key, [])
    assert values, f"[{section}] {key} 缺失"
    return values[0]


def _exec_start(unit: dict[str, dict[str, list[str]]]) -> str:
    return _single(unit, "Service", "ExecStart")


def _shell_command(unit: dict[str, dict[str, list[str]]]) -> str:
    """把 unit 里的 ExecStart 还原成 /bin/sh 实际收到的字符串."""
    return _exec_start(unit).replace("$$", "$")


def _run_exec_start(unit: dict[str, dict[str, list[str]]], env: dict[str, str]) -> list[str]:
    """把 rq 可执行文件换成 echo，在真实 /bin/sh 下验证队列名/URL 展开结果."""
    command = _shell_command(unit).replace(
        "/home/workflow/ReviewAgent/.venv/bin/rq", "echo"
    )
    result = subprocess.run(
        ["/bin/sh", "-c", command],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", **env},
        check=True,
    )
    return result.stdout.split()


def test_all_units_present():
    present = {path.name for path in DEPLOY_DIR.glob("reviewagent-*")}
    assert present == set(ALL_UNITS)


def test_legacy_single_worker_removed():
    assert not (DEPLOY_DIR / "reviewagent-worker.service").exists(), (
        "reviewagent-worker.service 已被 reviewagent-worker@.service 取代；"
        "两者同时启用会重复消费同一队列"
    )


@pytest.mark.parametrize("name", LONG_RUNNING_UNITS)
def test_long_running_units_autostart_and_reload(name: str):
    unit = _unit(name)
    assert "Unit" in unit and "Service" in unit and "Install" in unit
    assert _single(unit, "Service", "User") == "workflow"
    assert _single(unit, "Install", "WantedBy") == "multi-user.target"
    assert "network-online.target" in _single(unit, "Unit", "After")
    assert _single(unit, "Service", "Restart") == "always"
    assert _single(unit, "Service", "EnvironmentFile").endswith("config/instance.env")
    assert any(
        value == "REVIEWAGENT_DATA_DIR=/var/lib/reviewagent"
        for value in unit["Service"]["Environment"]
    ), "数据目录必须固定到 /var/lib/reviewagent（deploy.sh 会按实际路径改写）"
    assert _single(unit, "Service", "ExecStartPre").endswith("reviewagent config check")


@pytest.mark.parametrize("name", TIMER_UNITS)
def test_timers_boot_with_system(name: str):
    unit = _unit(name)
    assert _single(unit, "Install", "WantedBy") == "timers.target"
    assert _single(unit, "Timer", "Unit").endswith((".service",))


def test_reconciler_timer_runs_after_boot():
    timer = _unit("reviewagent-reconciler.timer")
    assert _single(timer, "Timer", "Unit") == "reviewagent-reconciler.service"
    assert "OnBootSec" in timer["Timer"], "开机后必须自动开始 reconcile"
    assert "OnUnitActiveSec" in timer["Timer"]


def test_weekly_timer_still_scheduled():
    timer = _unit("reviewagent-weekly.timer")
    assert _single(timer, "Timer", "Unit") == "reviewagent-weekly-enqueue.service"
    assert _single(timer, "Timer", "OnCalendar")
    assert _single(timer, "Timer", "Persistent") == "true"


@pytest.mark.parametrize("name", ONESHOT_UNITS)
def test_oneshot_units_are_timer_driven(name: str):
    unit = _unit(name)
    assert _single(unit, "Service", "Type") == "oneshot"
    assert "Install" not in unit, "oneshot 由 timer 触发，不应单独 enable"


def test_worker_queue_and_redis_are_env_driven():
    unit = _unit("reviewagent-worker@.service")
    command = _exec_start(unit)
    assert "$${RQ_QUEUE_NAME:-review}" in command
    assert "$${REDIS_URL:-" in command
    # 硬编码 "rq worker review" 会让 RQ_QUEUE_NAME 失效
    assert "rq worker review " not in command

    assert _run_exec_start(unit, {}) == [
        "worker", "review", "--url", "redis://127.0.0.1:6379/0",
    ]
    assert _run_exec_start(unit, {"RQ_QUEUE_NAME": "custom"})[1] == "custom"
    assert _run_exec_start(
        unit, {"REDIS_URL": "redis://127.0.0.1:6380/0"}
    )[-1] == "redis://127.0.0.1:6380/0"


def test_weekly_worker_queue_derives_from_main_queue():
    unit = _unit("reviewagent-weekly-worker.service")
    command = _exec_start(unit)
    assert "$${RQ_WEEKLY_QUEUE_NAME:-}" in command
    assert "$${RQ_QUEUE_NAME:-review}-weekly" in command

    assert _run_exec_start(unit, {})[1] == "review-weekly"
    assert _run_exec_start(unit, {"RQ_QUEUE_NAME": "foo"})[1] == "foo-weekly"
    assert _run_exec_start(
        unit, {"RQ_QUEUE_NAME": "foo", "RQ_WEEKLY_QUEUE_NAME": "bar"}
    )[1] == "bar"


@pytest.mark.parametrize(
    "name", ["reviewagent-worker@.service", "reviewagent-weekly-worker.service"]
)
def test_stop_timeout_covers_full_job(name: str):
    unit = _unit(name)
    stop_timeout = int(_single(unit, "Service", "TimeoutStopSec"))
    job_timeout = SCHEMA_BY_NAME["rq_worker_timeout"].default
    assert stop_timeout >= job_timeout, (
        "停止超时小于 RQ job_timeout 会在 job 执行中 SIGKILL worker，"
        "留下永远 running 的 review_runs"
    )


def test_reconciler_service_runs_existing_module():
    unit = _unit("reviewagent-reconciler.service")
    assert _exec_start(unit).endswith("-m reviewagent.reconciler.loop")
    assert (REPO_ROOT / "reviewagent" / "reconciler" / "loop.py").is_file()


@pytest.mark.parametrize("name", ALL_UNITS)
def test_exec_start_parses_in_posix_sh(name: str):
    service = _unit(name).get("Service")
    if not service or "ExecStart" not in service:
        pytest.skip("timer 没有 ExecStart")
    subprocess.run(["sh", "-n", "-c", _shell_command(_unit(name))], check=True)


@pytest.mark.parametrize(
    "script", ["deploy.sh", "deploy/preflight.sh", "deploy/verify.sh"]
)
def test_deploy_scripts_are_syntactically_valid(script: str):
    subprocess.run(["bash", "-n", str(REPO_ROOT / script)], check=True)


def test_deploy_sh_bootstraps_from_instance_env_template():
    text = (REPO_ROOT / "deploy.sh").read_text(encoding="utf-8")
    assert "config/examples/instance.env.example" in text
    assert "cp .env.example" not in text, "仓库里已没有 .env.example"
    assert "MISSING_DEPLOY_UNITS" not in text, (
        "旧实现用未初始化数组 + set -u，成功路径会退出 1"
    )
    assert "install -m 600" in text, "首次生成的实例配置必须是 600"


def test_deploy_sh_enables_autostart_topology():
    text = (REPO_ROOT / "deploy.sh").read_text(encoding="utf-8")
    assert "systemctl enable --now" in text
    assert "reviewagent-worker@${i}.service" in text
    assert "RQ_WORKER_COUNT" in text, "worker 实例数必须跟随 RQ_WORKER_COUNT"
    for name in ALL_UNITS:
        assert name in text, f"deploy.sh 未安装 {name}"
    assert "deploy/preflight.sh" in text
    assert "deploy/verify.sh" in text
    assert "systemctl disable --now reviewagent-worker.service" in text, (
        "升级时必须下线旧的单 worker unit，否则两个进程抢同一队列"
    )


def _exec_line_index(text: str, needle: str) -> int | None:
    """返回「真正执行」该脚本的行号（跳过注释行）；找不到返回 None."""
    for idx, line in enumerate(text.splitlines()):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if needle in stripped:
            return idx
    return None


def test_deploy_sh_materialises_qoder_agents_for_every_provider():
    """`.qoder/agents` 必须对所有 provider 无条件物化。

    历史缺陷：`deploy.sh` 只在 `LLM_PROVIDER=qodercli` 时同步 `.qoder/agents`，
    而生产推荐（也是模板默认）用的是 `qoder-sdk` —— 于是生产机的子代理注册表
    永远是空的（`.qoder/` 在 .gitignore 里，git pull 也带不过去），
    与本地 `scripts/restart_local.sh` 的无条件同步行为不一致。
    """
    text = (REPO_ROOT / "deploy.sh").read_text(encoding="utf-8")
    assert "scripts/sync_qoder_agents.py" in text, "deploy.sh 必须物化 .qoder/agents"
    assert '= "qodercli"' not in text, (
        "不能再按 LLM_PROVIDER 分支跳过 .qoder/agents 同步："
        "两种 provider 跑的是同一个 qodercli 二进制，ACP server 都要读这份注册表"
    )
    # 按「执行行」定位（注释里也可能提到脚本名）
    sync_at = _exec_line_index(text, "scripts/sync_qoder_agents.py")
    check_at = _exec_line_index(text, "scripts/ops/check_agent_drift.py")
    assert sync_at is not None, "deploy.sh 必须物化 .qoder/agents"
    assert check_at is not None, "deploy.sh 必须做 agent 漂移校验"
    assert sync_at < check_at, "必须先物化再校验"


def test_deploy_sh_fails_fast_on_agent_drift():
    """物化后必须按内容校验，漂移即中止部署。

    `sync_qoder_agents.py` 按 mtime 增量写：文件比源新但内容陈旧时会被跳过，
    于是注册表静默陈旧。漂移守卫（按内容比对）必须在同一脚本里 fail-fast。
    """
    text = (REPO_ROOT / "deploy.sh").read_text(encoding="utf-8")
    assert "scripts/ops/check_agent_drift.py" in text, "deploy.sh 必须做 agent 漂移校验"
    guard = text.split("scripts/ops/check_agent_drift.py", 1)[1]
    assert "exit 1" in guard, "漂移校验失败必须中止部署，而不是继续装 unit 起服务"


def test_verify_sh_checks_agent_drift():
    """部署后复检必须覆盖 agent 提示词漂移（与本地 restart_local.sh 对齐）。"""
    text = (REPO_ROOT / "deploy" / "verify.sh").read_text(encoding="utf-8")
    assert "scripts/ops/check_agent_drift.py" in text, (
        "verify.sh 必须复检 agent 提示词漂移"
    )


def test_preflight_agent_check_applies_to_all_providers():
    """`.qoder/agents` 体检不能只挂在 qodercli 分支下。"""
    text = (REPO_ROOT / "deploy" / "preflight.sh").read_text(encoding="utf-8")
    qodercli_at = text.index("qodercli)")
    branch_end = text.index(";;", qodercli_at)
    qodercli_branch = text[qodercli_at:branch_end]
    assert ".qoder/agents" not in qodercli_branch, (
        ".qoder/agents 检查不应只挂在 qodercli 分支下（qoder-sdk 同样需要）"
    )
    outside = text[:qodercli_at] + text[branch_end:]
    assert ".qoder/agents" in outside, "分支外应有 provider 无关的 .qoder/agents 体检"

