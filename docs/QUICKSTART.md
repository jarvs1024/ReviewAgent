# 快速启动指南

> 适用于：本地 macOS 开发 / 86 服务器运维 / 重建部署
> 更新日期：2026-08-03（命令与配置已对齐当前代码）

---

## 一、本地 macOS 开发（v2 环境）

仓库已内置启动脚本，直接跑（脚本写死了 v2 环境变量）：

```bash
# 1. 依赖（Python 3.12）
cd /Users/jarvs/ReviewAgent
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# 2. 起 Redis（86 用的 testmate-redis 容器，映射到宿主机 :63790，db=2）
#    docker start testmate-redis   # 若已存在

# 3. 起 opencode serve（headless）
nohup /Users/jarvs/.opencode/bin/opencode serve --port 4096 --hostname 127.0.0.1 \
  > /tmp/opencode.log 2>&1 &

# 4. 同步 agent prompt 到 opencode（改了 prompts/*.md 后必跑）
python scripts/sync_agents.py

# 5. 起 webhook + worker（两个终端 / 后台）
bash scripts/run_webhook.sh     # :5052
bash scripts/run_worker.sh      # 同时起主队列 review-v2 + 周报队列 review-v2-weekly 两个 worker

# 6. （如需 GitLab 容器能回调）起 socat bridge
docker run -d --name reviewagent-bridge --network gitlab-stack_net -p 5051:5051 \
  alpine/socat TCP-LISTEN:5051,fork,reuseaddr TCP:host.docker.internal:5052
```

> 一键重启（本地 v2）：`bash scripts/restart_local.sh` 重启 opencode serve + web(:3000) + 主队列(review-v2，3 worker) 与周报队列(review-v2-weekly，1 worker)，配置读自 `.env`（web 用 :3000，与 `run_webhook.sh` 的 :5052 不是同一套本地配置）。

> 注意：`scripts/run_webhook.sh` / `run_worker.sh` 当前硬编码了真实凭证，仅供本地使用；生产请改为 `.env` 读取（见 DEPLOYMENT.md 安全提示）。

---

## 二、86 服务器运维

```bash
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
.venv/bin/python -c '
import sqlite3
db = sqlite3.connect("data/telemetry.db"); db.row_factory = sqlite3.Row
for r in db.execute("SELECT id, project_id, mr_iid, command, status, duration_ms FROM review_runs ORDER BY id DESC LIMIT 20"):
    print(dict(r))
'

# 重启
systemctl restart reviewagent-webhook reviewagent-worker

# 重启 opencode（setsid 后台，重启机器会丢）
pkill -f "opencode serve"; sleep 2
setsid bash -c 'nohup /usr/local/bin/opencode serve --port 4096 --hostname 0.0.0.0 >/var/log/opencode.log 2>&1 &' < /dev/null
```

---

## 三、手动触发 webhook（绕过 GitLab 调试）

```bash
curl -X POST http://localhost:5052/webhook \
  -H "X-Gitlab-Token: 42abb7d89ac3b177492706f9bc94bc56" \
  -H "Content-Type: application/json" \
  -d '{
    "object_kind": "note",
    "object_attributes": {
      "noteable_type": "MergeRequest",
      "note": "/describe"
    },
    "merge_request": {"iid": 1},
    "project": {"id": 34},
    "user": {"username": "test"}
  }'
```

---

## 四、周报

```bash
# 生成本周周报（JSON + MD + XLSX，默认 dry_run 不推送）
bash scripts/run_weekly_report.sh

# 真实推送到钉钉（需配 REVIEWAGENT_WEEKLY_DINGTALK_WEBHOOK_URL + SECRET）
WEEKLY_PUSH=true bash scripts/run_weekly_report.sh

# 指定项目 / 上周
bash scripts/run_weekly_report.sh --project-id 34 --week-offset -1
```

> 周报含三段采集（`REVIEWAGENT_WEEKLY_COLLECTORS`，默认 `telemetry,merged_mrs,repo_scan`）：本周检视概况、main 变更汇总、以及**代码质量全量扫描（`repo_scan`，含固有代码全局评估）**。三段各一次 opencode LLM 调用。

---

## 五、看 worker 进度（关键日志关键词）

```
webhook.queued        → webhook 路由成功，已入队
worker.run_*          → worker 拾到任务
git.clone_bare ok     → git clone 成功
git.worktree_add ok   → worktree 创建
opencode.run start    → 调 opencode
opencode.run ok       → opencode 返回成功
gitlab.update_*       → 写回 GitLab（标题/描述/建议）
describe.ok / improve.ok → 全部完成
```

---

## 六、故障排查决策树

```
GitLab webhook 调不到服务？
  ├─ 本地 curl OK → 防火墙 / bridge 问题（firewalld 或 socat）
  ├─ 本地 curl 失败 → 服务没起（systemctl / run_*.sh）
  └─ webhook URL 配错 → GitLab 那侧核对

Worker 不执行？
  ├─ SQLite 无新记录 → webhook 没路由（鉴权 / cooldown / bot 白名单）
  └─ SQLite 有 running → worker 卡死 → 看 worker 日志

Worker 报 "git not found"？
  └─ systemd unit PATH 不全 → 加 /usr/bin

git clone 重定向登录页？
  └─ URL 没带 token → 检查 GitLabClient.get_project_git_url

opencode HTTP 400 / 500？
  ├─ file part 问题 → 当前实现已改为内联 diff 文本
  └─ 模型找不到 → 检查 opencode.jsonc + sync_agents.py 是否同步

MR description / 建议没改？
  ├─ 看 worker 日志最后几行
  └─ SQLite review_runs.error 字段有详细错误
```

---

## 七、相关文档

- [`README.md`](../README.md) — 项目入口（功能 / 架构 / 配置）
- [`docs/DEPLOYMENT.md`](DEPLOYMENT.md) — 86 + 本地 v2 部署全记录（含踩坑）
- [`docs/V2_ENVIRONMENT.md`](V2_ENVIRONMENT.md) — 本地 v2 环境说明
