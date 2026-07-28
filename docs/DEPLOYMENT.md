# 86 服务器部署全记录

> 更新日期：2026-07-28 · PoC 部署 + 端到端跑通

## 概览

| 项 | 值 |
|---|---|
| 主机 | `ops-host` (ssh-remote alias) → `root@ops-host.internal:22` |
| OS | Anolis OS 23.4（RHEL 兼容） |
| GitLab 实例 | `http://gitlab.internal`（10.20.27.x 子网，与 86 不同段） |
| Python | 3.12.13（dnf install） |
| Redis | 复用 `testmate-redis` Docker 容器 db=1 |
| opencode | `/usr/local/bin/opencode v1.15.10`，deepseek provider |
| ReviewAgent webhook | systemd `reviewagent-webhook.service`，监听 `0.0.0.0:3000` |
| ReviewAgent worker | systemd `reviewagent-worker.service`，RQ `review` queue |
| 数据目录 | `/home/workflow/data/`（bind）· `/tmp/reviewagent-worktrees/`（tmpfs） |

---

## 部署步骤（PoC 实测顺序）

```bash
# 1. 基础依赖
dnf install -y python3.12 python3.12-devel git

# 2. 项目代码（从本地 scp / rsync）
mkdir -p /home/workflow/ReviewAgent
# ... 把 D:\Code\ReviewAgent 内容传过来 ...

# 3. Python venv + 依赖
cd /home/workflow/ReviewAgent
python3.12 -m venv .venv
.venv/bin/pip install --upgrade pip   # 必须先升！Anolis 默认 pip 23.3.1 有 urllib3 bug
.venv/bin/pip install -e ".[dev]"

# 4. firewalld 放行端口（**最关键**——不开放外部永远调不到）
firewall-cmd --zone=public --add-port=3000/tcp --permanent
firewall-cmd --zone=public --add-port=4096/tcp --permanent
firewall-cmd --reload

# 5. systemd unit（PATH 必须含 /usr/bin，否则 worker 找不到 git）
cat > /etc/systemd/system/reviewagent-webhook.service <<'EOF'
[Unit]
Description=ReviewAgent Webhook (FastAPI)
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/home/workflow/ReviewAgent
Environment="PATH=/home/workflow/ReviewAgent/.venv/bin:/usr/local/bin:/usr/bin:/bin"
EnvironmentFile=/home/workflow/ReviewAgent/.env
ExecStart=/home/workflow/ReviewAgent/.venv/bin/uvicorn reviewagent.main:app --host 0.0.0.0 --port 3000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
# (worker service 类似，ExecStart 改为 rq worker review)
systemctl daemon-reload
systemctl enable --now reviewagent-webhook reviewagent-worker

# 6. opencode 配置（**绕开 models.dev 必查**——必须用 HTTP API 路径）
mkdir -p /root/.config/opencode/agents
cp /home/workflow/ReviewAgent/reviewagent/prompts/describe.md /root/.config/opencode/agents/pr-describer.md

cat > /root/.config/opencode/opencode.jsonc <<'JSONEOF'
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "deepseek": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "DeepSeek",
      "options": {
        "baseURL": "https://api.deepseek.com/v1",
        "apiKey": "sk-YOUR_DEEPSEEK_KEY"
      },
      "models": {
        "deepseek-chat":     { "name": "DeepSeek Chat" },
        "deepseek-reasoner": { "name": "DeepSeek Reasoner" },
        "deepseek-v4-flash": { "name": "DeepSeek v4 Flash" }
      }
    }
  }
}
JSONEOF
chmod 600 /root/.config/opencode/opencode.jsonc

# 7. 起 opencode serve（用 setsid 完全脱离 ssh session）
cd /root
setsid bash -c 'nohup /usr/local/bin/opencode serve --port 4096 --hostname 0.0.0.0 >/var/log/opencode.log 2>&1 &' < /dev/null
sleep 5
curl http://127.0.0.1:4096/global/health   # 期望 {"healthy":true,"version":"1.15.10"}

# 8. GitLab webhook 配置（GitLab 那侧手动）
#    URL: http://ops-host.internal:3000/webhook
#    Secret token: 与 .env GITLAB_WEBHOOK_SECRET 一致
#    Trigger: Merge request events + Comments
```

---

## 踩坑全记录（按时间顺序）

### 坑 1：pip install 失败 — urllib3 / Python 3.12 bug

**症状**：`TypeError: '>=' not supported between instances of 'int' and 'NoneType'`

**根因**：Anolis OS 自带 pip 23.3.1 + urllib3 1.26.x 处理内网 mirror 的 Content-Length 缺失有 bug

