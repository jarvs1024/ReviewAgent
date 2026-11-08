#!/usr/bin/env bash
# ReviewAgent 部署前置检查（只读，不改系统状态）。
#
# 用法:
#   sudo bash deploy/preflight.sh            # 检查并打印 ok/warn/fail
#   sudo bash deploy/preflight.sh --strict   # warn 也按 fail 处理
#   PROJECT_DIR=/opt/ReviewAgent SERVICE_USER=review bash deploy/preflight.sh
#
# 退出码: 0 = 无 fail（--strict 下无 warn）; 1 = 有 fail。
# 只读约定: 本脚本不创建目录、不启动服务、不改文件权限；修复动作由 deploy.sh 负责。
set -uo pipefail

PROJECT_DIR="${PROJECT_DIR:-/home/workflow/ReviewAgent}"
DATA_DIR="${REVIEWAGENT_DATA_DIR:-/var/lib/reviewagent}"
ENV_FILE="${ENV_FILE:-${PROJECT_DIR}/config/instance.env}"
VENV="${PROJECT_DIR}/.venv"
SERVICE_USER="${SERVICE_USER:-workflow}"
WEBHOOK_PORT="${WEBHOOK_PORT:-3000}"

STRICT=0
case "${1:-}" in
    --strict) STRICT=1 ;;
    "") ;;
    *) echo "未知参数: $1（支持 --strict）" >&2; exit 2 ;;
esac

FAIL=0
WARN=0
ok() { printf '  [ ok ] %s\n' "$*"; }
warn() {
    printf '  [warn] %s\n' "$*"
    WARN=$((WARN + 1))
}
fail() {
    printf '  [FAIL] %s\n' "$*"
    FAIL=$((FAIL + 1))
}
section() { printf '\n== %s ==\n' "$*"; }

is_root() { [ "$(id -u)" -eq 0 ]; }

# 从 EnvironmentFile 读取键值（不 source，避免 root 执行被部署用户可写的脚本）。
env_value() {
    local key="$1"
    [ -f "${ENV_FILE}" ] || return 0
    sed -n "s/^[[:space:]]*${key}=//p" "${ENV_FILE}" | tail -n 1 \
        | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/" | tr -d '\r'
}

section "系统与运行时"
if [ "$(uname -s)" = "Linux" ]; then
    ok "Linux ($(uname -r))"
else
    warn "非 Linux 主机（$(uname -s)）：systemd / 开机自启检查不适用"
fi

if command -v systemctl >/dev/null 2>&1; then
    ok "systemctl: $(command -v systemctl)"
else
    fail "未找到 systemctl（生产部署需要 systemd）"
fi

if [ -f "${PROJECT_DIR}/reviewagent/main.py" ]; then
    ok "项目代码: ${PROJECT_DIR}"
else
    fail "项目代码不存在: ${PROJECT_DIR}/reviewagent/main.py"
fi

if command -v python3.12 >/dev/null 2>&1; then
    ok "python3.12: $(python3.12 --version 2>&1)"
else
    fail "未找到 python3.12（venv 与依赖安装需要）"
fi

if command -v git >/dev/null 2>&1; then
    ok "git: $(git --version)"
else
    fail "未找到 git（worker 克隆仓库依赖）"
fi

if command -v curl >/dev/null 2>&1; then
    ok "curl: $(command -v curl)"
else
    fail "未找到 curl（GitLab 连通性检查依赖）"
fi

if [ -x "${VENV}/bin/python" ]; then
    ok "venv: ${VENV}"
    if "${VENV}/bin/python" -c 'import rq' >/dev/null 2>&1; then
        ok "rq 已安装 (${VENV}/bin/rq)"
    else
        fail "venv 内缺少 rq：重新执行 deploy.sh 安装依赖"
    fi
else
    fail "venv 不存在: ${VENV}（先执行 deploy.sh）"
fi

section "服务账号与目录"
if id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    ok "服务账号: ${SERVICE_USER} ($(id -u "${SERVICE_USER}"))"
else
    fail "服务账号不存在: ${SERVICE_USER}"
fi

for dir in "${PROJECT_DIR}" "${DATA_DIR}" "${DATA_DIR}/repos" "${DATA_DIR}/weekly_reports" /tmp/reviewagent-worktrees; do
    if [ ! -d "${dir}" ]; then
        fail "目录缺失: ${dir}（deploy.sh 会创建）"
        continue
    fi
    owner="$(stat -c '%U' "${dir}" 2>/dev/null || stat -f '%Su' "${dir}")"
    if [ "${owner}" = "${SERVICE_USER}" ] || [ "${owner}" = "root" ]; then
        if [ "${owner}" = "${SERVICE_USER}" ]; then
            ok "目录属主 ${SERVICE_USER}: ${dir}"
        else
            warn "目录属主为 root: ${dir}（服务进程可能无写权限）"
        fi
    else
        fail "目录属主为 ${owner}，应为 ${SERVICE_USER}: ${dir}"
    fi
