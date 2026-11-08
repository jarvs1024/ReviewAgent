#!/usr/bin/env bash
# ReviewAgent 一键部署（Linux + systemd）— 幂等，可反复执行用于升级。
#
# 用法:
#   sudo bash deploy.sh               # 全量部署 / 升级 + 开机自启 + 部署后验证
#   sudo bash deploy.sh --check       # 只做前置检查（不修改系统）
#   sudo bash deploy.sh --no-verify   # 部署但不跑 deploy/verify.sh
#
# 环境变量:
#   PROJECT_DIR           代码目录          (默认 /home/workflow/ReviewAgent)
#   REVIEWAGENT_DATA_DIR  运行数据目录      (默认 /var/lib/reviewagent)
#   SERVICE_USER          服务账号          (默认 workflow)
#   WEBHOOK_PORT          webhook 端口      (默认 3000)
#
# 首次执行会从 config/examples/instance.env.example 生成 config/instance.env
# （600 权限）后退出；填入 GITLAB_URL / PAT / WEBHOOK_SECRET 后重新执行即可。
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/home/workflow/ReviewAgent}"
DATA_DIR="${REVIEWAGENT_DATA_DIR:-/var/lib/reviewagent}"
ENV_FILE="${PROJECT_DIR}/config/instance.env"
ENV_TEMPLATE="${PROJECT_DIR}/config/examples/instance.env.example"
SERVICE_USER="${SERVICE_USER:-workflow}"
WEBHOOK_PORT="${WEBHOOK_PORT:-3000}"
VENV="${PROJECT_DIR}/.venv"
WORKTREE_DIR="/tmp/reviewagent-worktrees"

RUN_PREFLIGHT_ONLY=0
RUN_VERIFY=1
for arg in "$@"; do
    case "${arg}" in
        --check) RUN_PREFLIGHT_ONLY=1 ;;
        --no-verify) RUN_VERIFY=0 ;;
        -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
        *) echo "未知参数: ${arg}（支持 --check / --no-verify）" >&2; exit 2 ;;
    esac
done

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    SUDO="sudo"
fi

run_as_service() {
    if [ "$(id -u)" -eq 0 ] && command -v runuser >/dev/null 2>&1; then
        runuser -u "${SERVICE_USER}" -- "$@"
    else
        "$@"
    fi
}

# 以服务账号执行、并按 systemd 的方式把 EnvironmentFile 导出为进程 env。
run_with_env() {
    local runner=()
    if [ "$(id -u)" -eq 0 ] && command -v runuser >/dev/null 2>&1; then
        runner=(runuser -u "${SERVICE_USER}" --)
    fi
    "${runner[@]+"${runner[@]}"}" bash -c 'cd "$1" || exit 1; set -a; . "$2"; set +a; shift 2; exec "$@"' \
        _ "${PROJECT_DIR}" "${ENV_FILE}" "$@"
}

env_value() {
    local key="$1"
    [ -f "${ENV_FILE}" ] || return 0
    sed -n "s/^[[:space:]]*${key}=//p" "${ENV_FILE}" | tail -n 1 \
        | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/" | tr -d '\r'
}

run_preflight() {
    PROJECT_DIR="${PROJECT_DIR}" \
    REVIEWAGENT_DATA_DIR="${DATA_DIR}" \
    ENV_FILE="${ENV_FILE}" \
    SERVICE_USER="${SERVICE_USER}" \
    WEBHOOK_PORT="${WEBHOOK_PORT}" \
        bash "${PROJECT_DIR}/deploy/preflight.sh"
}

echo "==> [1/10] 检查项目目录 (${PROJECT_DIR})"
if [ ! -f "${PROJECT_DIR}/reviewagent/main.py" ]; then
    echo "    项目代码不存在，请先把仓库同步到 ${PROJECT_DIR}"
    exit 1
fi
cd "${PROJECT_DIR}"

echo "==> [2/10] 服务账号 (${SERVICE_USER})"
if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    if [ "$(id -u)" -eq 0 ] && command -v useradd >/dev/null 2>&1; then
        useradd --system --create-home --home-dir "/home/${SERVICE_USER}" \
            --shell /bin/bash "${SERVICE_USER}"
        echo "    已创建系统账号 ${SERVICE_USER}"
    else
        echo "    服务账号不存在且无法自动创建；请先 useradd --system ${SERVICE_USER}"
        exit 1
    fi
fi

