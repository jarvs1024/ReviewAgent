#!/usr/bin/env bash
# ReviewAgent 周报 cron 启动脚本.
# 用法: scripts/run_weekly_report.sh [extra args passed to weekly_report.py]
#   WEEKLY_PUSH=true  启用真实推送
set -eo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/.."

# 注入 env (如果存在 .env)
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

PUSH_FLAG=""
if [ "${WEEKLY_PUSH:-false}" = "true" ]; then
  PUSH_FLAG="--push"
fi

# 透传额外参数
exec .venv/bin/python scripts/weekly_report.py $PUSH_FLAG "$@"