done

avail_kb="$(df -Pk "${DATA_DIR}" 2>/dev/null | awk 'NR==2 {print $4}')"
if [ -n "${avail_kb}" ] && [ "${avail_kb}" -ge 2097152 ] 2>/dev/null; then
    ok "磁盘可用 $(awk -v k="${avail_kb}" 'BEGIN {printf "%.1f", k/1048576}') GiB (${DATA_DIR})"
else
    fail "磁盘可用不足 2 GiB (${DATA_DIR}: ${avail_kb:-未知} KB)"
fi

section "实例配置 (${ENV_FILE})"
if [ ! -f "${ENV_FILE}" ]; then
    fail "配置文件不存在：先 cp config/examples/instance.env.example config/instance.env 并填入密钥"
else
    perms="$(stat -c '%a' "${ENV_FILE}" 2>/dev/null || stat -f '%Lp' "${ENV_FILE}")"
    case "${perms}" in
        600|640) ok "配置文件权限 ${perms}" ;;
        *) warn "配置文件权限 ${perms}，建议 chmod 600（文件内含 PAT）" ;;
    esac

    if git -C "${PROJECT_DIR}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        env_rel="${ENV_FILE#"${PROJECT_DIR}"/}"
        if git -C "${PROJECT_DIR}" ls-files --error-unmatch "${env_rel}" >/dev/null 2>&1; then
            fail "${env_rel} 已被 git 跟踪：立即 git rm --cached 并轮换密钥"
        else
            ok "${env_rel} 未被 git 跟踪"
        fi
    fi

    for key in GITLAB_URL GITLAB_PERSONAL_ACCESS_TOKEN GITLAB_WEBHOOK_SECRET; do
        if [ -n "$(env_value "${key}")" ]; then
            ok "${key} 已配置"
        else
            fail "${key} 为空（必填）"
        fi
    done

    gitlab_url="$(env_value GITLAB_URL)"
    case "${gitlab_url}" in
        http://*|https://*) ok "GITLAB_URL 格式: ${gitlab_url%/}" ;;
        "") ;;
        *) fail "GITLAB_URL 需以 http:// 或 https:// 开头: ${gitlab_url}" ;;
    esac

    provider="$(env_value LLM_PROVIDER)"
    provider="${provider:-qodercli}"
    ok "LLM_PROVIDER=${provider}"
    case "${provider}" in
        qoder-sdk)
            if [ -n "$(env_value QODER_SDK_PAT)" ]; then
                ok "QODER_SDK_PAT 已配置"
            else
                fail "LLM_PROVIDER=qoder-sdk 需要 QODER_SDK_PAT"
            fi
            edition="$(env_value QODER_SDK_EDITION)"
            ok "QODER_SDK_EDITION=${edition:-intl}"
            ;;
        qodercli)
            cli_path="$(env_value QODER_SDK_CLI_PATH)"
            if [ -n "${cli_path}" ] && [ -x "${cli_path}" ]; then
                ok "qodercli CLI: ${cli_path}"
            elif command -v "${cli_path:-qodercli}" >/dev/null 2>&1 || command -v qoderclicn >/dev/null 2>&1; then
                ok "qodercli CLI 在 PATH 中"
            else
                fail "LLM_PROVIDER=qodercli 但未找到 qodercli/qoderclicn（或用 QODER_SDK_CLI_PATH 指定）"
            fi
            if [ -d "${PROJECT_DIR}/.qoder/agents" ] && [ -n "$(ls -A "${PROJECT_DIR}/.qoder/agents" 2>/dev/null)" ]; then
                ok ".qoder/agents 已生成"
            else
                warn ".qoder/agents 缺失（deploy.sh 会用 scripts/sync_qoder_agents.py 生成）"
            fi
            ;;
        opencode)
            opencode_url="$(env_value OPENCODE_URL)"
            opencode_url="${opencode_url:-http://127.0.0.1:4096}"
            if curl -fsS --max-time 5 "${opencode_url%/}/global/health" >/dev/null 2>&1 \
                || curl -fsS --max-time 5 "${opencode_url%/}" >/dev/null 2>&1; then
                ok "opencode 可达: ${opencode_url}"
            else
                fail "LLM_PROVIDER=opencode 但 ${opencode_url} 不可达"
            fi
            ;;
        *)
            warn "未识别的 LLM_PROVIDER=${provider}（按第三方协议自行确认）"
            ;;
    esac

    redis_url="$(env_value REDIS_URL)"
    redis_url="${redis_url:-redis://127.0.0.1:6379/0}"
    redis_rest="${redis_url#redis://}"
    redis_rest="${redis_rest#*@}"
    redis_host="${redis_rest%%:*}"
    redis_host="${redis_host%%/*}"
    redis_port=""
    case "${redis_rest}" in
        *:*/*) redis_port="${redis_rest#*:}"; redis_port="${redis_port%%/*}" ;;
        *:*) redis_port="${redis_rest#*:}" ;;
    esac
    redis_host="${redis_host:-127.0.0.1}"
    redis_port="${redis_port:-6379}"
    if command -v redis-cli >/dev/null 2>&1; then
        if [ "$(redis-cli -h "${redis_host}" -p "${redis_port}" ping 2>/dev/null)" = "PONG" ]; then
            ok "Redis 可用: ${redis_host}:${redis_port}"
        else
            fail "Redis 不可达: ${redis_host}:${redis_port}（deploy.sh 可拉起本机实例）"
        fi
    else
        warn "未找到 redis-cli，跳过 Redis 连通性检查（${redis_host}:${redis_port}）"
    fi
fi

section "GitLab API"
gitlab_url="$(env_value GITLAB_URL)"
gitlab_token="$(env_value GITLAB_PERSONAL_ACCESS_TOKEN)"
if [ -n "${gitlab_url}" ] && [ -n "${gitlab_token}" ] && command -v curl >/dev/null 2>&1; then
    user_json="$(curl -fsS --max-time 10 -H "PRIVATE-TOKEN: ${gitlab_token}" \
        "${gitlab_url%/}/api/v4/user" 2>/dev/null)"
    if [ -n "${user_json}" ]; then
        username="$(printf '%s' "${user_json}" | sed -n 's/.*"username":"\([^"]*\)".*/\1/p')"
        ok "GitLab token 有效（user=${username:-unknown}）"
    else
        fail "GitLab API 不可用或 token 无效: ${gitlab_url%/}/api/v4/user"
    fi
