#!/usr/bin/env bash
set -e
cd /Users/jarvs/ReviewAgent
export GITLAB_URL='http://127.0.0.1:8929'
export GITLAB_PERSONAL_ACCESS_TOKEN='glpat-rv2-kAf7nQp3xY8mLsW4vB2jRt9dHc6gZ1eP'
export GITLAB_WEBHOOK_SECRET='42abb7d89ac3b177492706f9bc94bc56'
export GITLAB_BOT_USERNAME='review-bot-v2'
export OPENCODE_URL='http://127.0.0.1:4096'
export OPENCODE_MODEL='minimax/MiniMax-M2.7'
export REDIS_URL='redis://127.0.0.1:63790/2'
export RQ_QUEUE_NAME='review-v2'
export RQ_WEEKLY_QUEUE_NAME="${RQ_QUEUE_NAME}-weekly"   # 周报独立队列, 与 review 命令队列隔离
export RQ_WORKER_TIMEOUT='600'
export REVIEWAGENT_DATA_DIR='/Users/jarvs/ReviewAgent/data'
# macOS fork safety - avoid NSNumber initialize crash
export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES

# 主 review worker (improve/describe/suggestion) — 后台运行, 不被周报拖慢
/Users/jarvs/ReviewAgent/.venv/bin/rq worker "${RQ_QUEUE_NAME}" --url "${REDIS_URL}" &
MAIN_PID=$!
# 周报 worker — 独立进程, 与上面互不阻塞 (共享 Redis / opencode / SQLite 资源)
/Users/jarvs/ReviewAgent/.venv/bin/rq worker "${RQ_WEEKLY_QUEUE_NAME}" --url "${REDIS_URL}" &
WEEKLY_PID=$!
trap 'kill $MAIN_PID $WEEKLY_PID 2>/dev/null' EXIT INT TERM
wait
