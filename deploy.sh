#!/usr/bin/env bash
# ReviewAgent 部署脚本 — 在 86 服务器 /home/workflow 目录执行
# 用法: bash deploy.sh

set -euo pipefail

PROJECT_DIR="/home/workflow"

echo "==> [1/6] 检查项目目录"
mkdir -p "${PROJECT_DIR}"
cd "${PROJECT_DIR}"

if [ ! -f "${PROJECT_DIR}/reviewagent/main.py" ]; then
    echo "==> 项目代码不存在，请先把 D:\\Code\\ReviewAgent 内容同步到 ${PROJECT_DIR}"
    echo "    推荐: scp -r D:\\Code\\ReviewAgent\\* deploy@ops-host.internal:${PROJECT_DIR}/"
    exit 1
fi

echo "==> [2/6] 创建 Python 虚拟环境"
if [ ! -d ".venv" ]; then
    python3.12 -m venv .venv
fi
source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[dev]"

echo "==> [3/7] 启动 Redis"
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

echo "==> [4/7] 检查 git（worker 需要）"
if ! command -v git >/dev/null 2>&1; then
    echo "git 未安装；用包管理器装"
    if command -v dnf >/dev/null 2>&1; then
        dnf install -y git
    elif command -v apt >/dev/null 2>&1; then
        apt install -y git
    elif command -v yum >/dev/null 2>&1; then
        yum install -y git
    else
        echo "未找到包管理器，请手动安装 git 后重试"
        exit 1
    fi
fi
git --version

echo "==> [5/7] 准备 .env（如不存在）"
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo "请编辑 .env 填入 GITLAB_URL / GITLAB_PERSONAL_ACCESS_TOKEN / GITLAB_WEBHOOK_SECRET"
    echo "OPENCODE_URL 默认指向本地 4096 端口"
    exit 0
fi

echo "==> [5/6] 确保数据目录存在"
mkdir -p data/repos data/weekly_reports
mkdir -p /tmp/reviewagent-worktrees
chmod 700 data/repos

echo "==> [6/6] 安装 systemd unit"
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    SUDO="sudo"
fi

${SUDO} tee /etc/systemd/system/reviewagent-webhook.service >/dev/null <<'EOF'
[Unit]
Description=ReviewAgent Webhook (FastAPI)
After=network.target redis-server.service

[Service]
Type=simple
User=workflow
WorkingDirectory=/home/workflow
Environment="PATH=/home/workflow/.venv/bin:/usr/local/bin:/usr/bin:/bin"
EnvironmentFile=/home/workflow/.env
ExecStart=/home/workflow/.venv/bin/uvicorn reviewagent.main:app --host 0.0.0.0 --port 3000
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

${SUDO} tee /etc/systemd/system/reviewagent-worker.service >/dev/null <<'EOF'
[Unit]
Description=ReviewAgent Worker (RQ)
After=network.target redis-server.service

[Service]
Type=simple
User=workflow
WorkingDirectory=/home/workflow
Environment="PATH=/home/workflow/.venv/bin:/usr/local/bin:/usr/bin:/bin"
EnvironmentFile=/home/workflow/.env
ExecStart=/home/workflow/.venv/bin/rq worker review --url redis://127.0.0.1:6379/0
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

${SUDO} tee /etc/systemd/system/reviewagent-weekly-worker.service >/dev/null <<'EOF'
[Unit]
Description=ReviewAgent Weekly Report Worker (RQ, isolated queue)
After=network.target redis-server.service

[Service]
Type=simple
User=workflow
WorkingDirectory=/home/workflow
Environment="PATH=/home/workflow/.venv/bin:/usr/local/bin:/usr/bin:/bin"
EnvironmentFile=/home/workflow/.env
# 周报专用队列, 与主 review 队列物理隔离, 互不阻塞 (共享 Redis / opencode / SQLite)
ExecStart=/home/workflow/.venv/bin/rq worker review-weekly --url redis://127.0.0.1:6379/0
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

${SUDO} systemctl daemon-reload
${SUDO} systemctl enable --now reviewagent-webhook reviewagent-worker reviewagent-weekly-worker

# ---- 周报定时触发 (systemd timer, 用户可自定义时间) ----
${SUDO} tee /etc/systemd/system/reviewagent-weekly-enqueue.service >/dev/null <<'EOF'
[Unit]
Description=ReviewAgent Weekly Report Enqueue (oneshot)
After=network.target redis-server.service

[Service]
Type=oneshot
User=workflow
WorkingDirectory=/home/workflow
Environment="PATH=/home/workflow/.venv/bin:/usr/local/bin:/usr/bin:/bin"
EnvironmentFile=/home/workflow/.env
ExecStart=/home/workflow/.venv/bin/python /home/workflow/scripts/weekly_report.py --enqueue
StandardOutput=journal
StandardError=journal
EOF

${SUDO} tee /etc/systemd/system/reviewagent-weekly.timer >/dev/null <<'EOF'
[Unit]
Description=ReviewAgent Weekly Report Timer
Requires=reviewagent-weekly-enqueue.service

[Timer]
# 默认每周一 09:00 触发; 用户可通过 systemctl edit reviewagent-weekly.timer 覆盖
OnCalendar=Mon 09:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

${SUDO} systemctl daemon-reload
${SUDO} systemctl enable --now reviewagent-weekly.timer
sleep 2
${SUDO} systemctl status reviewagent-webhook --no-pager
${SUDO} systemctl status reviewagent-worker --no-pager

echo ""
echo "==> 部署完成！"
echo "  webhook health:  curl http://127.0.0.1:3000/health"
echo "  webhook logs:    sudo journalctl -u reviewagent-webhook -f"
echo "  worker logs:     sudo journalctl -u reviewagent-worker -f"
echo "  周报 worker:     sudo journalctl -u reviewagent-weekly-worker -f"
echo "  周报定时:        sudo systemctl list-timers reviewagent-weekly.timer"
echo "  自定义时间:      sudo systemctl edit reviewagent-weekly.timer (改 OnCalendar)"