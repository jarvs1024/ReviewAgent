# 统一端到端验证：配置生效性审计 + 真实数据功能链

**日期**：2026-11-08
**分支**：`new_review_v2`（不合入 `main`）
**状态**：已确认，进入实施

## 1. 背景与目标

现状：功能 E2E 是 `scripts/e2e/verify_e2e.py` 的一次性脚本，依赖手工预置的 MR；
配置/规则/开关是否真的生效，只有零散的单元测试佐证，没有一套可重复的全量验证。

目标：

1. 配置项、规则、开关**逐项**有可执行的"生效证据"，新增配置项漏测会让门禁变红；
2. 真实数据场景（临时 GitLab 项目 + 真实 MR + 真实 LLM）跑通功能链，并断言
   当前 `review.yaml` 的调参在产出物上可观测；
3. 两者合并为**一个入口**，支持快速档/全量档，产出结构化报告。

## 2. 审计结论（2026-11-08）

92 个 schema 字段中 88 个在活路径被读取，4 项名不副实：

| 字段 | 问题 | 处理 |
|---|---|---|
| `review_config_dir` | Config 字段无人读取，行为在 `scenario/profile.py` 里重新解析 env | 接线：优先读 `current().review_config_dir` |
| `weekly_timezone` | 周边界硬编码 +08:00，值只进元数据 | 接线：`ZoneInfo(cfg.weekly_timezone)` 驱动周边界 |
| `weekly_cron_schedule` | 只落到 dataclass；systemd timer 硬编码 `Mon 10:30` | 接线：`services_ops.sh` 按 env 渲染 timer drop-in |
| `rq_worker_count` | Python 不读，worker 数量由 shell 直接读 env | 保留：env 确实生效（脚本平面），catalog 登记为 `script` |

另：`profile.capabilities` 全仓无消费点，登记为 `reserved`（不删，避免破坏第三方场景包）。

三个缺陷：

1. `review.yaml` 只校验顶层分组，嵌套拼错（`limits.max_suggestion`）静默无效；
2. env 覆盖 `review.yaml` 时无任何提示；
3. 用户可调面缺少 `repo_context` / `reporting`。

## 3. 配置生效性机制

### 3.1 单一登记表（防漂移）

`scripts/e2e/catalog.py` 给 `SCHEMA` 每个字段登记一种 witness：

- `startup`：非法值启动即失败（generic，按 `type/choices/minimum/maximum` 参数化）；
- `behavior`：进程内真实函数的行为 delta（如 `max_suggestions=1` 只发 1 条）；
- `live`：由全量档的真实链路证明（LLM/GitLab/周报/Redis 等）；
- `script`：由 shell/systemd 消费（`rq_worker_count`）；
- `reserved`：当前无消费点，文档标注（`capabilities`）。

`tests/test_config_catalog.py` 断言五个 kind 的并集**恰好等于**全部 schema 字段，
且每个 `behavior` 条目在 `BEHAVIOR_WITNESSES` 中可调用。新增字段不登记 → 红。

### 3.2 三层 witness

- **L1 配置平面**：非法值失败；`config show --sources` 来源正确；五级优先级；
  `review.yaml` 嵌套白名单与分组类型校验；env 遮蔽告警。
- **L2 运行时行为**：真实 diff fixture + 纯函数/规划器，逐 knob 断言可观测 delta：
  建议条数、严重度/评分过滤、抽样、文件数上限、分批、源码上下文行数、优先级权重、
  路径过滤、仓库上下文行数、规则键装配/禁用/覆盖/提升、动态规则参数。
- **L3 真实链路**：临时 GitLab 项目 + MR，webhook → worker → 评论/汇总；
  describe/improve/auto_chain/adopt/dismiss/ui_apply/telemetry/weekly；
  并用当前 `review.yaml` 验证条数上限、`min_severity`、`skip_paths` 在真实产出上生效。

## 4. 入口与产物

```bash
# 快速档（秒级，无需栈）：配置平面 + 行为 witness + 规则/开关
.venv/bin/python scripts/e2e/run_e2e.py --tier fast

# 全量档：快速档 + 临时项目/真实 MR/真实 LLM + 功能链 + 周报
.venv/bin/python scripts/e2e/run_e2e.py --tier full [--round 1] [--keep]

# 兼容旧用法
.venv/bin/python scripts/e2e/verify_e2e.py --feature all --mr ... # shim → run_e2e
```

产物：`logs/e2e/report-<stamp>.json`（每项 witness 的 kind/期望/实际/证据/耗时），
失败非零退出；`logs/e2e/run-*.log` 保留单项明细。

## 5. 模块划分

```
scripts/e2e/
  __init__.py
  catalog.py      # schema 字段 → witness 登记表（唯一来源）
  report.py       # 结果模型、JSON/控制台输出
  stack.py        # HTTP/GitLab/Redis/telemetry 客户端，锁清理（仅套件启动时）
  fixtures.py     # 临时项目/分支/MR 创建与清理
  flows.py        # 8 个功能检查（从 verify_e2e.py 迁入并参数化）
  config_audit.py # 快速档执行器（pytest 子集 + 静态目录校验）
  run_e2e.py      # 单入口
  verify_e2e.py   # 兼容 shim
tests/
  test_config_catalog.py        # 目录完整性 + 启动校验 parametric
  test_config_effectiveness.py  # L2 行为 witness
  test_review_config_validation.py
  test_scenario_wiring.py       # review_config_dir / weekly_timezone
```

## 6. 关键决策

1. **锁处理**：套件只在启动时清理 `cooldown/max_review_calls/diff_head`，运行中
   绝不删 `reviewagent:lock:*`（上次重复链事故根因）；需要重跑时等待锁自然过期。
2. **真实数据即临时资产**：每次全量档自建 `reviewagent-e2e-<stamp>` 项目与分支，
   结束删除；`--keep` 保留用于排查。不使用任何生产 MR 编号。
3. **配置可观测**：不在测试中改 `review.yaml`（需重启），而是断言当前值在真实产出
   上可观测；"改值→行为变化"由 L2 进程内 witness 覆盖。
4. **不做**：跨进程热加载配置、自动改 systemd timer 的本地 Mac 路径（仅生产 systemd 生效）。

## 7. 验收标准

- `tests/test_config_catalog.py`：目录与 schema 完全对齐，缺一项即失败；
- `run_e2e.py --tier fast` 绿，覆盖全部 `behavior` witness；
- `run_e2e.py --tier full` 在本地栈（GitLab :8929 / Redis :6380 / qoder-sdk）绿，
  临时资产清理干净，报告落盘；
- 全量 pytest 绿（含新增测试），`config check` 通过；
- `docs/CONFIG.md`、`config/README.md`、`CHANGELOG.md` 同步；只提交到 `new_review_v2`。
