#!/usr/bin/env bash
# ReviewAgent periodic reconciler daemon.
# 用 launchd StartInterval=60 调起, 每 60s 跑一次 reconcile_open_mrs().
#
# 为什么不放 RQ worker 里:
#   - RQ worker 是 process-based, 长期重复入队会污染队列.
#   - 独立脚本简单, 失败隔离, launchd 自动重启.
#
# 用法 (手动跑一次):
#   bash scripts/run_reconciler.sh --project-id 34
#
# 安装 launchd agent (系统级, 60s 一次):
#   cp scripts/com.jarvs.reviewagent.reconciler.plist /Users/jarvs/Library/LaunchAgents/
#   launchctl load /Users/jarvs/Library/LaunchAgents/com.jarvs.reviewagent.reconciler.plist
set -eo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_DIR}"

# shellcheck source=scripts/_env.sh
source "${PROJECT_DIR}/scripts/_env.sh"

exec .venv/bin/python -m reviewagent.reconciler.loop "$@"
