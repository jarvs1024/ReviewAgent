#!/usr/bin/env bash
# 本地重启 ReviewAgent 全部常驻服务（screen 托管）
#
# 用法（在自己的终端执行，不要在 agent 沙箱里跑）:
#     bash scripts/restart_local.sh
#
# 托管的会话:
#   revagent-redis      redis-server :6380          （RQ 队列，本地进程）
#   revagent-opencode   opencode serve :4096        （仅 LLM_PROVIDER=opencode 时启动）
#   revagent-web        uvicorn :3000               （webhook + telemetry API）
#   revagent-workerN    rq worker review-v2         （MR 检视主队列）
#   revagent-weekly     rq worker review-v2-weekly  （周报独立队列）
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/Users/jarvs/ReviewAgent}"
OPENCODE_BIN="${OPENCODE_BIN:-${HOME}/.opencode/bin/opencode}"
REDIS_BIN="${REDIS_BIN:-$(command -v redis-server || true)}"
DEFAULT_RQ_WORKER_CLASS="reviewagent.workers.rq_worker.ReviewAgentSpawnWorker"
SESSIONS=(revagent-web revagent-weekly)

cd "${PROJECT_DIR}"
mkdir -p logs
# shellcheck source=scripts/_env.sh
source "${PROJECT_DIR}/scripts/_env.sh"
WORKER_COUNT="${RQ_WORKER_COUNT:-3}"

# 若从 Qoder App / agent 沙箱的终端启动，父进程会注入 App 内部的 SDK 运行变量。
# 这些变量会让服务里 qodercli 子进程误判为 App 的 SDK entrypoint，卡在
# "Control request timeout: initialize"。它们不属于本服务，启动前清掉；
# 实例真正需要的键（QODER_SDK_PAT 等）由 instance.env 提供，不受影响。
while IFS= read -r leaked_var; do
    unset "${leaked_var}" 2>/dev/null || true
done <<'EOF'
QODER_AGENT_SDK_ENTRYPOINT
QODER_AGENT_SDK_DAEMON
QODER_AGENT_SDK_VERSION
QODER_CLI
QODERCLI_RUNTIME_PACKAGING
QODER_WORKER_RUNTIME_ASSET_ROOT
QODER_SDK_AUTH_PAYLOAD_FILE
QODER_SDK_CUSTOM_BASE_URL_BYOK
QODER_CONFIG_DIR
QODERCN_CONFIG_DIR
QODER_SESSION_TYPE
QODER_SEARCH_HOST_PID
QODER_PRODUCT_ID
QODER_FEATURE_CODEBASE_BACKFLOW
QODER_FEATURE_WORKFLOWS_DISABLE
QODER_CLI_MAX_SUBAGENT_SPAWN_DEPTH
QODER_SECURITY_SCAN_SETTINGS_JSON
QODER_SECURITY_SCAN_RULES_FILE
QODER_SITES_ROOT
__QODER_BASH_PATH_PREFIX
EOF

# ---- Redis 本地进程定位（REDIS_URL 指向本机时才托管）----
REDIS_URL_VALUE="${REDIS_URL:-redis://127.0.0.1:6379/0}"
REDIS_HOSTPORT="${REDIS_URL_VALUE#*://}"
REDIS_HOSTPORT="${REDIS_HOSTPORT%%/*}"
REDIS_HOSTPORT="${REDIS_HOSTPORT##*@}"
REDIS_HOST="${REDIS_HOSTPORT%%:*}"
REDIS_PORT="${REDIS_HOSTPORT##*:}"
REDIS_DIR="${PROJECT_DIR}/data/redis"
REDIS_PIDFILE="${REDIS_DIR}/redis.pid"
MANAGE_REDIS=false
if [[ "${REDIS_HOST}" == "127.0.0.1" || "${REDIS_HOST}" == "localhost" ]]; then
    MANAGE_REDIS=true
fi
if [[ "${LLM_PROVIDER:-qodercli}" == "opencode" ]]; then
    START_OPENCODE=true
else
    START_OPENCODE=false
fi

