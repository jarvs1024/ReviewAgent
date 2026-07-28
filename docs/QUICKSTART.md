# ReviewAgent 快速启动指南

> 适用于：本地开发 / 86 服务器运维 / 重建部署

---

## 本地开发（Windows）

```powershell
# 1. 安装依赖（Python 3.12）
cd D:\Code\ReviewAgent
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env 填：
#   GITLAB_URL=https://gitlab.your-company.com
#   GITLAB_PERSONAL_ACCESS_TOKEN=<your PAT>
#   GITLAB_WEBHOOK_SECRET=<random hex>

# 3. 启动 Redis（必须）
docker run -d -p 6379:6379 --name review-redis redis:7-alpine
# 或本地 redis-server --daemonize yes

# 4. 启动 webhook（开发模式，autoreload）
uvicorn reviewagent.main:app --host 0.0.0.0 --port 3000 --reload

# 5. 启动 RQ worker（另起终端）
rq worker review --url redis://localhost:6379/0

# 6. opencode（本地安装）
npm i -g opencode-ai
# 或用 86 上的 opencode（修改 .env 的 OPENCODE_URL）

# 7. 测试 webhook（curl 模拟）
curl -X POST http://localhost:3000/webhook ^
  -H "X-Gitlab-Token: <your secret>" ^
  -H "Content-Type: application/json" ^
  -d @test_payload.json
```

---

## 86 服务器运维

```bash
# SSH 上 86
ssh root@ops-host.internal

# 服务状态
systemctl status reviewagent-webhook reviewagent-worker
curl http://127.0.0.1:3000/health
curl http://127.0.0.1:4096/global/health

# 实时日志
journalctl -u reviewagent-webhook -f
journalctl -u reviewagent-worker -f
tail -f /var/log/opencode.log

# 看 SQLite 检视历史
/home/workflow/ReviewAgent/.venv/bin/python -c '
import sqlite3
db = sqlite3.connect("/home/workflow/data/telemetry.db")
db.row_factory = sqlite3.Row
for r in db.execute("SELECT id, project_id, mr_iid, command, status, duration_ms FROM review_runs ORDER BY id DESC LIMIT 20"):
    print(dict(r))
'

# 重启服务
systemctl restart reviewagent-webhook reviewagent-worker

# opencode 不在 systemd（用 setsid 后台），重启机器会丢
# 重启 opencode：
pkill -f "opencode serve"
sleep 2
cd /root
setsid bash -c 'nohup /usr/local/bin/opencode serve --port 4096 --hostname 0.0.0.0 >/var/log/opencode.log 2>&1 &' < /dev/null
```

---

## 86 服务器重建（PoC 部署步骤）

详见 [`docs/DEPLOYMENT.md`](DEPLOYMENT.md)。快速命令版：

```bash
# 系统依赖
dnf install -y python3.12 python3.12-devel git

# 项目代码（从 git 或 scp 同步）
mkdir -p /home/workflow/ReviewAgent

# venv + 依赖
cd /home/workflow/ReviewAgent
python3.12 -m venv .venv
.venv/bin/pip install --no-cache-dir 'urllib3<2'  # 关键：先修 urllib3 bug
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e ".[dev]"

# firewalld
firewall-cmd --zone=public --add-port=3000/tcp --permanent
firewall-cmd --zone=public --add-port=4096/tcp --permanent
firewall-cmd --reload

# systemd unit（PATH 含 /usr/bin！）
# 见 DEPLOYMENT.md § "systemd unit"

# opencode 配置
mkdir -p /root/.config/opencode/agents
cp reviewagent/prompts/describe.md /root/.config/opencode/agents/pr-describer.md
# 写 opencode.jsonc（见 DEPLOYMENT.md § "/root/.config/opencode/opencode.jsonc"）

# 起 opencode serve（setsid 防被 ssh session 杀）
cd /root
setsid bash -c 'nohup /usr/local/bin/opencode serve --port 4096 --hostname 0.0.0.0 >/var/log/opencode.log 2>&1 &' < /dev/null

# 启服务
systemctl daemon-reload
systemctl enable --now reviewagent-webhook reviewagent-worker

# 验证
curl http://127.0.0.1:3000/health
curl http://127.0.0.1:4096/global/health
```

