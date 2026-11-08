#!/usr/bin/env bash
# 统一的实例配置加载器（被 scripts/*.sh source）。
#
# 查找顺序：
#   1. REVIEWAGENT_ENV_FILE 显式指定
#   2. ${PROJECT_DIR}/config/instance.env（新位置）
#   3. ${PROJECT_DIR}/.env（旧位置，兼容尚未迁移的部署）
#
# 导出 REVIEWAGENT_ENV_FILE，供 restart_local.sh 的子 shell 复用同一份配置。

if [[ -z "${PROJECT_DIR:-}" ]]; then
    echo "scripts/_env.sh: PROJECT_DIR must be set before sourcing" >&2
    return 1
fi

if [[ -z "${REVIEWAGENT_ENV_FILE:-}" ]]; then
    if [[ -f "${PROJECT_DIR}/config/instance.env" ]]; then
        REVIEWAGENT_ENV_FILE="${PROJECT_DIR}/config/instance.env"
    elif [[ -f "${PROJECT_DIR}/.env" ]]; then
        REVIEWAGENT_ENV_FILE="${PROJECT_DIR}/.env"
    fi
fi
export REVIEWAGENT_ENV_FILE

if [[ -n "${REVIEWAGENT_ENV_FILE}" && -f "${REVIEWAGENT_ENV_FILE}" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "${REVIEWAGENT_ENV_FILE}"
    set +a
fi