else
    warn "缺少 GITLAB_URL / PAT，跳过 GitLab API 检查"
fi

section "Webhook 端口"
if [ -x "${VENV}/bin/uvicorn" ] && command -v curl >/dev/null 2>&1 \
    && curl -fsS --max-time 3 "http://127.0.0.1:${WEBHOOK_PORT}/health" 2>/dev/null | grep -q '"ok"'; then
    ok "端口 ${WEBHOOK_PORT} 已由 ReviewAgent 监听"
elif command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | grep -q ":${WEBHOOK_PORT} "; then
    fail "端口 ${WEBHOOK_PORT} 被其他进程占用且 /health 不是 ReviewAgent"
elif command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:"${WEBHOOK_PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
    fail "端口 ${WEBHOOK_PORT} 被其他进程占用且 /health 不是 ReviewAgent"
else
    ok "端口 ${WEBHOOK_PORT} 空闲"
fi

section "systemd unit 文件"
UNITS=(
    reviewagent-webhook.service
    reviewagent-worker@.service
    reviewagent-weekly-worker.service
    reviewagent-weekly-enqueue.service
    reviewagent-weekly.timer
    reviewagent-reconciler.service
    reviewagent-reconciler.timer
)
for unit in "${UNITS[@]}"; do
    if [ -f "${PROJECT_DIR}/deploy/${unit}" ]; then
        ok "deploy/${unit}"
    else
        fail "缺少 deploy/${unit}"
    fi
done

section "配置与规则装配"
if [ -x "${VENV}/bin/python" ] && [ -f "${ENV_FILE}" ]; then
    run_env=("${VENV}/bin/python")
    runner=()
    if is_root && id -u "${SERVICE_USER}" >/dev/null 2>&1 && command -v runuser >/dev/null 2>&1; then
        runner=(runuser -u "${SERVICE_USER}" --)
    fi
    for sub in "config check" "rules check"; do
        # shellcheck disable=SC2086,SC2068
        if ${runner[@]+"${runner[@]}"} bash -c 'cd "$1" || exit 1; set -a; . "$2"; set +a; shift 2; exec "$@"' \
            _ "${PROJECT_DIR}" "${ENV_FILE}" "${run_env[@]}" -m reviewagent ${sub} >/tmp/reviewagent-preflight.out 2>&1; then
            ok "reviewagent ${sub}"
        else
            fail "reviewagent ${sub}: $(head -n 1 /tmp/reviewagent-preflight.out)"
        fi
    done
    rm -f /tmp/reviewagent-preflight.out
else
    warn "venv 或配置文件缺失，跳过 config/rules check"
fi

printf '\n== 汇总 ==\n'
printf '  fail=%d warn=%d\n' "${FAIL}" "${WARN}"
if [ "${FAIL}" -gt 0 ]; then
    echo "  => 存在阻塞项，修复后重跑 deploy/preflight.sh"
    exit 1
fi
if [ "${STRICT}" -eq 1 ] && [ "${WARN}" -gt 0 ]; then
    echo "  => --strict: warn 视为失败"
    exit 1
fi
echo "  => 前置检查通过"