echo "==> [3/10] 实例配置 (${ENV_FILE})"
if [ ! -f "${ENV_FILE}" ]; then
    if [ ! -f "${ENV_TEMPLATE}" ]; then
        echo "    模板缺失: ${ENV_TEMPLATE}"
        exit 1
    fi
    if [ "$(id -u)" -eq 0 ]; then
        install -m 600 -o "${SERVICE_USER}" -g "${SERVICE_USER}" "${ENV_TEMPLATE}" "${ENV_FILE}"
    else
        install -m 600 "${ENV_TEMPLATE}" "${ENV_FILE}"
    fi
    echo "    已生成 ${ENV_FILE}（600 权限）"
    echo "    请填写以下必填项后重新执行 deploy.sh："
    echo "      GITLAB_URL / GITLAB_PERSONAL_ACCESS_TOKEN / GITLAB_WEBHOOK_SECRET"
    echo "      LLM_PROVIDER=qoder-sdk 时还需 QODER_SDK_PAT"
    exit 0
fi

if [ "${RUN_PREFLIGHT_ONLY}" -eq 1 ]; then
    run_preflight
    exit $?
fi

echo "==> [4/10] Python 虚拟环境"
if ! command -v python3.12 >/dev/null 2>&1; then
    echo "    未找到 python3.12，请先安装（apt install python3.12 python3.12-venv）"
    exit 1
fi
PYTHON_BIN="$(command -v python3.12)"
# pip install -e 会在项目根写 egg-info，venv 也会写文件，两者都归服务账号所有。
$SUDO mkdir -p "${VENV}"
$SUDO chown "${SERVICE_USER}:${SERVICE_USER}" "${PROJECT_DIR}" "${VENV}"
if [ ! -x "${VENV}/bin/python" ]; then
    run_as_service "${PYTHON_BIN}" -m venv "${VENV}"
fi
run_as_service "${VENV}/bin/python" -m pip install --upgrade pip
run_as_service "${VENV}/bin/python" -m pip install -e ".[dev]"

echo "==> [5/10] 数据目录"
# 全部归服务账号所有：worker 要在这里 clone 仓库、写 telemetry.db / 周报。
$SUDO install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 750 \
    "${DATA_DIR}" "${DATA_DIR}/repos" "${DATA_DIR}/weekly_reports"
# 工作树在 /tmp：重启后会被系统清理，worker 会以自身身份重建；这里先修正历史遗留的 root 属主。
$SUDO install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 700 "${WORKTREE_DIR}"

