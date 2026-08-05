#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/Users/jarvs/ReviewAgent}"
cd "${PROJECT_DIR}"
set -a
source .env
set +a

exec "${PROJECT_DIR}/.venv/bin/uvicorn" reviewagent.main:app --host 0.0.0.0 --port 5052 --log-level info
