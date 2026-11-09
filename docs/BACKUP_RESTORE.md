# 备份与恢复 Runbook

> 适用于 `/var/lib/reviewagent` 单实例部署。所有命令以 `workflow` 用户执行，
> 数据目录变量按需替换（下称 `$DATA_DIR`，默认 `/var/lib/reviewagent`）。

## 一、备份什么

| 路径 | 必要性 | 说明 |
|---|---|---|
| `$DATA_DIR/telemetry.db` | **必须** | 检视历史 / 建议 / 采纳记录 / 周报索引（SQLite） |
| `$DATA_DIR/instance.json` | **必须** | profile ↔ data_dir 绑定（含 schema_version）；丢了会重建绑定，跨场景恢复时是门禁 |
| `$DATA_DIR/repos/` | **必须** | bare git 仓库（多 MR 共用对象库；丢了可重建，但重 clone 很慢） |
| `$DATA_DIR/weekly_reports/` | 建议 | 周报产物（JSON / Markdown），API 直接读取 |
| `config/instance.env` | **必须（高敏）** | 凭证（GitLab PAT / webhook secret / Qoder PAT / 钉钉 webhook 与 secret）；按密钥管理流程保存，不要进 git |
| `config/profiles/*/review.yaml` | 建议 | 用户场景调参；无密钥，可随代码库版本化 |
| `/tmp/reviewagent-worktrees/` | 不备份 | 每次任务临时目录，可重建 |
| systemd unit | 不备份 | `deploy/` 是事实源，重装即可 |

## 二、在线备份（`VACUUM INTO`，不锁业务）

`VACUUM INTO` 生成一致性快照，允许数据库继续写入（读到的是快照时刻的已提交数据；
之后写入的行不在备份里，属预期）。要求 SQLite ≥ 3.27。

```bash
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_DIR=/var/backups/reviewagent/$STAMP
sudo install -d -o workflow -g workflow -m 700 "$BACKUP_DIR"

# 1) 数据库一致性快照（不要直接 cp 正在写的 telemetry.db）
sqlite3 "$DATA_DIR/telemetry.db" "VACUUM INTO '$BACKUP_DIR/telemetry.db'"

# 2) 绑定文件与仓库对象（repo 目录是静态文件，可直接打包）
cp "$DATA_DIR/instance.json" "$BACKUP_DIR/instance.json"
tar -C "$DATA_DIR" -czf "$BACKUP_DIR/repos.tar.gz" repos
tar -C "$DATA_DIR" -czf "$BACKUP_DIR/weekly_reports.tar.gz" weekly_reports 2>/dev/null || true

# 3) 凭证：走密钥管理系统，不要把 config/instance.env 打进普通备份目录
#    cp /home/workflow/ReviewAgent/config/instance.env <安全位置>

# 4) 备份自检
sqlite3 "$BACKUP_DIR/telemetry.db" "PRAGMA integrity_check;"          # 期望 ok
sqlite3 "$BACKUP_DIR/telemetry.db" "SELECT COUNT(*) FROM review_runs;" # 记录行数，恢复后对账
```

不要把 `-wal` / `-shm` 和快照一起拷贝：`VACUUM INTO` 产物是独立单文件库。

## 三、恢复前的 drain（必须先停写）

恢复不能覆盖正在被写的数据文件。手写 drain 三步（本期没有 API drain 端点，
`E_STORAGE` 磁盘水位拒绝也未实现——见 CHANGELOG 的 Known limits）：

```bash
# 1) 先停 worker（停止消费队列），再停 API（停止入队）
systemctl stop 'reviewagent-worker@*' reviewagent-weekly-worker
systemctl stop reviewagent-webhook

# 2) 确认没有 running run（有则等它结束，或确认是孤儿后由 sweep 收尾）
sqlite3 "$DATA_DIR/telemetry.db" \
  "SELECT id, project_id, mr_iid, command, started_at FROM review_runs WHERE status='running';"
#    期望输出为空。非空且 started_at 早于 rq_worker_timeout + 300s：属孤儿（work-horse 被强杀），
#    可忽略；恢复后第一次启动 worker 时 sweep_orphaned_runs 会自动标 failed。

# 3) 确认没有进程占用数据目录
systemctl is-active reviewagent-webhook 'reviewagent-worker@*' reviewagent-weekly-worker
```

### schema 版本门禁