**解决**：
```bash
.venv/bin/pip install --no-cache-dir 'urllib3<2'   # 先装 urllib3 1.26.20
.venv/bin/pip cache purge
.venv/bin/pip install --upgrade pip                 # 升到 26.x
.venv/bin/pip install -e ".[dev]"
```

### 坑 2：GitLab webhook 报 "No route to host"

**症状**：GitLab (gitlab.internal) → 86 (ops-host.internal:3000) 完全不通

**根因**：firewalld `public zone` 默认只放行 `dhcpv6-client` + `ssh`，**3000/4096 被挡**

**诊断**：
```bash
firewall-cmd --zone=public --query-port=3000/tcp
# 期望 YES，但实际 no
ss -tlnp | grep :3000
# uvicorn 进程在监听
curl http://127.0.0.1:3000/health  # 本地 OK
# 结论：loopback OK，远程被 firewalld 挡
```

**解决**：
```bash
firewall-cmd --zone=public --add-port=3000/tcp --permanent
firewall-cmd --zone=public --add-port=4096/tcp --permanent
firewall-cmd --reload
```

**为什么 pr-agent 之前能跑**：pr-agent Docker 容器启动时 Docker daemon 自动注册了端口；裸 uvicorn 进程不会自动注册。

### 坑 3：worker 报 "FileNotFoundError: 'git'"

**症状**：worker 调 `git clone --bare` 失败，`No such file or directory: 'git'`

**根因**：systemd unit 写 `Environment="PATH=/home/workflow/ReviewAgent/.venv/bin"`，**完全覆盖**默认 PATH，导致 `/usr/bin/git` 找不到

**解决**：扩展 systemd unit 的 PATH：
```ini
Environment="PATH=/home/workflow/ReviewAgent/.venv/bin:/usr/local/bin:/usr/bin:/bin"
```

### 坑 4：git clone 重定向到登录页

**症状**：
```
git clone --bare failed: fatal: unable to update url base from redirection:
  asked for: http://gitlab.internal/info/refs?service=git-upload-pack
   redirect: http://gitlab.internal/users/sign_in
```

**根因**：`workspace.py` 用 `gitlab_url.replace("https://", f"https://oauth2:{pat}@")` 构造 URL，但您的 GitLab 是 `http://`，replace 不匹配，URL 没带 token

**解决**：新增 `GitLabClient.get_project_git_url(project_id)`，直接通过 GitLab API 拿 `path_with_namespace` 然后拼带 token 的 URL：
```python
def get_project_git_url(self, project_id: int) -> str:
    project = self._gl.projects.get(project_id)
    path = getattr(project, "path_with_namespace", None) or str(project_id)
    base = self._gl.url.rstrip("/")
    scheme = "https" if base.startswith("https://") else "http"
    host = base[len(f"{scheme}://"):]
    return f"{scheme}://oauth2:{self._gl.private_token}@{host}/{path}.git"
```

URL 写入日志前用 `_scrub_token()` 脱敏。

### 坑 5：opencode `run --format json` 启动卡死

**症状**：`Failed to fetch models.dev` 10 秒超时，agent 不产出任何输出

**根因**：opencode 1.15.10 启动时强制 fetch `https://models.dev/api.json`（公网元数据源），86 network_isolated 阻断

**解决（两个层面）**：

1. **配置 provider**（避免 model lookup 失败）：
   ```jsonc
   // /root/.config/opencode/opencode.jsonc
   {
     "provider": { "deepseek": { ... } }
   }
   ```

2. **改用 HTTP API 模式**（subprocess + --attach 实测也不工作，0 输出）：
   ```python
   # POST /session 创建 ephemeral
   r = POST /session/{sid}/message
     parts: [{type:"text"}, {type:"file", url:"data:text/plain;base64,..."}]
     model: {providerID: "deepseek", modelID: "deepseek-v4-flash"}
     agent: "pr-describer"
   ```

### 坑 6：file part 缺 `url` 字段

**症状**：`opencode HTTP 400: Missing key at ["parts"][1]["url"]`

**根因**：opencode 的 file part schema 是 `url`（指向 data URL 或已上传文件的 url），不是 `content`

**解决**：改用 base64 data URL：
```python
b64 = base64.b64encode(raw).decode("ascii")
parts.append({
    "type": "file",
    "filename": fp.name,
    "mime": "text/plain",
    "url": f"data:text/plain;base64,{b64}",
})
```

### 坑 7：bash heredoc 在 ssh-remote exec 里转义问题