echo "==> [6/10] Redis"
redis_url="$(env_value REDIS_URL)"
redis_url="${redis_url:-redis://127.0.0.1:6379/0}"
redis_rest="${redis_url#redis://}"
redis_rest="${redis_rest#*@}"
redis_host="${redis_rest%%:*}"
redis_host="${redis_host%%/*}"
redis_port=""
case "${redis_rest}" in
    *:*/*) redis_port="${redis_rest#*:}"; redis_port="${redis_port%%/*}" ;;
    *:*) redis_port="${redis_rest#*:}" ;;
esac
redis_host="${redis_host:-127.0.0.1}"
redis_port="${redis_port:-6379}"

redis_ping() {
    command -v redis-cli >/dev/null 2>&1 \
        && [ "$(redis-cli -h "${redis_host}" -p "${redis_port}" ping 2>/dev/null)" = "PONG" ]
}

if redis_ping; then
    echo "    已在运行: ${redis_host}:${redis_port}"
elif [ "${redis_host}" != "127.0.0.1" ] && [ "${redis_host}" != "localhost" ]; then
    echo "    远程 Redis 不可达: ${redis_host}:${redis_port}（请先启动远端实例）"
    exit 1
else
    redis_unit=""
    for candidate in redis-server.service redis.service; do
        if systemctl list-unit-files "${candidate}" >/dev/null 2>&1 \
            && systemctl list-unit-files "${candidate}" --no-legend 2>/dev/null | grep -q "${candidate}"; then
            redis_unit="${candidate}"
            break
        fi
    done
    if [ -n "${redis_unit}" ]; then
        $SUDO systemctl enable --now "${redis_unit}"
        echo "    已通过 ${redis_unit} 启动并设为开机自启"
    elif command -v redis-server >/dev/null 2>&1; then
        redis-server --daemonize yes --bind "${redis_host}" --port "${redis_port}"
        echo "    WARNING: 本机无 redis systemd unit，已手动拉起；重启后需自行启动 Redis"
    else
        echo "    未找到 redis-server；请 apt install redis-server 或配置远端 REDIS_URL"
        exit 1
    fi
    sleep 1
    redis_ping || { echo "    Redis 启动失败: ${redis_host}:${redis_port}"; exit 1; }
fi

echo "==> [7/10] 前置检查"
run_preflight || {
    echo "    前置检查未通过，已停止；修复后重跑 deploy.sh"
    exit 1
}

echo "==> [8/10] 场景包附属文件"
provider="$(env_value LLM_PROVIDER)"
if [ "${provider:-qodercli}" = "qodercli" ]; then
    # QoderCLI ACP 从 .qoder/agents/*.md 读取子代理定义，缺失会导致检视直接失败。
    run_with_env "${VENV}/bin/python" scripts/sync_qoder_agents.py
    echo "    .qoder/agents 已同步"
fi

echo "==> [9/10] 安装 systemd units 并设置开机自启"
UNITS=(
    reviewagent-webhook.service
    reviewagent-worker@.service
    reviewagent-weekly-worker.service
    reviewagent-weekly-enqueue.service
    reviewagent-weekly.timer
    reviewagent-reconciler.service
    reviewagent-reconciler.timer
)
for unit in "${UNITS[@]}"; do
    if [ ! -f "deploy/${unit}" ]; then
        echo "    缺少 deploy/${unit}"
        exit 1
    fi
done

install_unit() {
    local unit="$1" tmp
    tmp="$(mktemp)"
    # unit 里写的是默认路径；PROJECT_DIR / DATA_DIR 被覆盖时按实际路径落盘。
    sed -e "s#/home/workflow/ReviewAgent#${PROJECT_DIR}#g" \
        -e "s#/var/lib/reviewagent#${DATA_DIR}#g" \
        "deploy/${unit}" > "${tmp}"
    $SUDO install -m 0644 "${tmp}" "/etc/systemd/system/${unit}"
    rm -f "${tmp}"
}

for unit in "${UNITS[@]}"; do
    install_unit "${unit}"
done

# 旧的单 worker unit 已被 reviewagent-worker@.service 取代，避免与新实例抢同一队列。
if [ -f /etc/systemd/system/reviewagent-worker.service ]; then
    $SUDO systemctl disable --now reviewagent-worker.service >/dev/null 2>&1 || true
    $SUDO rm -f /etc/systemd/system/reviewagent-worker.service
    echo "    已下线旧 unit reviewagent-worker.service"
fi

$SUDO systemctl daemon-reload

$SUDO systemctl enable --now \
    reviewagent-webhook.service \
    reviewagent-weekly-worker.service \
    reviewagent-weekly.timer \
    reviewagent-reconciler.timer

worker_count="$(env_value RQ_WORKER_COUNT)"
worker_count="${worker_count:-3}"
case "${worker_count}" in
    ''|*[!0-9]*)
        echo "    RQ_WORKER_COUNT 非法: ${worker_count}（需正整数）"
        exit 1
        ;;
esac
if [ "${worker_count}" -lt 1 ] || [ "${worker_count}" -gt 32 ]; then
    echo "    RQ_WORKER_COUNT 超出范围 1..32: ${worker_count}"
    exit 1
fi
for i in $(seq 1 "${worker_count}"); do
    $SUDO systemctl enable --now "reviewagent-worker@${i}.service"
done
echo "    主队列 worker 实例: reviewagent-worker@1..${worker_count}"

# 调小 RQ_WORKER_COUNT 时，关掉多出来的实例，避免它们继续消费队列。
while IFS= read -r unit; do
    [ -n "${unit}" ] || continue
    idx="${unit#reviewagent-worker@}"
    idx="${idx%.service}"
    if [ "${idx}" -gt "${worker_count}" ] 2>/dev/null; then
        $SUDO systemctl disable --now "${unit}" >/dev/null 2>&1 || true
        echo "    已下线多余实例 ${unit}"
    fi
done < <($SUDO systemctl list-units --all --no-legend 'reviewagent-worker@*.service' 2>/dev/null \
            | awk '{print $1}' | grep -E '^reviewagent-worker@[0-9]+\.service$')

echo "==> [10/10] 部署后验证"
if [ "${RUN_VERIFY}" -eq 1 ]; then
    PROJECT_DIR="${PROJECT_DIR}" ENV_FILE="${ENV_FILE}" SERVICE_USER="${SERVICE_USER}" \
        WEBHOOK_PORT="${WEBHOOK_PORT}" bash "${PROJECT_DIR}/deploy/verify.sh"
else
    echo "    已跳过 (--no-verify)"
fi

echo ""
echo "==> 部署完成（所有 unit 已 enable，重启后自动拉起）"
echo "    health:        curl http://127.0.0.1:${WEBHOOK_PORT}/health"
echo "    readyz:        curl http://127.0.0.1:${WEBHOOK_PORT}/readyz"
echo "    webhook 日志:  sudo journalctl -u reviewagent-webhook -f"
echo "    worker 日志:   sudo journalctl -u 'reviewagent-worker@*' -f"
echo "    周报 worker:   sudo journalctl -u reviewagent-weekly-worker -f"
echo "    reconciler:    sudo journalctl -u reviewagent-reconciler -f"
echo "    定时任务:      sudo systemctl list-timers 'reviewagent-*.timer'"
echo "    再次自检:      sudo bash deploy/preflight.sh && sudo bash deploy/verify.sh"
