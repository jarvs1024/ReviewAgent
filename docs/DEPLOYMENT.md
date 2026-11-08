# 部署文档

> 更新日期：2026-11-08

ReviewAgent 通过 systemd 管理全部服务，部署在 Linux 服务器上。配置分三个平面：
**实例平面**（`config/instance.env`，本机一份）、**场景平面**（`profiles/` + 用户调参
`config/profiles/<name>/review.yaml`，由 `REVIEW_PROFILE` 选择）、
**规则平面**（规则包，见 [`docs/RULES.md`](RULES.md)）。完整生效配置见
[`docs/CONFIG.md`](CONFIG.md)，配置地图见 [`config/README.md`](../config/README.md)。

| 维度 | 生产服务器 |
|---|---|
| 主机 | Linux（workflow 用户） |
| OS | Linux (Anolis/CentOS) |
| webhook 端口 | `:3000` |
| RQ 队列 | `review`（主队列） + `review-weekly`（周报） |
| Redis | `redis://127.0.0.1:6379/0`（Docker 容器） |
| LLM Provider | `qoder-sdk`（推荐）/ `qodercli` subprocess / `opencode` HTTP |
| GitLab bot | 与 86 共用同一 bot 账号 |
| 进程管理 | systemd（4 unit + 1 timer） |
| 代码路径 | `/home/workflow/ReviewAgent` |
| 数据目录 | `/var/lib/reviewagent`（telemetry.db、repos/、weekly_reports/） |
| 场景包 | `ssd-python-autotest`（`REVIEW_PROFILE`） |

---

## 一、基础依赖

```bash
# Python 3.12+（需 >=3.12，3.13 亦可）
dnf install -y python3.12 python3.12-devel git
mkdir -p /home/workflow/ReviewAgent
cd /home/workflow/ReviewAgent
python3.12 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e ".[dev]"

# Node.js（LLM_PROVIDER=qodercli 时需要）
# 确保 node 在 PATH 中，qodercli 通过 npm 全局安装
```

---

## 二、目录准备

数据目录**不再**是代码目录下的 `./data`：systemd unit 显式设置
`REVIEWAGENT_DATA_DIR=/var/lib/reviewagent`（可在 `config/instance.env` 覆盖，两者必须一致）。

```bash
# 数据目录（SQLite + bare repos + 周报）+ worktree 临时目录
sudo install -d -o workflow -g workflow -m 700 /var/lib/reviewagent
sudo install -d -o workflow -g workflow -m 700 /tmp/reviewagent-worktrees

# 场景与数据绑定：config check 会在 data_dir 下写 instance.json（首次绑定后不可混用其他 profile）
sudo -u workflow /home/workflow/ReviewAgent/.venv/bin/python -m reviewagent config check \
    --profile ssd-python-autotest
```

`instance.json` 把 profile 名绑定到 data_dir。**切换场景必须新 data_dir**，否则
`config check` 直接报 `is bound to profile '...'`（fail-fast，不会静默混数据）。

---

## 三、配置

```bash
cp config/examples/instance.env.example config/instance.env
chmod 600 config/instance.env
# 编辑 config/instance.env，填入真实值：
#   GITLAB_URL / GITLAB_PERSONAL_ACCESS_TOKEN / GITLAB_WEBHOOK_SECRET（必填）
#   LLM_PROVIDER=qoder-sdk + QODER_SDK_PAT
#   REDIS_URL=redis://127.0.0.1:6379/0
#   REVIEW_PROFILE=ssd-python-autotest
#   REVIEWAGENT_DATA_DIR=/var/lib/reviewagent（也可由 systemd unit 固定）
#   LOG_FORMAT=text（text=现网观感 / json=机读单行）
```

模板只含实例平面键，且只列需要显式设置的值（其余字段有 schema 默认值，由 profile 提供）。
场景调参写 `config/profiles/<name>/review.yaml`，不要写进 `instance.env`。
**systemd `EnvironmentFile=` 不剥离行尾注释**：`instance.env` 里注释必须单独成行。

### RQ worker class（运维键，不在模板 / SCHEMA）

`RQ_WORKER_CLASS` 是 `rq` CLI 的环境变量，不参与应用配置 schema，按平台写进 systemd unit：

