#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/Users/jarvs/ReviewAgent}"
cd "${PROJECT_DIR}"
# shellcheck source=scripts/_env.sh
source "${PROJECT_DIR}/scripts/_env.sh"

exec "${PROJECT_DIR}/.venv/bin/uvicorn" reviewagent.main:app --host 0.0.0.0 --port 5052 --log-level info
