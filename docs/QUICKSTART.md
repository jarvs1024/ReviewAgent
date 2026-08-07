# 快速启动指南

> 适用于：本地开发 / 服务器运维
> 更新日期：2026-08-07

---

## 一、本地开发

```bash
# 1. 依赖（Python 3.12）
cd ReviewAgent
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# 2. 配置
cp .env.example .env
# 编辑 .env，填入 GITLAB_URL / PAT / WEBHOOK_SECRET

# 3. 起 Redis（Docker 或本地）
# docker run -d --name redis -p 6379:6379 redis:7

# 4. 起 webhook + worker
.venv/bin/uvicorn reviewagent.main:app --host 0.0.0.0 --port 3000
.venv/bin/rq worker review --url redis://127.0.0.1:6379/0
```

---

## 二、服务器运维

```bash
# 服务状态
systemctl is-active reviewagent-webhook reviewagent-worker@1 reviewagent-worker@2 reviewagent-worker@3
curl http://127.0.0.1:3000/health

# 实时日志
journalctl -u reviewagent-webhook -f
journalctl -u reviewagent-worker@1 -f

# 看 SQLite 检视历史
.venv/bin/python -c '
import sqlite3
db = sqlite3.connect("/home/workflow/data/telemetry.db"); db.row_factory = sqlite3.Row
for r in db.execute("SELECT id, project_id, mr_iid, command, status, duration_ms FROM review_runs ORDER BY id DESC LIMIT 20"):
    print(dict(r))
'

# 重启
systemctl restart reviewagent-webhook reviewagent-worker@1 reviewagent-worker@2 reviewagent-worker@3
```

---

## 三、手动触发 webhook（绕过 GitLab 调试）

```bash
curl -X POST http://localhost:3000/webhook \
  -H "X-Gitlab-Token: <your-secret>" \
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

# 真实推送到钉钉
WEEKLY_PUSH=true bash scripts/run_weekly_report.sh

# 指定项目 / 上周
bash scripts/run_weekly_report.sh --project-id 34 --week-offset -1
```

> 周报含三段采集（默认 `telemetry,merged_mrs,repo_scan`）：本周检视概况、main 变更汇总、代码质量全量扫描（含固有代码评估）。三段各一次 LLM 调用。

---

## 五、看 worker 进度（关键日志关键词）

```
webhook.queued        → webhook 路由成功，已入队
worker.run_*          → worker 拾到任务
git.clone_bare ok     → git clone 成功
git.worktree_add ok   → worktree 创建
llm.run start         → 调 LLM（qodercli / opencode）
llm.run ok            → LLM 返回成功
gitlab.update_*       → 写回 GitLab（标题/描述/建议）
describe.ok / improve.ok → 全部完成
```

---

## 六、故障排查决策树

```
GitLab webhook 调不到服务？
  ├─ 本地 curl OK → 防火墙问题（firewalld）
  ├─ 本地 curl 失败 → 服务没起（systemctl）
  └─ webhook URL 配错 → GitLab 那侧核对

Worker 不执行？
  ├─ SQLite 无新记录 → webhook 没路由（鉴权 / cooldown / bot 白名单）
  └─ SQLite 有 running → worker 卡死 → 看 worker 日志

Worker 报 "git not found"？
  └─ systemd unit PATH 不全 → 加 /usr/bin

LLM 调用失败？
  ├─ qodercli 模式 → 检查 node 在 PATH 中、qodercli 已安装
  └─ opencode 模式 → 检查 opencode serve :4096 是否运行

MR description / 建议没改？
  ├─ 看 worker 日志最后几行
  └─ SQLite review_runs.error 字段有详细错误
```

---

## 七、相关文档

- [`README.md`](../README.md) — 项目入口（功能 / 架构 / 配置）
- [`docs/DEPLOYMENT.md`](DEPLOYMENT.md) — 服务器部署记录
- [`docs/LLM_PROVIDER_ADAPTER.md`](LLM_PROVIDER_ADAPTER.md) — LLM Provider 适配层设计