```ini
# Linux（fork 可用，默认无需设置）；macOS 本地开发必须用 SpawnWorker 规避 fork crash：
Environment=RQ_WORKER_CLASS=reviewagent.workers.rq_worker.ReviewAgentSpawnWorker
```

`RQ_WORKER_COUNT`（默认 3）只被本地脚本 `scripts/restart_local.sh` /
`scripts/services_ops.sh` 使用；systemd 生产部署按需要起多个
`reviewagent-worker.service`（或改用模板 unit 实例化）。

---

## 四、systemd 服务

`deploy/` 目录是 unit 的唯一事实源（`deploy.sh` 只负责安装）：

| Unit | 用途 |
|---|---|
| `reviewagent-webhook.service` | FastAPI webhook（uvicorn :3000，含 `/readyz`） |
| `reviewagent-worker.service` | RQ review 队列 worker |
| `reviewagent-weekly-worker.service` | RQ review-weekly 队列 worker（周报专用） |
| `reviewagent-weekly-enqueue.service` | 周报入队 oneshot |
| `reviewagent-weekly.timer` | 每周一 10:30 触发周报（与 `REVIEWAGENT_WEEKLY_CRON_SCHEDULE` 默认一致） |

所有 long-running unit 都带三行启动护栏：

```ini
Environment=REVIEW_PROFILE=ssd-python-autotest
Environment=REVIEWAGENT_DATA_DIR=/var/lib/reviewagent
ExecStartPre=/home/workflow/ReviewAgent/.venv/bin/python -m reviewagent config check
```

`ExecStartPre` 失败（缺必填 env / profile 非法 / instance 绑定冲突 / 配置值越界）时
服务直接进入 `failed`，不会带着坏配置接流量；错误只有一行，落在
`journalctl -u <unit>`。

```bash
# 部署 service 文件到 /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now reviewagent-worker
systemctl enable --now reviewagent-webhook
systemctl enable --now reviewagent-weekly-worker
systemctl enable --now reviewagent-weekly.timer
```

> 注意启动顺序：**先 worker，后 API**（原因见「六、升级与回滚顺序」）。

---

## 五、GitLab webhook（GitLab 侧手动）

- URL：`http://<server-ip>:3000/webhook`
- Secret token：与 `config/instance.env` 的 `GITLAB_WEBHOOK_SECRET` 一致
- Trigger：☑ Merge request events  ☑ Comments  ☑ Push events

---

## 六、升级与回滚顺序

### 升级：先新 worker、后新 API

1. `git`/`rsync` 同步代码 → `pip install -e ".[dev]"` → `systemctl daemon-reload`
2. `systemctl restart reviewagent-worker reviewagent-weekly-worker`
3. 确认 worker 日志出现 `bootstrap.ready kind=worker ... config_fingerprint=...`
4. `systemctl restart reviewagent-webhook`
5. 按「九、验证清单」检查

**为什么不能反着来**：新 API 入队的 job manifest 带新字段（如新增 kwargs），旧
worker 的 job 函数签名不认识，会以 `unexpected keyword argument` 失败。先上新
worker 时，旧 API 产生的 job 对新 worker 仍然兼容。

回滚顺序相反：**先回 API、再回 worker**（先让 producer 停止产出新 manifest）。

### 切换场景：新 data_dir + 同时重启 API 与 worker

场景包与 data_dir 是绑定的（`instance.json`），而 RQ job manifest 会校验
`profile_fingerprint` / `config_fingerprint`；混跑会出现「job 被指纹拒绝」或
「读到另一场景的历史数据」。因此切换必须是一次原子切换：

