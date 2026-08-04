#!/usr/bin/env bash
# 本地重启 ReviewAgent 全部常驻服务（screen 托管）
#
# 用法（在自己的终端执行，不要在 agent 沙箱里跑）:
#     bash scripts/restart_local.sh
#
# 托管的会话:
#   revagent-opencode   opencode serve :4096      （LLM 后端，检视/周报都依赖）
#   revagent-web        uvicorn :3000             （webhook + telemetry API）
#   revagent-worker1~3  rq worker review-v2       （MR 检视主队列）
#   revagent-weekly     rq worker review-v2-weekly（周报独立队列）
set -uo pipefail

PROJECT_DIR="/Users/jarvs/ReviewAgent"
OPENCODE_BIN="${HOME}/.opencode/bin/opencode"
SESSIONS=(revagent-opencode revagent-web revagent-worker1 revagent-worker2 revagent-worker3 revagent-weekly)

cd "${PROJECT_DIR}"
mkdir -p logs

echo "==> [1/3] 停止旧会话"
for s in "${SESSIONS[@]}"; do
    if screen -ls 2>/dev/null | grep -q "\.${s}[[:space:]]"; then
        screen -S "${s}" -X quit 2>/dev/null && echo "    killed ${s}"
    fi
done
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
    "exec ${OPENCODE_BIN} serve --port 4096 2>&1 | tee -a logs/opencode-4096.log"
echo "    started revagent-opencode (:4096)"

screen -dmS revagent-web bash -c \
    'set -a && source .env && set +a && exec .venv/bin/uvicorn reviewagent.main:app --host 0.0.0.0 --port 3000 --log-level info 2>&1 | tee -a logs/server-3000.log'
echo "    started revagent-web (:3000)"

for i in 1 2 3; do
    screen -dmS "revagent-worker${i}" bash -c \
        "set -a && source .env && set +a && export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES && exec .venv/bin/rq worker review-v2 --url \"\${REDIS_URL}\" 2>&1 | tee -a logs/worker-w${i}.log"
    echo "    started revagent-worker${i} (review-v2)"
done

screen -dmS revagent-weekly bash -c \
    'set -a && source .env && set +a && export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES && exec .venv/bin/rq worker review-v2-weekly --url "${REDIS_URL}" 2>&1 | tee -a logs/worker-weekly.log'
echo "    started revagent-weekly (review-v2-weekly)"

sleep 4

echo "==> [3/3] 状态检查"
screen -ls 2>/dev/null | grep revagent || echo "    (无 revagent 会话，启动失败)"
echo ""
curl -s -o /dev/null -m 3 -w "    webhook  :3000 -> HTTP %{http_code}\n" http://127.0.0.1:3000/  || echo "    webhook  :3000 -> DOWN"
curl -s -o /dev/null -m 3 -w "    opencode :4096 -> HTTP %{http_code}\n" http://127.0.0.1:4096/ || echo "    opencode :4096 -> DOWN"
echo ""
echo "==> 完成。查看日志: screen -r revagent-web / tail -f logs/worker-weekly.log"
echo "==> 手动跑周报: .venv/bin/python scripts/weekly_report.py --push"