terminate_matching() {
    local pattern="$1"
    local matched_pids
    matched_pids="$(pgrep -f -- "${pattern}" || true)"
    if [[ -z "${matched_pids}" ]]; then
        return
    fi
    kill -TERM ${matched_pids} 2>/dev/null || true
    for _attempt in {1..20}; do
        if ! pgrep -f -- "${pattern}" >/dev/null; then
            return
        fi
        sleep 0.25
    done
    matched_pids="$(pgrep -f -- "${pattern}" || true)"
    if [[ -n "${matched_pids}" ]]; then
        kill -KILL ${matched_pids} 2>/dev/null || true
    fi
}

terminate_worker_jobs() {
    local jobs_terminated=0
    local worker_pid
    local horse_pid
    local horse_pgid
    while IFS= read -r worker_pid; do
        while IFS= read -r horse_pid; do
            horse_pgid="$(ps -o pgid= -p "${horse_pid}" | tr -d ' ')"
            if [[ -n "${horse_pgid}" ]]; then
                kill -KILL -- "-${horse_pgid}" 2>/dev/null || true
                jobs_terminated=1
            fi
        done < <(pgrep -P "${worker_pid}" || true)
    done < <(pgrep -f -- "rq.cli worker" || true)
    if [[ "${jobs_terminated}" -eq 1 ]]; then
        sleep 1
    fi
}

echo "==> [1/3] 停止旧会话"
screen -wipe >/dev/null 2>&1 || true
terminate_worker_jobs
for s in "${SESSIONS[@]}"; do
    if screen -S "${s}" -X quit 2>/dev/null; then
        echo "    killed ${s}"
    fi
done
while IFS= read -r worker_session; do
    if screen -S "${worker_session}" -X quit 2>/dev/null; then
        echo "    killed ${worker_session}"
    fi
done < <(screen -ls 2>/dev/null | sed -nE 's/^[[:space:]]*[0-9]+\.(revagent-worker[0-9]+)[[:space:]].*/\1/p')
terminate_matching "rq.cli worker"
terminate_matching "${PROJECT_DIR}/.venv/bin/uvicorn reviewagent.main:app"
terminate_matching "${OPENCODE_BIN} serve --port 4096"

if ${MANAGE_REDIS}; then
    screen -S revagent-redis -X quit 2>/dev/null || true
    if [[ -f "${REDIS_PIDFILE}" ]]; then
        redis_pid="$(cat "${REDIS_PIDFILE}" 2>/dev/null || true)"
        if [[ -n "${redis_pid}" ]] && kill -0 "${redis_pid}" 2>/dev/null; then
            kill -TERM "${redis_pid}" 2>/dev/null || true
            for _attempt in {1..20}; do
                kill -0 "${redis_pid}" 2>/dev/null || break
                sleep 0.25
            done
            kill -KILL "${redis_pid}" 2>/dev/null || true
        fi
        rm -f "${REDIS_PIDFILE}"
    fi
fi
sleep 2

echo "==> [2/3] 启动服务"

if ${MANAGE_REDIS}; then
    if [[ -z "${REDIS_BIN}" ]]; then
        echo "    ERROR: 未找到 redis-server（brew install redis 或设置 REDIS_BIN）" >&2
        exit 1
    fi
    mkdir -p "${REDIS_DIR}"
    screen -dmS revagent-redis bash -c \
        "exec '${REDIS_BIN}' --port ${REDIS_PORT} --bind ${REDIS_HOST} --dir '${REDIS_DIR}' --appendonly yes --pidfile '${REDIS_PIDFILE}' --logfile '${PROJECT_DIR}/logs/redis-${REDIS_PORT}.log'"
    echo "    started revagent-redis (${REDIS_HOST}:${REDIS_PORT})"
    for _attempt in {1..40}; do
        if command -v redis-cli >/dev/null 2>&1; then
            redis-cli -h "${REDIS_HOST}" -p "${REDIS_PORT}" ping 2>/dev/null | grep -q PONG && break
        elif .venv/bin/python -c "import redis,sys; redis.Redis(host=sys.argv[1], port=int(sys.argv[2])).ping()" "${REDIS_HOST}" "${REDIS_PORT}" 2>/dev/null; then
            break
        fi
        sleep 0.25
    done
    if command -v redis-cli >/dev/null 2>&1 && ! redis-cli -h "${REDIS_HOST}" -p "${REDIS_PORT}" ping 2>/dev/null | grep -q PONG; then
        echo "    ERROR: Redis ${REDIS_HOST}:${REDIS_PORT} 未就绪，见 logs/redis-${REDIS_PORT}.log" >&2
        exit 1
    fi
