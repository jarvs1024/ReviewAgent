#!/usr/bin/env bash
# ReviewAgent 服务管理脚本 (86 服务器).
#
# 用法:
#   bash scripts/services_ops.sh <command>
#
# 命令:
#   start     启动所有服务
#   stop      停止所有服务
#   restart   重启所有服务
#   status    查看所有服务状态 (默认, 含 PID / uptime / 最近日志)
#   logs      实时跟踪所有服务日志 (类似 tail -f)
#
# 示例:
#   bash scripts/services_ops.sh            # 默认查看状态
#   bash scripts/services_ops.sh restart    # 重启
#   bash scripts/services_ops.sh stop       # 停止
#   bash scripts/services_ops.sh logs       # 跟踪日志
set -eo pipefail

# ============================================================
# 配置
# ============================================================
SYSTEMD_SERVICES=(
    "reviewagent-webhook"
    "reviewagent-worker@1"
    "reviewagent-worker@2"
    "reviewagent-worker@3"
)
WORKER_COUNT=3

OPENCODE_BIN="/usr/local/bin/opencode"
OPENCODE_HOST="127.0.0.1"
OPENCODE_PORT="4096"
OPENCODE_LOG="/tmp/opencode-serve.log"

# 颜色
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

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

# opencode PID (可能多个, 取最新的)
_opencode_pid() {
    pgrep -f 'opencode serve' 2>/dev/null | head -1 || true
}