- `instance.json` 里的 `schema_version` 必须是当前代码支持的版本，且 `profile` 必须与
  恢复目标实例一致；不一致时 `reviewagent config check` 直接失败（退出码 2）：
  `unsupported schema_version ... in .../instance.json` / `is bound to profile '...'`。
- **跨场景（换 `REVIEW_PROFILE`）不要原地恢复同一个 data_dir**：按
  `docs/DEPLOYMENT.md` 的「切换场景 = 新 data_dir + 同时重启 API 与 worker」执行，
  否则新场景会读到旧场景的历史数据或被 instance 绑定挡住。
- `telemetry.db` 自身没有独立的 schema_version：打开时 `Store._init_schema()` 幂等建表
  + 逐列 `ALTER TABLE` 迁移，因此**旧库可被新版本直接打开**；反向（新库 → 旧代码）
  不受支持，回滚请回滚整个 data_dir。

## 四、恢复

```bash
BACKUP_DIR=/var/backups/reviewagent/<STAMP>
NEW_DATA_DIR=/var/lib/reviewagent-restored       # 建议先恢复到新目录验证，再切换

sudo install -d -o workflow -g workflow -m 700 "$NEW_DATA_DIR"
cp "$BACKUP_DIR/telemetry.db" "$NEW_DATA_DIR/telemetry.db"
cp "$BACKUP_DIR/instance.json" "$NEW_DATA_DIR/instance.json"
tar -C "$NEW_DATA_DIR" -xzf "$BACKUP_DIR/repos.tar.gz"
tar -C "$NEW_DATA_DIR" -xzf "$BACKUP_DIR/weekly_reports.tar.gz" 2>/dev/null || true
```

准备好后把 webhook / worker / weekly-worker / weekly-enqueue / reconciler 五个 unit
（`deploy/reviewagent-webhook.service`、`reviewagent-worker@.service`、
`reviewagent-weekly-worker.service`、`reviewagent-weekly-enqueue.service`、
`reviewagent-reconciler.service`）的 `Environment=REVIEWAGENT_DATA_DIR=...`
指向新目录（`systemctl edit --full`），`systemctl daemon-reload`。

## 五、恢复后校验

```bash
# 1) 静态门禁（profile / instance 绑定 / 配置合法）
cd /home/workflow/ReviewAgent
.venv/bin/python -m reviewagent config check --profile ssd-python-autotest

# 2) 库完整性 + 行数对账（与备份时记录的数字一致）
sqlite3 "$NEW_DATA_DIR/telemetry.db" "PRAGMA integrity_check;"            # ok
sqlite3 "$NEW_DATA_DIR/telemetry.db" "SELECT COUNT(*) FROM review_runs;"  # == 备份时
sqlite3 "$NEW_DATA_DIR/telemetry.db" "SELECT COUNT(*) FROM suggestions;"  # == 备份时
#    周报不是表：产物是 weekly_reports/ 下的文件，用 `ls | wc -l` 对账

# 3) 孤儿清扫演练（只读确认，不改库）
sqlite3 "$NEW_DATA_DIR/telemetry.db" \
  "SELECT COUNT(*) FROM review_runs WHERE status='running';"

# 4) 起服务：先 worker 后 API（与升级顺序一致），再验证
systemctl start 'reviewagent-worker@*' reviewagent-weekly-worker
systemctl start reviewagent-webhook
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:3000/readyz       # 期望 200
curl -s http://127.0.0.1:3000/api/v1/telemetry/health

# 5) 指纹核对：日志里的 profile_fingerprint / config_fingerprint 与备份实例一致
journalctl -u reviewagent-webhook -n 200 | grep -E 'config.resolved|bootstrap.ready'
```

`tests/runtime/test_ops_drill.py` 把上面第 2 步与「孤儿 run 被 sweep 标终态」做成了
可重复的自动化演练（`VACUUM INTO` → 复制到新目录 → 重新打开 → 行数一致）。

## 六、已知限制（本期不做）

- **磁盘水位拒绝（`E_STORAGE`）未实现**：磁盘满时写入会以 SQLite 错误暴露，需人工监控
  `df`（建议配合外部告警）。
- **API drain 端点未实现**：drain 只能按本文第三节手写（停 worker → 停 API → 确认无
  running run → 拷贝）。
- **secret 轮换后自动重投未实现**：轮换 `config/instance.env` 里的凭证后，重启 webhook 与 worker；
  已在队列里的 job 不会自动重投，失败后按需在 GitLab 重新触发。