**症状**：`ssh_ops.py: error: unrecognized arguments: 'JSONEOF'` 或 `$schema` 被解析成空

**解决**：
- 用 `<<'JSONEOF'` 单引号 heredoc（变量不展开）
- 多行内容用 `-- "..."` 把命令隔离
- API key 写入文件时用 base64 / data URL 编码避免 shell 解析

---

## 当前真实配置

### `/home/workflow/ReviewAgent/.env`（不入 git；以下为示例值）

```bash
GITLAB_URL=https://gitlab.your-company.com
GITLAB_PERSONAL_ACCESS_TOKEN=glpat-***REDACTED***
GITLAB_WEBHOOK_SECRET=***REDACTED-32-CHAR-HEX***
GITLAB_BOT_USERNAME=review-bot
OPENCODE_URL=http://127.0.0.1:4096
OPENCODE_USERNAME=opencode
OPENCODE_PASSWORD=
REDIS_URL=redis://127.0.0.1:6379/1
RQ_QUEUE_NAME=review
RQ_WORKER_TIMEOUT=600
REVIEWAGENT_DATA_DIR=/var/lib/reviewagent/data
REVIEWAGENT_LOG_LEVEL=INFO
MR_COOLDOWN_SECONDS=30
MAX_REVIEW_CALLS_PER_MR=0
```

### `/root/.config/opencode/opencode.jsonc`（chmod 600）

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "deepseek": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "DeepSeek",
      "options": {
        "baseURL": "https://api.deepseek.com/v1",
        "apiKey": "sk-YOUR_DEEPSEEK_KEY"
      },
      "models": {
        "deepseek-chat":     { "name": "DeepSeek Chat" },
        "deepseek-reasoner": { "name": "DeepSeek Reasoner" },
        "deepseek-v4-flash": { "name": "DeepSeek v4 Flash" }
      }
    }
  }
}
```

### `/etc/systemd/system/reviewagent-{webhook,worker}.service`

两文件同构，PATH 必须含 `/usr/bin`：

```ini
[Service]
Environment="PATH=/home/workflow/ReviewAgent/.venv/bin:/usr/local/bin:/usr/bin:/bin"
EnvironmentFile=/home/workflow/ReviewAgent/.env
WorkingDirectory=/home/workflow/ReviewAgent
ExecStart=/home/workflow/ReviewAgent/.venv/bin/uvicorn reviewagent.main:app --host 0.0.0.0 --port 3000
# worker 同理：ExecStart=/home/workflow/ReviewAgent/.venv/bin/rq worker review --url redis://127.0.0.1:6379/1
```

### firewalld

```bash
firewall-cmd --zone=public --list-ports
# 3000/tcp 4096/tcp
```

---

## 验证清单（部署后）

```bash
# 1. 服务健康
curl http://127.0.0.1:3000/health
curl http://127.0.0.1:4096/global/health
systemctl is-active reviewagent-webhook reviewagent-worker

# 2. Redis
/home/workflow/ReviewAgent/.venv/bin/python -c \
  'import redis; r=redis.from_url("redis://127.0.0.1:6379/1"); print(r.ping())'

# 3. SQLite
ls -la /home/workflow/data/telemetry.db
sqlite3 /home/workflow/data/telemetry.db ".tables"
# 期望：mr_activity  review_runs

# 4. firewalld
firewall-cmd --zone=public --query-port=3000/tcp   # yes
firewall-cmd --zone=public --query-port=4096/tcp   # yes

# 5. GitLab webhook（GitLab 那侧手动）
#    触发一个 MR open → 30s 内看 86 上 SQLite 是否有新记录
```

---

## 备份 / 恢复

需要备份的：
- `/home/workflow/data/` — SQLite + bare repos（**核心**）
- `/home/workflow/ReviewAgent/.env` — 凭证（**高敏**）
- `/root/.config/opencode/opencode.jsonc` — opencode 凭证（**高敏**）

不需要备份（可重建）：
- `/tmp/reviewagent-worktrees/` — tmpfs，重启即清
- systemd unit 文件 — 可重建
- 日志 / opencode.db — 临时

---

## SSH 远程操作备忘

主机别名：`ops-host`
调用：`cd C:/Users/reviewer/.claude/skills/ssh-remote && python scripts/ssh_ops.py exec --session ops-host --yes --i-know --trust-host --trust-override --cmd-timeout 30 -- "命令"`

常用 flags：
- `--yes` — 已确认目标主机
- `--i-know` — 高危操作（pkill / systemctl / firewalld）需此标志
- `--trust-host` — 首次连接受信任 host key
- `--trust-override` — 绕过 network_isolated 默认软约束