_opencode_alive() {
    local pid
    pid=$(_opencode_pid)
    [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

# ============================================================
# 核心操作
# ============================================================
do_start() {
    _info "Starting all services ..."
    echo ""

    # 1) systemd 服务
    for svc in "${SYSTEMD_SERVICES[@]}"; do
        if systemctl is-active --quiet "$svc" 2>/dev/null; then
            _warn "$svc already running, skipping"
        else
            _info "Starting $svc ..."
            systemctl start "$svc"
            _ok "$svc started"
        fi
    done

    # 2) opencode
    if _opencode_alive; then
        _warn "opencode serve already running (PID $(_opencode_pid))"
    else
        _info "Starting opencode serve ..."
        nohup "$OPENCODE_BIN" serve \
            --hostname "$OPENCODE_HOST" --port "$OPENCODE_PORT" --print-logs \
            > "$OPENCODE_LOG" 2>&1 &
        sleep 2
        if _opencode_alive; then
            _ok "opencode serve started (PID $(_opencode_pid))"
        else
            _fail "opencode serve failed to start (check $OPENCODE_LOG)"
            return 1
        fi
    fi

    echo ""
    do_status
}

do_stop() {
    _info "Stopping all services ..."
    echo ""

    # 1) systemd 服务
    for svc in "${SYSTEMD_SERVICES[@]}"; do
        if systemctl is-active --quiet "$svc" 2>/dev/null; then
            _info "Stopping $svc ..."
            systemctl stop "$svc"
            _ok "$svc stopped"
        else
            _warn "$svc not running, skipping"
        fi
    done

    # 2) opencode
    if _opencode_alive; then
        local pid=$(_opencode_pid)
        _info "Stopping opencode serve (PID $pid) ..."
        kill "$pid" 2>/dev/null || true
        sleep 1
        if _opencode_alive; then
            _warn "opencode still alive, sending SIGKILL ..."
            kill -9 "$pid" 2>/dev/null || true
            sleep 1
        fi
        _ok "opencode serve stopped"
    else
        _warn "opencode serve not running, skipping"
    fi

    echo ""
    _ok "all services stopped"
}

do_restart() {
    _info "Restarting all services ..."
    echo ""

    # 1) systemd 服务
    for svc in "${SYSTEMD_SERVICES[@]}"; do
        _info "Restarting $svc ..."
        systemctl restart "$svc"
        _ok "$svc restarted"
    done

    # 2) opencode
    if _opencode_alive; then
        local pid=$(_opencode_pid)
        _info "Stopping opencode serve (PID $pid) ..."
        kill "$pid" 2>/dev/null || true
        sleep 1
    fi
    _info "Starting opencode serve ..."
    nohup "$OPENCODE_BIN" serve \
        --hostname "$OPENCODE_HOST" --port "$OPENCODE_PORT" --print-logs \
        > "$OPENCODE_LOG" 2>&1 &
    sleep 2

    if _opencode_alive; then
        _ok "opencode serve started (PID $(_opencode_pid))"
    else
        _fail "opencode serve failed to start (check $OPENCODE_LOG)"
        return 1
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
    for svc in "${SYSTEMD_SERVICES[@]}"; do
        local status pid uptime
        status=$(systemctl is-active "$svc" 2>/dev/null || echo "unknown")

        if [[ "$status" == "active" ]]; then
            pid=$(systemctl show -p MainPID --value "$svc" 2>/dev/null || echo "-")
            uptime=$(systemctl show -p ActiveEnterTimestamp --value "$svc" 2>/dev/null || echo "-")
            if [[ "$uptime" != "-" && "$uptime" != "" ]]; then
                # 计算运行时长
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
            all_active=false
        fi
    done

    echo ""

    # 2) opencode
    _info "opencode serve:"
    echo ""
    if _opencode_alive; then
        local oc_pid oc_mem oc_port_status
        oc_pid=$(_opencode_pid)
        oc_mem=$(ps -p "$oc_pid" -o rss= 2>/dev/null | awk '{printf "%.0fMB", $1/1024}' || echo "-")
        # 检查端口是否在监听
        if ss -tlnp 2>/dev/null | grep -q ":${OPENCODE_PORT} "; then
            oc_port_status="${GREEN}listening on :${OPENCODE_PORT}${NC}"
        else
            oc_port_status="${RED}NOT listening on :${OPENCODE_PORT}${NC}"
        fi
        printf "  ${GREEN}%-30s %-10s %-8s %-12s %-20s${NC}\n" \
            "opencode serve" "active" "$oc_pid" "$oc_mem" ""
        echo -e "  Port: $oc_port_status"
        echo -e "  Log:  $OPENCODE_LOG"
    else
        printf "  ${RED}%-30s %-10s %-8s${NC}\n" "opencode serve" "inactive" "-"
        echo -e "  ${RED}Check: $OPENCODE_LOG${NC}"
        all_active=false
    fi

    echo ""

    # 3) Redis 连接检查
    _info "dependencies:"
    echo ""
    local redis_ok=false
    if command -v redis-cli &>/dev/null && redis-cli ping 2>/dev/null | grep -q PONG; then
        redis_ok=true
    elif python3 -c "import redis; redis.Redis().ping()" 2>/dev/null \
      || /home/workflow/ReviewAgent/.venv/bin/python -c "import redis; redis.Redis().ping()" 2>/dev/null; then
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
    rq_info=$(/home/workflow/ReviewAgent/.venv/bin/python -c "
import redis
r = redis.Redis()
for q in ['review', 'review-weekly']:
    pending = r.llen(q)
    # failed key may be a set (RQ >= 1.14) or list; handle both
    try:
        ktype = r.type(f'rq:failed:{q}').decode()
        if ktype == b'set':
            failed = r.scard(f'rq:failed:{q}')
        elif ktype == b'list':
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

    local log_files=()

    # systemd journal 用 journalctl
    echo "  Attaching to:"
    for svc in "${SYSTEMD_SERVICES[@]}"; do
        echo "    - $svc (journalctl -u $svc)"
    done
    if [[ -f "$OPENCODE_LOG" ]]; then
        echo "    - opencode serve ($OPENCODE_LOG)"
    fi
    echo ""
    _separator
    echo ""

    # 用 tail -f 跟踪 opencode 日志, 同时用 journalctl 跟踪 systemd 日志
    # 简单方案: 开两个 tail
    trap 'echo; _info "log tail stopped"; exit 0' INT TERM

    if [[ -f "$OPENCODE_LOG" ]]; then
        # 合并 journalctl + opencode log
        (
            journalctl -u reviewagent-webhook -u reviewagent-worker@1 \
                       -u reviewagent-worker@2 -u reviewagent-worker@3 \
                       -n 0 --no-pager -f 2>/dev/null &
            tail -n 0 -f "$OPENCODE_LOG" 2>/dev/null &
            wait
        )
    else
        journalctl -u reviewagent-webhook -u reviewagent-worker@1 \
                   -u reviewagent-worker@2 -u reviewagent-worker@3 \
                   -n 50 --no-pager -f
    fi
}

usage() {
    echo "Usage: $0 {start|stop|restart|status|logs}"
    echo ""
    echo "Commands:"
    echo "  start     Start all services (webhook + workers + opencode)"
    echo "  stop      Stop all services"
    echo "  restart   Restart all services (default)"
    echo "  status    Show detailed service status"
    echo "  logs      Tail all service logs in real-time"
    echo ""
    echo "Examples:"
    echo "  $0              # restart (default)"
    echo "  $0 status       # check status"
    echo "  $0 logs         # follow logs"
}

# ============================================================
# 入口
# ============================================================
cmd="${1:-status}"

case "$cmd" in
    start)   do_start   ;;
    stop)    do_stop    ;;
    restart) do_restart ;;
    status)  do_status  ;;
    logs)    do_logs    ;;
    -h|--help|help) usage ;;
    *)
        _fail "Unknown command: $cmd"
        usage
        exit 1
        ;;
esac
