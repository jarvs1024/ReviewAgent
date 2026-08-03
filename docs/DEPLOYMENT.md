# 部署文档

> 更新日期：2026-08-03 · 两环境并存（86 服务器 + 本地 v2）

ReviewAgent 目前有**两套并存**的部署目标，互相隔离（与旧 `pr-agent v1` 也完全隔离）。本文档记录两者的真实配置与踩坑。

| 维度 | 86 服务器（生产向 PoC） | 本地 v2（macOS 开发向） |
|---|---|---|
| 主机 | `ops-host`（root@ops-host.internal） | 本机 `/Users/jarvs`（macOS） |
| OS | Anolis OS 23.4（RHEL 兼容） | macOS + Docker Desktop |
| webhook 端口 | `:3000` | `:5052`（前接 socat bridge `:5051`） |
| RQ 队列 | `review` | `review-v2` |
| Redis | `redis://127.0.0.1:6379/1`（testmate-redis 容器） | `redis://127.0.0.1:63790/2`（testmate-redis 容器） |
| opencode | `:4096`，模型 `deepseek-v4-flash` | `:4096`，模型 `minimax/MiniMax-M2.7` |
| GitLab bot | `review-bot`（id=35） | `review-bot-v2`（id=40） |
| 进程管理 | systemd unit | `scripts/run_*.sh` 后台 + socat 容器 |
| firewalld | 放行 3000 / 4096 | Docker 网络（GitLab 容器经 bridge 可达） |

---

## 一、86 服务器部署（生产向）

### 1. 基础依赖
```bash
dnf install -y python3.12 python3.12-devel git
```

### 2. 项目代码 + venv
```bash
mkdir -p /home/workflow/ReviewAgent
# 把仓库同步到 /home/workflow/ReviewAgent
cd /home/workflow/ReviewAgent
python3.12 -m venv .venv
.venv/bin/pip install --no-cache-dir 'urllib3<2'   # 先修 Anolis pip/urllib3 bug
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e ".[dev]"
```

### 3. firewalld 放行（最关键）
```bash
firewall-cmd --zone=public --add-port=3000/tcp --permanent
firewall-cmd --zone=public --add-port=4096/tcp --permanent
firewall-cmd --reload
```

### 4. systemd unit
`/etc/systemd/system/reviewagent-webhook.service` 与 `reviewagent-worker.service`，**PATH 必须含 `/usr/bin`**（否则 worker 找不到 `git`）：
```ini
[Unit]
Description=ReviewAgent Webhook (FastAPI)
After=network.target redis-server.service

[Service]
Type=simple
User=root
WorkingDirectory=/home/workflow/ReviewAgent
Environment="PATH=/home/workflow/ReviewAgent/.venv/bin:/usr/local/bin:/usr/bin:/bin"
EnvironmentFile=/home/workflow/ReviewAgent/.env
ExecStart=/home/workflow/ReviewAgent/.venv/bin/uvicorn reviewagent.main:app --host 0.0.0.0 --port 3000
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```
worker 同理，`ExecStart` 改为：
```
/home/workflow/ReviewAgent/.venv/bin/rq worker review --url redis://127.0.0.1:6379/1
```
```bash
systemctl daemon-reload
systemctl enable --now reviewagent-webhook reviewagent-worker
```

### 5. opencode serve（注意：未进 systemd）
```bash
mkdir -p /root/.config/opencode/agents
# 把 reviewagent/prompts/{describe,improve}.md 同步到 agent 目录（用 scripts/sync_agents.py）
setsid bash -c 'nohup /usr/local/bin/opencode serve --port 4096 --hostname 0.0.0.0 >/var/log/opencode.log 2>&1 &' < /dev/null
curl http://127.0.0.1:4096/global/health   # {"healthy":true,...}
```

### 6. GitLab webhook（GitLab 侧手动）
- URL：`http://ops-host.internal:3000/webhook`
- Secret token：与 `.env` 的 `GITLAB_WEBHOOK_SECRET` 一致
- Trigger：☑ Merge request events  ☑ Comments

---

## 二、本地 v2 部署（macOS）

直接使用仓库内置脚本（已写死 v2 环境变量，含 Redis `:63790/2`、队列 `review-v2`、模型 minimax）：
```bash
bash scripts/run_webhook.sh      # 后台 uvicorn :5052
bash scripts/run_worker.sh       # 同时起主队列 review-v2 + 周报队列 review-v2-weekly 两个 worker（含 OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES）
```

