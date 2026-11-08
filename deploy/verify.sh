#!/usr/bin/env bash
# ReviewAgent 部署后验证：服务状态、开机自启、HTTP 探针、配置指纹一致性。
#
# 用法:
#   sudo bash deploy/verify.sh              # 最多等 30s 让 /health 起来
#   sudo bash deploy/verify.sh --wait 120   # 自定义等待秒数
#
# 退出码: 0 = 全部通过; 1 = 有失败项。
set -uo pipefail

PROJECT_DIR="${PROJECT_DIR:-/home/workflow/ReviewAgent}"
ENV_FILE="${ENV_FILE:-${PROJECT_DIR}/config/instance.env}"
SERVICE_USER="${SERVICE_USER:-workflow}"
WEBHOOK_PORT="${WEBHOOK_PORT:-3000}"

WAIT_SECONDS=30
while [ $# -gt 0 ]; do
    case "$1" in
        --wait) WAIT_SECONDS="${2:-30}"; shift 2 ;;
        *) echo "未知参数: $1（支持 --wait <秒>）" >&2; exit 2 ;;
    esac
done

FAIL=0
ok() { printf '  [ ok ] %s\n' "$*"; }
warn() { printf '  [warn] %s\n' "$*"; }
fail() {
    printf '  [FAIL] %s\n' "$*"
    FAIL=$((FAIL + 1))
}
section() { printf '\n== %s ==\n' "$*"; }

env_value() {
    local key="$1"
    [ -f "${ENV_FILE}" ] || return 0
    sed -n "s/^[[:space:]]*${key}=//p" "${ENV_FILE}" | tail -n 1 \
        | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/" | tr -d '\r'
}

worker_units() {
    systemctl list-units --all --no-legend 'reviewagent-worker@*.service' 2>/dev/null \
        | awk '{print $1}' | grep -E '^reviewagent-worker@[0-9]+\.service$' | sort -V
}

section "开机自启 (is-enabled)"
ENABLE_UNITS=(reviewagent-webhook.service reviewagent-weekly-worker.service
              reviewagent-weekly.timer reviewagent-reconciler.timer)
while IFS= read -r unit; do
    [ -n "${unit}" ] && ENABLE_UNITS+=("${unit}")
done < <(worker_units)

for unit in "${ENABLE_UNITS[@]}"; do
    state="$(systemctl is-enabled "${unit}" 2>/dev/null || true)"
    case "${state}" in
        enabled|enabled-runtime) ok "${unit}: ${state}" ;;
        "") fail "${unit}: 未安装" ;;
        *) fail "${unit}: ${state}（开机不会自启，执行 systemctl enable ${unit}）" ;;
    esac
done

section "运行状态 (is-active)"
for unit in "${ENABLE_UNITS[@]}"; do
    state="$(systemctl is-active "${unit}" 2>/dev/null || true)"
    if [ "${state}" = "active" ]; then
        ok "${unit}: active"
    else
        fail "${unit}: ${state:-unknown}（journalctl -u ${unit} -n 50 查看原因）"
    fi
done

section "HTTP 探针"
deadline=$((SECONDS + WAIT_SECONDS))
health_ok=0
while [ "${SECONDS}" -lt "${deadline}" ]; do
    if curl -fsS --max-time 3 "http://127.0.0.1:${WEBHOOK_PORT}/health" 2>/dev/null | grep -q '"ok"'; then
        health_ok=1
        break
    fi
    sleep 2
done
if [ "${health_ok}" -eq 1 ]; then
    ok "GET /health → ok"
else
    fail "GET /health 在 ${WAIT_SECONDS}s 内未就绪: http://127.0.0.1:${WEBHOOK_PORT}/health"
fi

ready_body="$(curl -sS --max-time 10 "http://127.0.0.1:${WEBHOOK_PORT}/readyz" 2>/dev/null)"
if printf '%s' "${ready_body}" | grep -q '"status":"ok"'; then
    ok "GET /readyz → ok"
else
    fail "GET /readyz 未就绪: ${ready_body:-无响应}"
fi

telemetry_body="$(curl -sS --max-time 10 "http://127.0.0.1:${WEBHOOK_PORT}/api/v1/telemetry/health" 2>/dev/null)"
if printf '%s' "${telemetry_body}" | grep -q '"status":"ok"'; then
    ok "GET /api/v1/telemetry/health → ok"
else
    fail "GET /api/v1/telemetry/health 失败: ${telemetry_body:-无响应}"
fi

section "Redis"
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
if command -v redis-cli >/dev/null 2>&1; then
    if [ "$(redis-cli -h "${redis_host}" -p "${redis_port}" ping 2>/dev/null)" = "PONG" ]; then
        ok "Redis PONG (${redis_host}:${redis_port})"
    else
        fail "Redis 无响应 (${redis_host}:${redis_port})"
    fi
else
    warn "未找到 redis-cli，跳过 Redis 检查"
fi

section "配置指纹一致性"
fingerprint_of() {
    local unit="$1" key="$2"
    journalctl -u "${unit}" -n 5000 --no-pager -o cat 2>/dev/null \
        | grep -o "${key}=[0-9a-f]*" | tail -n 1 | cut -d= -f2
}

WEBHOOK_PROFILE="$(fingerprint_of reviewagent-webhook profile_fingerprint)"
WEBHOOK_CONFIG="$(fingerprint_of reviewagent-webhook config_fingerprint)"
if [ -z "${WEBHOOK_CONFIG}" ]; then
    fail "reviewagent-webhook 日志中找不到 config_fingerprint（是否启动过？）"
else
    ok "webhook config_fingerprint=${WEBHOOK_CONFIG:0:12} profile_fingerprint=${WEBHOOK_PROFILE:0:12}"
fi

compare_fp() {
    local unit="$1" profile config
    profile="$(fingerprint_of "${unit}" profile_fingerprint)"
    config="$(fingerprint_of "${unit}" config_fingerprint)"
    if [ -z "${config}" ]; then
        fail "${unit}: 日志中找不到 config_fingerprint"
        return
    fi
    if [ -n "${WEBHOOK_CONFIG}" ] && [ "${config}" != "${WEBHOOK_CONFIG}" ]; then
        fail "${unit}: config_fingerprint=${config:0:12} 与 webhook=${WEBHOOK_CONFIG:0:12} 不一致（实例配置漂移）"
        return
    fi
    if [ -n "${WEBHOOK_PROFILE}" ] && [ "${profile}" != "${WEBHOOK_PROFILE}" ]; then
        fail "${unit}: profile_fingerprint=${profile:0:12} 与 webhook=${WEBHOOK_PROFILE:0:12} 不一致（场景包漂移）"
        return
    fi
    ok "${unit}: 指纹与 webhook 一致"
}

compare_fp reviewagent-weekly-worker.service
while IFS= read -r unit; do
    [ -n "${unit}" ] && compare_fp "${unit}"
done < <(worker_units)

section "定时任务"
systemctl list-timers --all 'reviewagent-*.timer' --no-pager 2>/dev/null \
    | sed -n '1,8p' | sed 's/^/  /'

printf '\n== 汇总 ==\n'
if [ "${FAIL}" -gt 0 ]; then
    printf '  fail=%d\n' "${FAIL}"
    echo "  => 部署验证未通过"
    exit 1
fi
echo "  => 部署验证通过（所有 unit 均已 enable，重启后自动拉起）"
