#!/usr/bin/env bash
# 本地重启 ReviewAgent 全部常驻服务（screen 托管）
#
# 用法（在自己的终端执行，不要在 agent 沙箱里跑）:
#     bash scripts/restart_local.sh
#
# 托管的会话:
#   revagent-opencode   opencode serve :4096      （LLM 后端，检视/周报都依赖）
#   revagent-web        uvicorn :3000             （webhook + telemetry API）
#   revagent-workerN    rq worker review-v2       （MR 检视主队列）
#   revagent-weekly     rq worker review-v2-weekly（周报独立队列）
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/Users/jarvs/ReviewAgent}"
OPENCODE_BIN="${OPENCODE_BIN:-${HOME}/.opencode/bin/opencode}"
DEFAULT_RQ_WORKER_CLASS="reviewagent.workers.rq_worker.ReviewAgentSpawnWorker"
SESSIONS=(revagent-opencode revagent-web revagent-weekly)

cd "${PROJECT_DIR}"
mkdir -p logs
set -a
source .env
set +a
WORKER_COUNT="${RQ_WORKER_COUNT:-3}"

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
    done < <(pgrep -f -- "${PROJECT_DIR}/.venv/bin/rq worker" || true)
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
terminate_matching "${PROJECT_DIR}/.venv/bin/rq worker"
terminate_matching "${PROJECT_DIR}/.venv/bin/uvicorn reviewagent.main:app"
terminate_matching "${OPENCODE_BIN} serve --port 4096"
sleep 2

echo "==> [2/3] 启动服务"

# Materialise .qoder/agents/*.md so the qodercli ACP server (when each
# RQ worker pre-boots it lazily on first job) picks up the synced
# subagent prompts. Idempotent: skips when nothing has changed.
if [ -x .venv/bin/python ]; then
    # reviewagent.config reads required env at import time. Source .env
    # in the same shell as the python invocation so sync_qoder_agents can
    # import reviewagent.config without RuntimeError.
    (set -a && source .env && set +a && .venv/bin/python scripts/sync_qoder_agents.py) 2>&1 | tee -a logs/sync_qoder_agents.log
else
    echo "    WARNING: .venv/bin/python not found; skipping sync_qoder_agents"
fi

screen -dmS revagent-opencode bash -c \
    "exec ${OPENCODE_BIN} serve --port 4096 --hostname 127.0.0.1 >> logs/opencode-4096.log 2>&1"
echo "    started revagent-opencode (:4096)"

screen -dmS revagent-web bash -c \
    'set -a && source .env && set +a && exec .venv/bin/uvicorn reviewagent.main:app --host 0.0.0.0 --port 3000 --log-level info >> logs/server-3000.log 2>&1'
echo "    started revagent-web (:3000)"

for ((worker_index = 1; worker_index <= WORKER_COUNT; worker_index++)); do
    screen -dmS "revagent-worker${worker_index}" bash -c \
        "set -a && source .env && set +a && exec .venv/bin/rq worker \"\${RQ_QUEUE_NAME}\" --url \"\${REDIS_URL}\" --worker-class \"\${RQ_WORKER_CLASS:-${DEFAULT_RQ_WORKER_CLASS}}\" </dev/null >> logs/worker-w${worker_index}.log 2>&1"
    echo "    started revagent-worker${worker_index} (${RQ_QUEUE_NAME})"
done

screen -dmS revagent-weekly bash -c \
    "set -a && source .env && set +a && exec .venv/bin/rq worker \"\${RQ_WEEKLY_QUEUE_NAME}\" --url \"\${REDIS_URL}\" --worker-class \"\${RQ_WORKER_CLASS:-${DEFAULT_RQ_WORKER_CLASS}}\" </dev/null >> logs/worker-weekly.log 2>&1"
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
curl -s -o /dev/null -m 3 -w "    opencode :4096 -> HTTP %{http_code}\n" http://127.0.0.1:4096/ || echo "    opencode :4096 -> DOWN"
echo ""
echo "==> 完成。查看日志: screen -r revagent-web / tail -f logs/worker-weekly.log"
echo "==> 手动跑周报: .venv/bin/python scripts/weekly_report.py --push"
