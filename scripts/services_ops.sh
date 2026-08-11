#!/usr/bin/env bash
# ReviewAgent 服务管理脚本 (ci-runner / 25 服务器).
#
# 用法:
#   bash scripts/services_ops.sh <command>
#
# 命令:
#   start         启动所有服务 (systemd + qodercli 健康检查)
#   stop          停止所有服务
#   restart       重启所有服务 (默认)
#   status        查看所有服务状态 (含 PID / uptime / Redis / RQ 队列)
#   logs          实时跟踪所有服务日志
#   clean-pycache 清理 __pycache__ 并重启 (部署后常用)
#
# 示例:
#   bash scripts/services_ops.sh            # 默认查看状态
#   bash scripts/services_ops.sh restart    # 重启
#   bash scripts/services_ops.sh clean-pycache  # 清缓存+重启
set -eo pipefail

# ============================================================
# 配置 — 从 .env 动态读取
# ============================================================
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${PROJECT_DIR:-$(cd "${SCRIPT_DIR}/.." && pwd)}"

# 加载 .env (如果存在)
if [[ -f "${PROJECT_DIR}/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "${PROJECT_DIR}/.env"
    set +a
fi

WORKER_COUNT="${RQ_WORKER_COUNT:-3}"

# 构造 systemd worker 列表: reviewagent-worker@1 @2 @3 ...
SYSTEMD_WORKERS=()
for ((i = 1; i <= WORKER_COUNT; i++)); do
    SYSTEMD_WORKERS+=("reviewagent-worker@${i}")
done

SYSTEMD_SERVICES=(
    "reviewagent-webhook"
    "${SYSTEMD_WORKERS[@]}"
)

# 周报相关服务
WEEKLY_SERVICES=(
    "reviewagent-weekly-worker"
    "reviewagent-weekly.timer"
)

ALL_SYSTEMD=(
    "${SYSTEMD_SERVICES[@]}"
    "${WEEKLY_SERVICES[@]}"
)

# qodercli: 从 .env 读取, 空则自动探测
QODERCLI_NODE="${QODERCLI_NODE_PATH:-$(which node 2>/dev/null || true)}"
QODERCLI_JS="${QODERCLI_JS_PATH:-$(readlink -f "$(which qodercli 2>/dev/null)" 2>/dev/null || true)}"
QODERCLI_MODEL="${QODERCLI_MODEL:-DeepSeek-V4-Flash}"
QODERCLI_FALLBACK="${QODERCLI_FALLBACK_MODEL:-}"

# 颜色
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

# ============================================================
# 工具函数
# ============================================================
_info()  { echo -e "${CYAN}==> $*${NC}"; }
_ok()    { echo -e "${GREEN}  [ok] $*${NC}"; }
_warn()  { echo -e "${YELLOW}  [!!] $*${NC}"; }
_fail()  { echo -e "${RED}  [err] $*${NC}"; }

_separator() {
    echo "────────────────────────────────────────────────────────"
}

# qodercli 健康检查: node + js 存在 + --version 可用
_qodercli_healthy() {
    [[ -n "$QODERCLI_NODE" ]] && [[ -x "$QODERCLI_NODE" ]] &&
    [[ -n "$QODERCLI_JS" ]] && [[ -f "$QODERCLI_JS" ]] &&
    "$QODERCLI_NODE" "$QODERCLI_JS" --version >/dev/null 2>&1
}

# 优雅终止匹配进程 (SIGTERM → 等待 → SIGKILL)
# 参考 restart_local.sh 的 terminate_matching
_terminate_matching() {
    local pattern="$1"
    local matched_pids
    matched_pids="$(pgrep -f -- "${pattern}" || true)"
    if [[ -z "${matched_pids}" ]]; then
        return
    fi
    # SIGTERM
    kill -TERM ${matched_pids} 2>/dev/null || true
    # 等待退出 (最多 5 秒)
    for _attempt in {1..20}; do
        if ! pgrep -f -- "${pattern}" >/dev/null 2>&1; then
            return
        fi
        sleep 0.25
    done
    # 仍未退出 → SIGKILL
    matched_pids="$(pgrep -f -- "${pattern}" || true)"
    if [[ -n "${matched_pids}" ]]; then
        _warn "force killing: ${pattern}"
        kill -KILL ${matched_pids} 2>/dev/null || true
    fi
}

# 终止 RQ worker 的 horse 子进程 (正在执行的任务)
# 参考 restart_local.sh 的 terminate_worker_jobs
_terminate_worker_jobs() {
    local jobs_terminated=0
    local worker_pid horse_pid horse_pgid
    while IFS= read -r worker_pid; do
        while IFS= read -r horse_pid; do
            horse_pgid="$(ps -o pgid= -p "${horse_pid}" 2>/dev/null | tr -d ' ')"
            if [[ -n "${horse_pgid}" ]]; then
                kill -KILL -- "-${horse_pgid}" 2>/dev/null || true
                jobs_terminated=1
            fi
        done < <(pgrep -P "${worker_pid}" 2>/dev/null || true)
    done < <(pgrep -f -- "rq.cli worker" 2>/dev/null || true)
    if [[ "${jobs_terminated}" -eq 1 ]]; then
        sleep 1
    fi
}

# ============================================================
# 核心操作
# ============================================================
do_start() {
    _info "Starting all services ..."
    echo ""

    # 1) systemd 服务 (批量启动)
    local svcs_to_start=()
    for svc in "${ALL_SYSTEMD[@]}"; do
        if systemctl is-active --quiet "$svc" 2>/dev/null; then
            _warn "$svc already running, skipping"
        else
            svcs_to_start+=("$svc")
        fi
    done
    if [[ ${#svcs_to_start[@]} -gt 0 ]]; then
        systemctl start "${svcs_to_start[@]}"
        for svc in "${svcs_to_start[@]}"; do
            _ok "$svc started"
        done
    fi

    # 2) qodercli 健康检查 (subprocess 模式, 无需启动常驻进程)
    if _qodercli_healthy; then
        local oc_ver
        oc_ver=$("$QODERCLI_NODE" "$QODERCLI_JS" --version 2>/dev/null || echo "unknown")
        _ok "qodercli ready (${oc_ver}) model=${QODERCLI_MODEL}"
        if [[ -n "$QODERCLI_FALLBACK" ]]; then
            _ok "fallback model: ${QODERCLI_FALLBACK}"
        fi
    else
        _fail "qodercli not healthy (node=${QODERCLI_NODE:-<not found>} js=${QODERCLI_JS:-<not found>})"
        all_active=false
    fi

    echo ""
    do_status
}

do_stop() {
    _info "Stopping all services ..."
    echo ""

    # 1) systemd 服务 (批量停止)
    local svcs_to_stop=()
    for svc in "${ALL_SYSTEMD[@]}"; do
        if systemctl is-active --quiet "$svc" 2>/dev/null; then
            svcs_to_stop+=("$svc")
        fi
    done
    if [[ ${#svcs_to_stop[@]} -gt 0 ]]; then
        systemctl stop "${svcs_to_stop[@]}"
        for svc in "${svcs_to_stop[@]}"; do
            _ok "$svc stopped"
        done
    else
        _warn "no systemd services running"
    fi

    echo ""
    _ok "all services stopped"
}

do_restart() {
    _info "Restarting all services ..."
    echo ""

    # 1) systemd 服务 (批量重启, 减少中断时间)
    systemctl restart "${SYSTEMD_SERVICES[@]}"
    for svc in "${SYSTEMD_SERVICES[@]}"; do
        _ok "$svc restarted"
    done

    # 2) qodercli 健康检查 (subprocess 模式, 无需重启常驻进程)
    if _qodercli_healthy; then
        _ok "qodercli ready"
    else
        _warn "qodercli not healthy (node=${QODERCLI_NODE:-<not found>} js=${QODERCLI_JS:-<not found>})"
    fi

    echo ""
    do_status
}

do_status() {
    _separator
    echo -e "${BOLD}ReviewAgent Service Status${NC}"
    _separator
    echo ""

    # 1) systemd 服务详情
    _info "systemd services:"
    echo ""
    printf "  ${BOLD}%-30s %-10s %-8s %-20s${NC}\n" "SERVICE" "STATUS" "PID" "UPTIME"
    printf "  %-30s %-10s %-8s %-20s\n" "──────" "──────" "───" "──────"

    local all_active=true
    for svc in "${ALL_SYSTEMD[@]}"; do
        local status pid uptime
        status=$(systemctl is-active "$svc" 2>/dev/null || echo "unknown")

        if [[ "$status" == "active" ]]; then
            pid=$(systemctl show -p MainPID --value "$svc" 2>/dev/null || echo "-")
            uptime=$(systemctl show -p ActiveEnterTimestamp --value "$svc" 2>/dev/null || echo "-")
            if [[ "$uptime" != "-" && "$uptime" != "" ]]; then
                local start_ts now_ts diff_secs
                start_ts=$(date -d "$uptime" +%s 2>/dev/null || echo 0)
                now_ts=$(date +%s)
                diff_secs=$((now_ts - start_ts))
                if (( diff_secs > 86400 )); then
                    uptime="$((diff_secs / 86400))d $((diff_secs % 86400 / 3600))h"
                elif (( diff_secs > 3600 )); then
                    uptime="$((diff_secs / 3600))h $((diff_secs % 3600 / 60))m"
                else
                    uptime="$((diff_secs / 60))m"
                fi
            else
                uptime="-"
            fi
            printf "  ${GREEN}%-30s %-10s %-8s %-20s${NC}\n" "$svc" "$status" "$pid" "$uptime"
        else
            pid="-"
            uptime="-"
            printf "  ${RED}%-30s %-10s %-8s %-20s${NC}\n" "$svc" "$status" "$pid" "$uptime"
            # timer 的 active (waiting) 不算 unhealthy
            if [[ "$svc" == *"timer"* ]]; then
                : # timer inactive 才报警
                all_active=false
            else
                all_active=false
            fi
        fi
    done

    # timer 特殊处理: active (waiting) 是正常状态
    local timer_status
    timer_status=$(systemctl is-active reviewagent-weekly.timer 2>/dev/null || echo "unknown")
    if [[ "$timer_status" == "active" ]]; then
        : # already printed above
    fi

    echo ""

    # 2) qodercli
    _info "qodercli (LLM subprocess):"
    echo ""
    if _qodercli_healthy; then
        local qc_ver qc_model qc_fallback
        qc_ver=$("$QODERCLI_NODE" "$QODERCLI_JS" --version 2>/dev/null || echo "unknown")
        qc_model="${QODERCLI_MODEL}"
        qc_fallback="${QODERCLI_FALLBACK:-<none>}"
        printf "  ${GREEN}%-20s %-10s %-30s${NC}\n" "qodercli" "ready" "version: ${qc_ver}"
        printf "  %-20s %-10s %-30s\n" "primary model" "" "${qc_model}"
        printf "  %-20s %-10s %-30s\n" "fallback model" "" "${qc_fallback}"
        printf "  %-20s %s\n" "node" "${QODERCLI_NODE}"
        printf "  %-20s %s\n" "js" "${QODERCLI_JS}"
    else
        printf "  ${RED}%-20s %-10s${NC}\n" "qodercli" "UNHEALTHY"
        echo -e "  node: ${QODERCLI_NODE:-${RED}<not found>${NC}}"
        echo -e "  js:   ${QODERCLI_JS:-${RED}<not found>${NC}}"
        all_active=false
    fi

    echo ""

    # 3) Redis 连接检查
    _info "dependencies:"
    echo ""
    local redis_ok=false
    if command -v redis-cli &>/dev/null && redis-cli ping 2>/dev/null | grep -q PONG; then
        redis_ok=true
    elif "${PROJECT_DIR}/.venv/bin/python" -c "import redis; redis.Redis().ping()" 2>/dev/null; then
        redis_ok=true
    elif python3 -c "import redis; redis.Redis().ping()" 2>/dev/null; then
        redis_ok=true
    fi
    if $redis_ok; then
        printf "  ${GREEN}%-30s %-10s${NC}\n" "Redis" "PONG"
    else
        printf "  ${RED}%-30s %-10s${NC}\n" "Redis" "UNREACHABLE"
        all_active=false
    fi

    # 4) RQ 队列概况
    echo ""
    _info "RQ queues:"
    echo ""
    local rq_info
    rq_info=$("${PROJECT_DIR}/.venv/bin/python" -c "
import redis
r = redis.Redis()
for q in ['${RQ_QUEUE_NAME:-review-v2}', '${RQ_WEEKLY_QUEUE_NAME:-review-weekly}']:
    pending = r.llen(q)
    try:
        ktype = r.type(f'rq:failed:{q}').decode()
        if ktype == 'set':
            failed = r.scard(f'rq:failed:{q}')
        elif ktype == 'list':
            failed = r.llen(f'rq:failed:{q}')
        else:
            failed = 0
    except Exception:
        failed = 0
    print(f'  {q:30s} pending={pending}  failed={failed}')
" 2>/dev/null) || rq_info="  (unable to query RQ queues)"
    echo "$rq_info"

    echo ""
    _separator
    if $all_active; then
        echo -e "  ${GREEN}${BOLD}All services are healthy.${NC}"
    else
        echo -e "  ${RED}${BOLD}Some services are not healthy. Check above.${NC}"
    fi
    _separator
    echo ""
}

do_logs() {
    _info "Tailing service logs (Ctrl+C to exit) ..."
    echo ""

    echo "  Attaching to:"
    for svc in "${ALL_SYSTEMD[@]}"; do
        echo "    - $svc (journalctl)"
    done
    echo ""
    _separator
    echo ""

    trap 'echo; _info "log tail stopped"; exit 0' INT TERM

    # 构建 journalctl -u 参数列表
    local jc_args=(-u reviewagent-webhook)
    for w in "${SYSTEMD_WORKERS[@]}"; do
        jc_args+=(-u "$w")
    done
    jc_args+=(-u reviewagent-weekly-worker)

    journalctl "${jc_args[@]}" -n 50 --no-pager -f
}

do_clean_pycache() {
    _info "Cleaning __pycache__ ..."
    local count
    count=$(find "${PROJECT_DIR}/reviewagent" -type d -name __pycache__ 2>/dev/null | wc -l)
    find "${PROJECT_DIR}/reviewagent" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
    _ok "removed ${count} __pycache__ directories"

    echo ""
    _info "Restarting services ..."
    systemctl restart "${SYSTEMD_SERVICES[@]}"
    for svc in "${SYSTEMD_SERVICES[@]}"; do
        _ok "$svc restarted"
    done
    echo ""
    do_status
}

usage() {
    echo "Usage: $0 {start|stop|restart|status|logs|clean-pycache}"
    echo ""
    echo "Commands:"
    echo "  start          Start all services (webhook + workers + weekly)"
    echo "  stop           Stop all services"
    echo "  restart        Restart all services (default)"
    echo "  status         Show detailed service status"
    echo "  logs           Tail all service logs in real-time"
    echo "  clean-pycache  Remove __pycache__ and restart services"
    echo ""
    echo "Environment:"
    echo "  PROJECT_DIR     Project root (default: script's parent dir)"
    echo "  RQ_WORKER_COUNT Number of workers (default: 3, from .env)"
    echo ""
    echo "Examples:"
    echo "  $0                        # status (default)"
    echo "  $0 restart                # restart all"
    echo "  $0 clean-pycache          # deploy helper: clean cache + restart"
    echo "  $0 logs                   # follow logs"
}

# ============================================================
# 入口
# ============================================================
cmd="${1:-status}"

case "$cmd" in
    start)          do_start          ;;
    stop)           do_stop           ;;
    restart)        do_restart        ;;
    status)         do_status         ;;
    logs)           do_logs           ;;
    clean-pycache)  do_clean_pycache  ;;
    -h|--help|help) usage             ;;
    *)
        _fail "Unknown command: $cmd"
        usage
        exit 1
        ;;
esac
