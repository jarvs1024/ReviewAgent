#!/usr/bin/env bash
# ReviewAgent 部署脚本 — 在 86 服务器执行
# 用法:
#   1) 把代码同步到 ${PROJECT_DIR} (默认 /home/workflow/ReviewAgent)
#   2) sudo bash deploy.sh
#
# 重要: 各 systemd unit 的"事实源"是 deploy/*.service 和 deploy/*.timer;
# 本脚本不再内嵌 heredoc，避免与 deploy/ 目录下的文件不一致.
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/home/workflow/ReviewAgent}"

echo "==> [1/6] 检查项目目录 (${PROJECT_DIR})"
mkdir -p "${PROJECT_DIR}"
cd "${PROJECT_DIR}"

if [ ! -f "${PROJECT_DIR}/reviewagent/main.py" ]; then
    echo "==> 项目代码不存在，请先把仓库内容同步到 ${PROJECT_DIR}"
    echo "    推荐: rsync -av --exclude='.venv' --exclude='data' \\"
    echo "          ./ deploy@<host>:${PROJECT_DIR}/"
    exit 1
fi

echo "==> [2/6] 创建 Python 虚拟环境"
if [ ! -d ".venv" ]; then
    python3.12 -m venv .venv
fi
source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[dev]"

echo "==> [3/6] 启动 Redis (如未跑)"
if ! redis-cli ping >/dev/null 2>&1; then
    if command -v redis-server >/dev/null 2>&1; then
        redis-server --daemonize yes --bind 127.0.0.1 --port 6379
        sleep 1
    else
        echo "未找到 redis-server；请先 apt install redis-server 或 docker run -d -p 6379:6379 redis:7-alpine"
        exit 1
    fi
fi
redis-cli ping

# git 是 worker clone repos 的硬依赖
if ! command -v git >/dev/null 2>&1; then
    if command -v dnf >/dev/null 2>&1; then
        dnf install -y git
    elif command -v apt >/dev/null 2>&1; then
        apt install -y git
    elif command -v yum >/dev/null 2>&1; then
        yum install -y git
    else
        echo "未找到包管理器，请手动安装 git"
        exit 1
    fi
fi
git --version

echo "==> [4/6] 准备 .env (如不存在)"
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo "请编辑 .env 填入 GITLAB_URL / GITLAB_PERSONAL_ACCESS_TOKEN / GITLAB_WEBHOOK_SECRET"
    echo "OPENCODE_URL 默认指向本地 4096 端口"
    echo "其余可用默认值"
    exit 0
fi

echo "==> [5/6] 数据目录"
mkdir -p data/repos data/weekly_reports
mkdir -p /tmp/reviewagent-worktrees
chmod 700 data/repos

echo "==> [6/6] 安装 systemd units (源: deploy/*.service / deploy/*.timer)"
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    SUDO="sudo"
fi

# deploy/ 目录下的文件就是 systemd unit 的 source of truth；脚本只负责 cp + 改权限
${SUDO} install -m 0644 \
    deploy/reviewagent-weekly-enqueue.service \
    deploy/reviewagent-weekly-worker.service \
    /etc/systemd/system/

${SUDO} install -m 0644 \
    deploy/reviewagent-weekly.timer \
    /etc/systemd/system/

# reviewagent-webhook.service 与 reviewagent-worker.service 历史上写在 deploy.sh heredoc 里;
# 现将它们作为 deploy/ 目录下的独立文件维护 (与 weekly-* 风格一致)，避免再次漂移.
# 若尚未补全，提示用户提交相关文件后再 deploy.
for unit in reviewagent-webhook.service reviewagent-worker.service; do
    if [ ! -f "deploy/${unit}" ]; then
        echo "==> 缺少 deploy/${unit}, 跳过安装 (请先补齐该文件再 deploy)"
        MISSING_DEPLOY_UNITS+=( "${unit}" )
    else
        ${SUDO} install -m 0644 "deploy/${unit}" /etc/systemd/system/
    fi
done

${SUDO} systemctl daemon-reload

# 启停 webhook / worker / weekly-worker / weekly.timer
ENABLE_UNITS=( reviewagent-weekly-worker.service reviewagent-weekly.timer )
for unit in "${ENABLE_UNITS[@]}"; do
    ${SUDO} systemctl enable --now "${unit}" || echo "  (启用 ${unit} 失败，跳过)"
done

# webhook + worker 是可选 (deploy/ 还没补齐)
for unit in reviewagent-webhook.service reviewagent-worker.service; do
    if [ ! -f "/etc/systemd/system/${unit}" ]; then
        continue
    fi
    ${SUDO} systemctl enable --now "${unit}" || echo "  (启用 ${unit} 失败，跳过)"
done

sleep 2
${SUDO} systemctl status reviewagent-webhook --no-pager 2>/dev/null || true
${SUDO} systemctl status reviewagent-worker --no-pager 2>/dev/null || true

echo ""
echo "==> 部署完成！"
if [[ "${#MISSING_DEPLOY_UNITS[@]:-0}" -gt 0 ]]; then
    echo "==> 警告: 以下 deploy/*.service 文件缺失，请补齐后重新跑 deploy.sh:"
    printf '    - deploy/%s\n' "${MISSING_DEPLOY_UNITS[@]}"
fi
echo "  webhook health:  curl http://127.0.0.1:3000/health"
echo "  webhook logs:    sudo journalctl -u reviewagent-webhook -f"
echo "  worker logs:     sudo journalctl -u reviewagent-worker -f"
echo "  周报 worker:     sudo journalctl -u reviewagent-weekly-worker -f"
echo "  周报定时:        sudo systemctl list-timers reviewagent-weekly.timer"
echo "  自定义周报时间:  sudo systemctl edit reviewagent-weekly.timer (覆盖 OnCalendar)"