```bash
# 1. 停 worker（先），再停 API；确认没有 running run
systemctl stop reviewagent-worker reviewagent-weekly-worker
systemctl stop reviewagent-webhook
sqlite3 /var/lib/reviewagent/telemetry.db \
  "SELECT COUNT(*) FROM review_runs WHERE status='running';"   # 期望 0

# 2. 准备新 data_dir（新的空目录，不复制旧库）
sudo install -d -o workflow -g workflow -m 700 /var/lib/reviewagent-v2

# 3. 改 config/instance.env（REVIEW_PROFILE / REVIEWAGENT_DATA_DIR）
#    数据目录若固定在 unit 的 Environment=，则同步改 unit；两处保持一致
sudo systemctl edit --full reviewagent-webhook reviewagent-worker reviewagent-weekly-worker

# 4. 启动前静态校验（会写新 data_dir 的 instance.json）
sudo -u workflow /home/workflow/ReviewAgent/.venv/bin/python -m reviewagent config check --profile <new-profile>
sudo -u workflow /home/workflow/ReviewAgent/.venv/bin/python -m reviewagent rules check --profile <new-profile>

# 5. 起 worker → 起 API
systemctl daemon-reload
systemctl start reviewagent-worker reviewagent-weekly-worker
systemctl start reviewagent-webhook
```

旧 data_dir 保持只读保留一段时间（回滚与对账用），不要删。

---

## 七、日志

- `LOG_FORMAT=text`（默认）：现网观感；`LOG_FORMAT=json`：单行 JSON，便于采集。
- `LOG_ROTATION=single`（默认）由 loguru 进程内轮转；`none` 交给 journald / logrotate。
- 关键事件名以代码为准：`chain.lock_timeout`、`improve.record_suggestion failed`
  （spec 早期草稿的 `review.lock_timeout` / `telemetry.write_fail` 是旧命名，日志里搜不到）。

```bash
journalctl -u reviewagent-webhook -f
journalctl -u reviewagent-worker -f
```

---

## 八、备份 / 恢复

见 [`docs/BACKUP_RESTORE.md`](BACKUP_RESTORE.md)（`VACUUM INTO` 在线备份、drain 步骤、
恢复校验）。要点：`config/instance.env` 与 `/var/lib/reviewagent/` 都要备份；`/tmp/reviewagent-worktrees/`
与 systemd unit 可重建，不需备份。

---

## 九、验证清单

```bash
# 1. 配置 / 规则静态校验（与 ExecStartPre 同一入口）
cd /home/workflow/ReviewAgent
.venv/bin/python -m reviewagent config check --profile ssd-python-autotest   # 期望首行 ok
.venv/bin/python -m reviewagent rules check --profile ssd-python-autotest    # 期望列表 + 两个指纹

# 2. 服务状态（每个 unit 必须 active (running)）
systemctl status reviewagent-webhook reviewagent-worker reviewagent-weekly-worker --no-pager
systemctl list-timers reviewagent-weekly.timer --no-pager

# 3. 健康 / 就绪
curl -s http://127.0.0.1:3000/health
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:3000/readyz   # 期望 200
# readyz 200 = profile / RuleSet / instance / SQLite / Redis 全通过；503 时 body 的 reasons 直接指出缺项

# 4. 启动日志里的指纹（API 与 worker 必须一致）
journalctl -u reviewagent-webhook -n 200 | grep -E 'config.resolved|bootstrap.ready'
journalctl -u reviewagent-worker  -n 200 | grep -E 'config.resolved|bootstrap.ready'
# profile_fingerprint / config_fingerprint 两边不同 → 场景或配置漂移，禁止放流量

# 5. Redis / SQLite
python -c 'import redis; print(redis.from_url("redis://127.0.0.1:6379/0").ping())'
ls -la /var/lib/reviewagent/telemetry.db
curl -s http://127.0.0.1:3000/api/v1/telemetry/health
```

---

## 十、踩坑记录

1. **pip install 失败（urllib3 / Python 3.12 bug）**：先 `pip install 'urllib3<2'`，再升级 pip。
2. **worker "FileNotFoundError: git"**：systemd unit 的 PATH 须含 `/usr/bin`。
3. **上传文件 null 字节污染**：ssh_ops.py upload 的 staging 可能预分配块导致小文件被 null
   填充，上传后用 `python3 -m compileall -q reviewagent` 验证。
4. **`config/instance.env` 行尾注释**：systemd `EnvironmentFile` 不剥离行尾 `#`，注释必须单独成行，
   否则值里会带上注释文本（例如 `LOG_FORMAT=text   # ...` 变成非法值，`config check` 报错）。
5. **数据目录写到了代码目录**：旧部署把数据放 `ReviewAgent/data/`；升级后统一
   `/var/lib/reviewagent`，迁移前先按 BACKUP_RESTORE 备份再拷贝，不要两边同时写。
