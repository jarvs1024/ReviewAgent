# 部署文档

> 更新日期：2026-08-07

ReviewAgent 通过 systemd 管理全部服务，部署在 Linux 服务器上。

| 维度 | 生产服务器 |
|---|---|
| 主机 | Linux（workflow 用户） |
| OS | Linux (Anolis/CentOS) |
| webhook 端口 | `:3000` |
| RQ 队列 | `review`（主队列） + `review-weekly`（周报） |
| Redis | `redis://127.0.0.1:6379/0`（Docker 容器） |
| LLM Provider | `qodercli` subprocess（默认），无需额外 daemon |
| GitLab bot | 与 86 共用同一 bot 账号 |
| 进程管理 | systemd（5 unit + 1 timer） |
| 代码路径 | `/home/workflow/ReviewAgent` |
| 数据目录 | `/home/workflow/data`（telemetry.db、weekly_reports/） |

---

## 一、基础依赖

```bash
# Python 3.12 + venv
dnf install -y python3.12 python3.12-devel git
mkdir -p /home/workflow/ReviewAgent
cd /home/workflow/ReviewAgent
python3.12 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e ".[dev]"

# Node.js（qodercli 需要）
# 确保 node 在 PATH 中，qodercli 通过 npm 全局安装
```

## 二、配置

```bash
cp .env.example .env
# 编辑 .env，填入真实值（GITLAB_URL / PAT / WEBHOOK_SECRET 必填）
# 关键配置：
#   LLM_PROVIDER=qodercli
#   REDIS_URL=redis://127.0.0.1:6379/0
#   REVIEWAGENT_DATA_DIR=/home/workflow/data
#   RQ_WORKER_CLASS=reviewagent.workers.rq_worker.ReviewAgentSpawnWorker
```

## 三、systemd 服务

共 5 个 service + 1 个 timer：

| Unit | 用途 |
|---|---|
| `reviewagent-webhook.service` | FastAPI webhook（uvicorn :3000） |
| `reviewagent-worker@.service` | RQ review 队列 worker（模板，实例化 @1/@2/@3） |
| `reviewagent-weekly-worker.service` | RQ review-weekly 队列 worker（周报专用） |
| `reviewagent-weekly-enqueue.service` | 周报入队 oneshot |
| `reviewagent-weekly.timer` | 每周一 10:00 触发周报 |

```bash
# 部署 service 文件到 /etc/systemd/system/
# 启用并启动
systemctl daemon-reload
systemctl enable --now reviewagent-webhook
systemctl enable --now reviewagent-worker@1 reviewagent-worker@2 reviewagent-worker@3
systemctl enable --now reviewagent-weekly-worker
systemctl enable --now reviewagent-weekly.timer
```

## 四、GitLab webhook（GitLab 侧手动）

- URL：`http://<server-ip>:3000/webhook`
- Secret token：与 `.env` 的 `GITLAB_WEBHOOK_SECRET` 一致
- Trigger：☑ Merge request events  ☑ Comments  ☑ Push events

---

## 五、验证清单

```bash
# 1. 服务状态
systemctl is-active reviewagent-webhook reviewagent-worker@1 reviewagent-worker@2 reviewagent-worker@3

# 2. 健康检查
curl http://127.0.0.1:3000/health

# 3. Redis
python -c 'import redis; print(redis.from_url("redis://127.0.0.1:6379/0").ping())'

# 4. SQLite
ls -la /home/workflow/data/telemetry.db

# 5. Telemetry API
curl http://127.0.0.1:3000/api/v1/telemetry/health
```

---

## 六、备份 / 恢复

需备份：
- `/home/workflow/data/` — SQLite + bare repos（核心）
- `/home/workflow/ReviewAgent/.env` — 凭证（高敏）

不需备份（可重建）：systemd unit、日志、`/tmp/reviewagent-worktrees/`（tmpfs）。

---

## 七、部署流程

代码更新后：

```bash
# 1. 上传更新文件到 /home/workflow/ReviewAgent/
# 2. 重启服务
systemctl restart reviewagent-webhook reviewagent-worker@1 reviewagent-worker@2 reviewagent-worker@3

# 3. 验证
systemctl is-active reviewagent-webhook reviewagent-worker@1 reviewagent-worker@2 reviewagent-worker@3
```

---

## 八、踩坑记录

1. **pip install 失败（urllib3 / Python 3.12 bug）**：先 `pip install 'urllib3<2'`，再升级 pip。
2. **worker "FileNotFoundError: git"**：systemd unit 的 PATH 须含 `/usr/bin`。
3. **上传文件 null 字节污染**：ssh_ops.py upload 的 staging 可能预分配块导致小文件被 null 填充，上传后用 `python3 -m compileall -q reviewagent` 验证。
4. **`full_files` NameError**：`_merge_chunks` 是 `@staticmethod`，不能引用调用方的局部变量，须用 `config.improve_full_files`。
