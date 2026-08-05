#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/Users/jarvs/ReviewAgent}"
cd "${PROJECT_DIR}"
set -a
source .env
set +a

: "${REDIS_URL:?REDIS_URL must be set in .env}"
: "${RQ_QUEUE_NAME:?RQ_QUEUE_NAME must be set in .env}"
RQ_WEEKLY_QUEUE_NAME="${RQ_WEEKLY_QUEUE_NAME:-${RQ_QUEUE_NAME}-weekly}"
RQ_WORKER_CLASS="${RQ_WORKER_CLASS:-reviewagent.workers.rq_worker.ReviewAgentSpawnWorker}"

# 主 review worker (improve/describe/suggestion) — 后台运行, 不被周报拖慢
"${PROJECT_DIR}/.venv/bin/rq" worker "${RQ_QUEUE_NAME}" --url "${REDIS_URL}" --worker-class "${RQ_WORKER_CLASS}" </dev/null &
MAIN_PID=$!
# 周报 worker — 独立进程, 与上面互不阻塞 (共享 Redis / opencode / SQLite 资源)
"${PROJECT_DIR}/.venv/bin/rq" worker "${RQ_WEEKLY_QUEUE_NAME}" --url "${REDIS_URL}" --worker-class "${RQ_WORKER_CLASS}" </dev/null &
WEEKLY_PID=$!
trap 'kill $MAIN_PID $WEEKLY_PID 2>/dev/null' EXIT INT TERM
wait