---

## GitLab webhook 配置（GitLab 那侧）

| 字段 | 值 |
|---|---|
| URL | `http://ops-host.internal:3000/webhook` |
| Secret token | 与 `.env` 中 `GITLAB_WEBHOOK_SECRET` 一致 |
| Trigger | ☑ Merge request events  ☑ Comments |
| SSL verification | ☐ 禁用（HTTP） |

触发命令（评论 MR 时）：
```
/describe    # 自动生成中文 description
/review      # (Phase 2)
/improve     # (Phase 2)
/dismiss N   # (Phase 2)
/adopt N     # (Phase 2)
```

---

## 测试 / 调试

### 单元测试（本地）

```bash
pytest tests/unit/ -v
```

### 手动触发 webhook（绕过 GitLab）

```bash
# 用真实 GitLab MR payload
curl -X POST http://localhost:3000/webhook \
  -H "X-Gitlab-Token: $(grep GITLAB_WEBHOOK_SECRET .env | cut -d= -f2)" \
  -H "Content-Type: application/json" \
  -d '{
    "object_kind": "note",
    "object_attributes": {
      "noteable_type": "MergeRequest",
      "note": "/describe"
    },
    "merge_request": {"iid": 1},
    "project": {"id": 1},
    "user": {"username": "test"}
  }'
```

### 看 worker 进度

```bash
journalctl -u reviewagent-worker -f
# 关键日志关键词：
#   webhook.queued   → webhook 路由成功
#   worker.run_describe  → worker 拾到任务
#   git.clone_bare ok   → git clone 成功
#   git.worktree_add ok → worktree 创建
#   opencode.run start  → 调 opencode
#   opencode.run ok     → opencode 返回成功
#   gitlab.update_title → 写 title
#   gitlab.update_description → 写 description
#   describe.ok         → 全部完成
```

### 调 opencode HTTP API 直接测

```python
import urllib.request, json

# 1. 创建 session
req = urllib.request.Request(
    "http://localhost:4096/session",
    data=json.dumps({"title": "manual-test"}).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
sid = json.loads(urllib.request.urlopen(req).read())["id"]

# 2. 发消息
req = urllib.request.Request(
    f"http://localhost:4096/session/{sid}/message",
    data=json.dumps({
        "parts": [{"type": "text", "text": "Say hello"}],
        "model": {"providerID": "deepseek", "modelID": "deepseek-v4-flash"},
        "agent": "pr-describer",
    }).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
print(urllib.request.urlopen(req, timeout=60).read().decode()[:1000])
```

---

## 故障排查决策树

```
GitLab webhook 调不到 86 ?
  ├─ 86 本地 curl OK → 防火墙问题 → firewall-cmd --add-port
  ├─ 86 本地 curl 失败 → 服务没启 → systemctl status
  └─ webhook URL 配错 → GitLab 那侧核对

Worker 不执行？
  ├─ SQLite 无新记录 → webhook 没路由（鉴权 / cooldown / bot 白名单）
  └─ SQLite 有 running → worker 卡死 → 看 worker 日志

Worker 报 "git not found"？
  └─ systemd unit PATH 不全 → 加 /usr/bin

git clone 重定向登录页？
  └─ URL 没带 token → 检查 GitLabClient.get_project_git_url

opencode HTTP 400？
  ├─ parts[1].url 缺 → 用 data URL 编码 file
  └─ opencode 模型找不到 → 检查 opencode.jsonc

opencode 启动卡 10s "fetch models.dev"？
  └─ 这是 opencode run 模式的限制 → 改用 HTTP API（已默认）

MR description 没改？
  ├─ 看 worker 日志最后几行
  └─ SQLite err 字段有详细错误
```

---

## 相关文档

- [`docs/STATUS.md`](STATUS.md) — 项目状态 + 后续路线图
- [`docs/DEPLOYMENT.md`](DEPLOYMENT.md) — 86 部署全记录（含所有踩坑）
- [`README.md`](../README.md) — 项目入口
- [`plans/glistening-gathering-perlis.md`](../plans/glistening-gathering-perlis.md) — 初始设计方案