> 一键重启（本地 v2）：`bash scripts/restart_local.sh` 重启 opencode serve + web(:3000) + 主队列(review-v2，3 worker) 与周报队列(review-v2-weekly，1 worker)，配置读自 `.env`（web 用 :3000，与 `run_webhook.sh` 的 :5052 不是同一套本地配置）。

因 macOS Docker Desktop 无法直接 `host.docker.internal:port` 访问宿主进程，需起 socat bridge 让 GitLab 容器可达 `:5051`：
```bash
docker run -d --name reviewagent-bridge \
  --network gitlab-stack_net \
  -p 5051:5051 \
  alpine/socat \
  TCP-LISTEN:5051,fork,reuseaddr TCP:host.docker.internal:5052
```
GitLab webhook URL 配 `http://<host>:5051/webhook`。

opencode agent 文件须与仓库 `prompts/*.md` 同步：
```bash
python scripts/sync_agents.py    # reviewagent/prompts/*.md → ~/.config/opencode/agent/*.md
```

---

## 踩坑全记录（按时间顺序，仍适用）

1. **pip install 失败（urllib3 / Python 3.12 bug）**：Anolis 自带 pip 23.3.1 + urllib3 1.26.x 处理内网 mirror 的 Content-Length 缺失有 bug。先 `pip install 'urllib3<2'`，再升级 pip。
2. **GitLab webhook "No route to host"**：firewalld `public zone` 默认只放行 ssh，3000/4096 被挡。需显式 `firewall-cmd --add-port`。
3. **worker "FileNotFoundError: git"**：systemd unit 的 `Environment="PATH=.../.venv/bin"` 覆盖默认 PATH 导致 `/usr/bin/git` 找不到。PATH 须含 `/usr/bin`。
4. **git clone 重定向到登录页**：`gitlab_url` 是 `http://` 时 `replace("https://"...)` 不匹配。改由 `GitLabClient.get_project_git_url(project_id)` 用 `path_with_namespace` 拼带 token 的 URL（日志用 `_scrub_token()` 脱敏）。
5. **opencode `run --format json` 卡死**：启动时强制 fetch `models.dev`，离线环境超时。改用 **HTTP API**（`POST /session` + `/session/:id/message`），serve 启动时已加载 provider，不需要公网元数据。
6. **file part 缺 `url` 字段**：opencode file part 用 `url`（data URL），非 `content`。但大文件 data URL 会触发 Server 500，故当前实现把 diff **内联进 prompt 文本**（`opencode_max_diff_chars` 截断）。
7. **bash heredoc 转义**：写 opencode.jsonc 用 `<<'JSONEOF'` 单引号 heredoc，API key 用 base64 避免 shell 解析。

---

## 验证清单

```bash
# 1. 服务健康
curl http://127.0.0.1:3000/health        # 86
curl http://127.0.0.1:5052/health        # 本地 v2
curl http://127.0.0.1:4096/global/health  # opencode

# 2. Redis
python -c 'import redis; print(redis.from_url("redis://127.0.0.1:6379/1").ping())'

# 3. SQLite
ls -la data/telemetry.db
python -c "import sqlite3; print(sqlite3.connect('data/telemetry.db').execute(\"SELECT name FROM sqlite_master WHERE type='table'\").fetchall())"
# 期望：mr_activity / review_runs / suggestion_actions / suggestions

# 4. Telemetry 累计量
curl http://127.0.0.1:3000/api/v1/telemetry/health
```

---

## 备份 / 恢复

需备份：
- `data/` — SQLite + bare repos（核心）
- `.env` — 凭证（高敏）
- `~/.config/opencode/opencode.jsonc` / `agent/` — opencode 凭证与 agent 文件（高敏）

不需备份（可重建）：`/tmp/reviewagent-worktrees/`（tmpfs）、systemd unit、日志。

---

## ⚠️ 安全提示

- `scripts/run_webhook.sh` / `scripts/run_worker.sh` 当前**硬编码了真实 GitLab PAT 与 webhook secret 明文**。生产前务必改为从 `.env` 读取并轮换密钥（参考 `run_weekly_report.sh` 的 `source .env`）。
- 历史文档里记录过的旧 secret（DeepSeek key、`glpat-...`、`414d0c...`）均已废弃，需轮换。
- 86 服务器 opencode 未进 systemd，重启会丢，需补 unit。