fi

# Materialise .qoder/agents/*.md so the qodercli ACP server (when each
# RQ worker pre-boots it lazily on first job) picks up the synced
# subagent prompts. Idempotent: skips when nothing has changed.
if [ -x .venv/bin/python ]; then
    # reviewagent.config reads required env at import time. Source the
    # resolved instance config in the same shell as the python invocation
    # so sync_qoder_agents can import reviewagent.config without RuntimeError.
    (set -a && source "${REVIEWAGENT_ENV_FILE}" && set +a && .venv/bin/python scripts/sync_qoder_agents.py) 2>&1 | tee -a logs/sync_qoder_agents.log
else
    echo "    WARNING: .venv/bin/python not found; skipping sync_qoder_agents"
fi

if ${START_OPENCODE}; then
    screen -dmS revagent-opencode bash -c \
        "exec ${OPENCODE_BIN} serve --port 4096 --hostname 127.0.0.1 >> logs/opencode-4096.log 2>&1"
    echo "    started revagent-opencode (:4096)"
else
    echo "    skipped revagent-opencode (LLM_PROVIDER=${LLM_PROVIDER:-qodercli})"
fi

screen -dmS revagent-web bash -c \
    "set -a && source '${REVIEWAGENT_ENV_FILE}' && set +a && exec .venv/bin/uvicorn reviewagent.main:app --host 0.0.0.0 --port 3000 --log-level info >> logs/server-3000.log 2>&1"
echo "    started revagent-web (:3000)"

for ((worker_index = 1; worker_index <= WORKER_COUNT; worker_index++)); do
    screen -dmS "revagent-worker${worker_index}" bash -c \
        "set -a && source '${REVIEWAGENT_ENV_FILE}' && set +a && PYTHONUNBUFFERED=1 exec .venv/bin/python -u -m rq.cli worker \"\${RQ_QUEUE_NAME}\" --url \"\${REDIS_URL}\" --worker-class \"\${RQ_WORKER_CLASS:-${DEFAULT_RQ_WORKER_CLASS}}\" </dev/null >> logs/worker-w${worker_index}.log 2>&1"
    echo "    started revagent-worker${worker_index} (${RQ_QUEUE_NAME})"
done

screen -dmS revagent-weekly bash -c \
    "set -a && source '${REVIEWAGENT_ENV_FILE}' && set +a && PYTHONUNBUFFERED=1 exec .venv/bin/python -u -m rq.cli worker \"\${RQ_WEEKLY_QUEUE_NAME}\" --url \"\${REDIS_URL}\" --worker-class \"\${RQ_WORKER_CLASS:-${DEFAULT_RQ_WORKER_CLASS}}\" </dev/null >> logs/worker-weekly.log 2>&1"
echo "    started revagent-weekly (${RQ_WEEKLY_QUEUE_NAME})"

sleep 4

echo "==> [3/3] 状态检查"
screen_status="$(screen -ls 2>/dev/null || true)"
if grep -q revagent <<<"${screen_status}"; then
    grep revagent <<<"${screen_status}"
else
    echo "    (无 revagent 会话，启动失败)"
fi
echo ""
curl -s -o /dev/null -m 3 -w "    webhook  :3000 -> HTTP %{http_code}\n" http://127.0.0.1:3000/  || echo "    webhook  :3000 -> DOWN"
if command -v redis-cli >/dev/null 2>&1; then
    redis_reply="$(redis-cli -h "${REDIS_HOST}" -p "${REDIS_PORT}" ping 2>/dev/null || true)"
    echo "    redis    ${REDIS_HOST}:${REDIS_PORT} -> ${redis_reply:-DOWN}"
fi
if ${START_OPENCODE}; then
    curl -s -o /dev/null -m 3 -w "    opencode :4096 -> HTTP %{http_code}\n" http://127.0.0.1:4096/ || echo "    opencode :4096 -> DOWN"
fi
echo ""
echo "==> 完成。查看日志: screen -r revagent-web / tail -f logs/worker-weekly.log"
echo "==> 手动跑周报: .venv/bin/python scripts/weekly_report.py --push"
