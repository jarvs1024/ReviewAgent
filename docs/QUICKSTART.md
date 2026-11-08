# 快速启动指南

> 适用于：本地开发 / 服务器运维
> 更新日期：2026-11-08

配置分三个平面：实例平面（`config/instance.env`，模板
`config/examples/instance.env.example`）、场景平面（`profiles/` + 用户调参
`config/profiles/<name>/review.yaml`，由 `REVIEW_PROFILE` 选择）、规则平面
（规则包，见 `docs/RULES.md`）。完整字段与来源见 `docs/CONFIG.md`，
配置地图见 `config/README.md`。

---

## 一、本地开发

```bash
# 1. 依赖（Python 3.12+，需 >=3.12，3.13 亦可）
cd ReviewAgent
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# 2. 配置
cp config/examples/instance.env.example config/instance.env
chmod 600 config/instance.env
# 编辑 config/instance.env，填入 GITLAB_URL / PAT / WEBHOOK_SECRET（必填）
# REVIEW_PROFILE 默认 ssd-python-autotest；场景调参见 config/profiles/<name>/review.yaml

# 3. 载入配置到当前 shell（config check 读进程环境）
set -a; source config/instance.env; set +a

# 4. 起 Redis（本地进程；默认 127.0.0.1:6380，避免与其它实例共用）
# brew install redis
# redis-server --port 6380 --bind 127.0.0.1 --dir data/redis --appendonly yes

# 5. 启动前静态校验（缺必填 / profile 非法 / 规则装配失败都在这里 fail-fast）
.venv/bin/python -m reviewagent config check
.venv/bin/python -m reviewagent rules check

# 6. 起 webhook + worker
.venv/bin/uvicorn reviewagent.main:app --host 0.0.0.0 --port 3000
.venv/bin/rq worker "${RQ_QUEUE_NAME:-review}" --url "${REDIS_URL:-redis://127.0.0.1:6380/0}"
```

> macOS 本地跑 worker 若遇到 RQ fork crash：`RQ_WORKER_CLASS=reviewagent.workers.rq_worker.ReviewAgentSpawnWorker`
> （`rq` CLI 运维键，不在应用 schema）。

---

## 二、服务器运维

```bash
# 一键部署 / 升级（幂等；含开机自启与部署后验证）
sudo bash deploy.sh
sudo bash deploy.sh --check     # 只做前置检查
sudo bash deploy/verify.sh      # 单独复检

# 生效配置（--sources 标注 env|config|profile|preset|default；secret 一律 ***）
.venv/bin/python -m reviewagent config show --diff-default --sources

# 服务状态（worker 是模板实例，按 RQ_WORKER_COUNT 起 @1..N）
systemctl status reviewagent-webhook 'reviewagent-worker@*' reviewagent-weekly-worker --no-pager
systemctl is-enabled reviewagent-webhook 'reviewagent-worker@*' reviewagent-weekly.timer
curl -s http://127.0.0.1:3000/health
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:3000/readyz   # 200 = 全就绪

# 实时日志
journalctl -u reviewagent-webhook -f
journalctl -u 'reviewagent-worker@*' -f
journalctl -u reviewagent-reconciler -f

# 看 SQLite 检视历史（生产数据目录 /var/lib/reviewagent）
.venv/bin/python -c '
import sqlite3
db = sqlite3.connect("/var/lib/reviewagent/telemetry.db"); db.row_factory = sqlite3.Row
for r in db.execute("SELECT id, project_id, mr_iid, command, status, duration_ms FROM review_runs ORDER BY id DESC LIMIT 20"):
    print(dict(r))
'

# 重启（顺序：先 worker 后 API，见 docs/DEPLOYMENT.md）
systemctl restart 'reviewagent-worker@*' reviewagent-weekly-worker
systemctl restart reviewagent-webhook
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
# 生成本周周报（JSON + MD，默认 dry_run 不推送）
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
bootstrap.ready / config.resolved   → 启动装配完成（含 profile_fingerprint / config_fingerprint）
webhook.queued                      → webhook 路由成功，已入队
worker.run_<command>                → worker 拾到任务（如 worker.run_improve）
git.clone_bare start / ok           → git clone 成功
qoder_sdk.usage / qodercli.usage    → LLM 调用返回（含 credits / context 占比）
gitlab.update_title / update_description / post_comment → 写回 GitLab
链锁竞争：chain.lock_timeout       → 释放链锁失败/超时，放弃本次 chain
落库失败：improve.record_suggestion failed → 建议未写入 telemetry
```

> 事件名以代码为准（spec 早期草稿的 `review.lock_timeout` / `telemetry.write_fail`
> 是旧命名，日志里搜不到）。

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
- [`docs/DEPLOYMENT.md`](DEPLOYMENT.md) — 服务器部署、切换场景与升级顺序
- [`docs/CONFIG.md`](CONFIG.md) — 生效配置（生成）与两个指纹
- [`docs/RULES.md`](RULES.md) — 规则包模板与 `rules check`
- [`docs/BACKUP_RESTORE.md`](BACKUP_RESTORE.md) — 备份 / 恢复 / drain
- [`docs/LLM_PROVIDER_ADAPTER.md`](LLM_PROVIDER_ADAPTER.md) — LLM Provider 适配层设计
