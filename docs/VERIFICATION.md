# ReviewAgent 验证总纲

> 本文档汇总 ReviewAgent 的**验证流程**、**验证点清单**、**证据**、**缺陷台账**与**剩余风险**。
> 覆盖三层验证：单元测试 / E2E 契约测试 / 真实业务全流程。

- 最后更新：2026-11-12
- 对应代码基线：`reviewagent` + `scripts/e2e`（本地 GitLab `http://127.0.0.1:8929/`）
- 真实业务样本：p31 `review-bot-v2/ssd-real-biz-full`（本地 SSD 仓库 `/Users/jarvs/Code/dml_epc_auto-main`，10477 文件）
  —— MR !3（重构前配置产出，15 条）与 MR !4（**重构后新配置**产出，23 条）

---

## 1. 结论摘要

> **2026-11-12 更新（三轮）**：① 用户重构后完整重跑三层验证，修复 D10–D14；
> ② 按「总结修复 + 补齐剩余项」要求，逐项**实证核实**上一轮遗留清单，确认属实后修复 **E8–E13**，
> 新增 2 条真实链路 E2E 流程（`apply_closure` / `reconcile_resolved`），并连跑 3 遍完整验证（见 §14）。

| 维度 | 结论 | 证据 |
|---|---|---|
| 违规检出正确性 | ✅ 通过 | MR !2 11 条 / MR !3 15 条 / MR !4 23 条，telemetry ↔ GitLab 双向对齐，resolve 状态不一致数 0 |
| 图1（检视汇总）正确性 | ✅ 通过 | 两个 MR 逐格核对共 **12/12** 零偏差（总数/采纳率/3 个严重度行/总计行） |
| 图2（inline 意见）正确性 | ✅ 通过 | 38 条建议正文、行号锚点、applicability 三态、规则键引用（15/15、23/23）均正确 |
| 数据采集准确性 | ✅ 通过（1 处上游限制） | 20 个 telemetry 端点 200；token 用量为 0，根因在 provider 上游 —— **本轮用探针钉死**：上游 usage **明确回填 0 值**（键存在），非 ReviewAgent 缺陷（见 §7.3） |
| 三条交互闭环 | ✅ 通过 | Apply / `/adopt` / `/dismiss` 均在真实浏览器点击 + 真实 GitLab 落地 |
| 其余功能全流程 | ✅ 通过 | 周报、`/describe` 刷新、`/improve` 增量、reconciler、冷却、限流 |
| 单元测试回归 | ✅ 通过 | `1655 passed / 10 failed / 2 skipped`；10 个失败为既有环境问题（本机缺 `qodercli.js`），**基线对照已实测**：HEAD 基线 `1436 passed / 10 failed / 2 skipped`（同一批失败），累计净增 **+219** 且零回归；**且与 shell 环境无关**（D15 修复后，带/不带 `instance.env` 数字完全一致） |
| E2E 快速档 | ✅ 通过 | `21/21` witnesses |
| E2E 全量档 | ✅ 通过 | `32/32`（16 个功能流 + 5 项配置观测 + 5 项真实配置断言 + 6 项遥测审计），**本轮新增 `apply_closure`（真实 Apply 闭环）与 `reconcile_resolved`（reconciler 链路）两条真实链路** |
| **可复现性** | ✅ 通过 | **同一份代码连跑 3 轮完整验证，核心指标逐项一致**（L1 1655/10/2 + 失败集合相同、E2E 快速 21/21、全量 32/32、L3 19/20 与 20/20、文档自检通过）→ 见 §14 |
| 重构后行为变更核实 | ✅ 通过 | `min_severity: low` 在真实 MR !4 上产出 1 条 low 级建议并进入图1 LOW 行；`review_mode: test` 新增真实消费点；conftest 参与检视（策略驱动） |
| 累计新发现缺陷 | 14 个（已全部修复） | D5 E2E 假阳性、D6 ISO 漏检、D7 证据锚点漂移、D8 E2E 真发钉钉、D9 describe 标题漂移、**D10 E2E 断言写死旧策略**、**D11 删除型建议漏判**、**D12 删除型采纳假阳性**、**D13 text_only 误用删除语义**、**D14 cohort 粒度过粗致图1 少算**、**D15 单测受部署环境污染**、**D16 收缩建议形态与落库矛盾** |
| 本轮新增（E 编号） | 2 个真缺陷 + 3 项覆盖补齐 | **E8 `review_runs.model` 一列两义**、**E11 token 断言只做恒等式**；E10 MR 事件 close/merge/draft 分支、E12 跨轮次 supersede 完整链路、E13 周报 live 字段 —— 详见 §7.1c |

### 本轮新增的测试资产

**第三轮（E 编号）新增：**

| 文件 | 例数 | 覆盖 |
|---|---|---|
| `tests/test_review_run_model_fields.py`（新建） | 8 | E8：`model` / `model_configured` 语义拆分（store 层 + 命令层 + HTTP 层） |
| `tests/test_webhook_mr_action_branches.py`（新建） | 13 | E10：MR 事件 `close`/`merge`/`draft`/未知 action/bot 自触发 |
| `tests/test_e2e_cost_signal_audit.py`（新建） | 13 | E11：成本信号 4 条语义断言 + 历史行豁免 + 缺列防护 |
| `tests/test_cross_round_supersede.py`（新建） | 12 | E12：跨轮次 supersede 完整链路 + 4 个边界 + 三代链 |
| `tests/test_weekly_live_field_audit.py`（新建） | 13 | E13：周报 live 字段（标题/emoji/dashboard 链接/分片数） |
| `tests/test_cleanup_e2e_projects.py`（新建） | 26 | O5：清理脚本的删除安全（保护名单 + 命名精确匹配） |
| `tests/test_e2e_telemetry_audit.py`（扩展） | +1 | E11：fixture 补 `model_configured` |
| **合计** | **+86** | 单元测试 **1568 → 1655** |

**前两轮（D 编号）新增：**

| 文件 | 行数 | 例数 | 覆盖 |
|---|---|---|---|
| `tests/test_telemetry_api_contract.py`（新建） | 339 | 40 | 20 个 telemetry 端点的 HTTP 契约 + 错误码 + 防漏测登记守卫 |
| `tests/test_webhook_entry_contract.py`（新建） | 179 | 22 | 命令解析、鉴权、限流/冷却的 fail-open 语义 |
| `tests/test_describe_title_sync.py`（新建） | 110 | 4 | D9：describe 改标题后同步 telemetry |
| `tests/test_e2e_telemetry_audit.py`（扩展） | +90 | +7 | D5：applicability 三态的 E2E 审计 |
| `tests/test_verify_e2e_smoke.py`（扩展） | +30 | +2 | D8：E2E 外发安全闸 |
| `tests/test_config_catalog.py`（扩展） | +50 | +3 | live 字段绑定/记账护栏 |
| **合计** | | **+78**（相对本轮起点 1450） | 单元测试 1450 → **1528**；HEAD 基线 1436 → **1528** |

### E2E 功能流程（14 → 16）

| 流程 | 验证内容 | 状态 |
|---|---|---|
| `applicability` | 三态与发布正文一致（text_only 无 suggestion 块 / delete 有块 / replace 块与 improved_code 一致） | 新增 |
| `cooldown` | 同 MR 同命令第二次被冷却拦截 | 新增 |
| `rate_limit` | `should_skip_max_review_calls` 判定与配置一致 | 新增 |
| `dedup` | 同 cohort 最多一条 open | 新增 |
| `push_dispatch` | push 路径命令派发（此前从未被触发） | 新增 |
| `weekly_notify_gate` | `--push` + `DRY_RUN=true` 仍是 dry_run（补上 catalog 声称存在却缺失的断言） | 新增 |
| **`apply_closure`** | **真实 Apply 闭环**：真改文件 → commit 到源分支 → 等 telemetry `state=applied` → 断言有 adopted action | **新增（E1）** |
| **`reconcile_resolved`** | **reconciler 链路**：GitLab API resolve discussion → 跑 `sync_resolved_suggestions.py` → 断言 DB `state=resolved` | **新增（E3）** |
| `weekly_report` | 周报产出 + **外发门控断言** + **live 字段断言**（标题/emoji/dashboard 链接/分片数，E13） | 改造 |

> **执行顺序**：会改状态的 flow（`apply_closure` / `reconcile_resolved`）排在审计类断言**之后** ——
> 否则 apply_closure 改文件导致 MR head 变化，会污染审计基线产生**假失败**（修复前实测 16/20）。
> 实现见 `flows.STATE_MUTATING_FEATURES` + `run_e2e.py` 的收尾阶段。

---

## 2. 验证方法论：三层证据

| 层 | 手段 | 覆盖 | 运行成本 | 门禁位置 |
|---|---|---|---|---|
| **L1 单元测试** | `pytest` 直接打函数/类 | 纯逻辑、边界、错误路径 | 秒级 | CI 必过 |
| **L2 E2E 契约测试** | `scripts/e2e/run_e2e.py --tier fast/full` | 配置生效性、跨模块契约、真实产物形态 | fast 秒级 / full 分钟级 | 定时 + 手工 |
| **L3 真实业务验证** | 真实 GitLab + 真实 LLM + 真实队列 + 真实钉钉 | 端到端行为、UI 可见效果 | 高（真实调用） | 本文档 |

**证据强度排序**：L3 > L2 > L1。L1 全绿**不代表**功能可用 —— 本轮 4 个新缺陷里有 3 个是 L1 测不到、L2 会误报的（详见 §7）。

---

## 3. 验证流程

### 3.1 环境准备

```bash
cd /Users/jarvs/ReviewAgent
set -a && . config/instance.env && set +a   # 实例配置（.gitignore，权限 600）

# 重启全套服务（必须补 PATH/REDIS_BIN）
PATH="/opt/homebrew/bin:$PATH" \
  REDIS_BIN=/opt/homebrew/bin/redis-server bash scripts/restart_local.sh
```

服务栈：web `:3000`、Redis `:6380`、review worker ×3、weekly worker、launchd weekly 定时器。

### 3.2 三层验证的执行顺序

```
① L1 单元测试        pytest -q                          → 1499 passed
② L2 E2E 快速档     run_e2e.py --tier fast             → 21/21 witnesses
③ L2 E2E 全量档     run_e2e.py --tier full             → 临时项目 + 真实 LLM
④ L3 真实业务验证   见 §4
```

### 3.3 L2 E2E 框架结构

```
scripts/e2e/run_e2e.py (唯一入口)
├── --tier fast  → config_audit.run_fast
│                  ├── catalog.static_rows()   91 个 schema 字段的生效证据目录
│                  └── quick_witnesses.QUICK   21 个进程内行为 delta
└── --tier full  → stack.probe() 前置检查
                   → fixtures.create_review_fixture()  每轮建临时项目/MR
                   → flows.FLOWS[feature]              14 个功能流
                   → config_audit.observable_checks()  配置在真实产物上可观测
                   → telemetry_audit.run()              6 张表字段级对账
                   → real_config.run()                  真实 diff 上的规划断言
```

产物：`logs/e2e/report-<stamp>.json` + 每用例明细 `logs/e2e/run-<slug>-r<round>-<stamp>.log`。

**锁策略**：`stack.clear_ephemeral_locks` 只清 `cooldown` / `max_review_calls` / `diff_head`，
**绝不删** `reviewagent:lock:*`（链锁，删了会放行重复链，历史上因此出过并发重复检视）。

### 3.4 新增一个 E2E 用例的标准步骤

| 步 | 动作 | 落点 |
|---|---|---|
| 1 | 判定用例类型（功能链 / 字段级审计 / 配置生效 / 进程内 delta） | — |
| 2 | 实现函数，签名固定 `(mr_iid, round_idx, log) -> FeatureResult` | `scripts/e2e/flows.py` 或 `telemetry_audit.py` / `real_config.py` / `quick_witnesses.py` |
| 3 | 注册到 `FLOWS` 字典（唯一注册点） | `scripts/e2e/flows.py: FLOWS` |
| 4 | 同步 `EXPECTED_FEATURES`（否则注册表守卫测试会红） | `tests/test_verify_e2e_smoke.py` |
| 5 | 纯断言部分补单元测试 | `tests/test_e2e_*.py` |
| 6 | 跑 `--tier fast`（秒级兜底）→ `--tier full --only <新用例>` | — |

---

## 4. L3 真实业务验证流程（本轮实跑）

### 4.1 样本构造

| 项 | 值 |
|---|---|
| 业务仓库 | 本地 SSD 自动化代码 `/Users/jarvs/Code/dml_epc_auto-main` |
| GitLab 项目 | p31 `review-bot-v2/ssd-real-biz-full`（10477 文件，字节级一致） |
| 违规构造 | 同时违反仓库规则 `SSD-RULE-*`、动态跨文件规则 `DYN-CROSS-FILE-REFS`、通用规则 `R-*` |
| MR | !2（基线 11 条）、!3（复验 15 条，head `82de89a`，3 个 commit） |

### 4.2 实跑链路

```
push / MR open
   → webhook 鉴权 → 机器人回环检查 → 冷却 → 限流 → 命令链派发
   → RQ 队列 → worker → workspace 准备 → LLM 调用 → 规则校验/去重
   → 三态 applicability 判定 → 发布 inline（GitLab Discussion）
   → 实时刷新顶部「检视汇总」（图1）
   → telemetry 落库（suggestions / review_runs / suggestion_actions / reviewed_file_state）

用户交互（真实浏览器）
   → Apply suggestion  ──┐
   → /adopt [理由]      ──┼→ note webhook → 校验 → 状态翻转 → resolve → 汇总表刷新
   → /dismiss [理由]    ──┘
```

### 4.3 实跑结果

**图1（note 1109「检视汇总」）逐格核对：**

> **口径说明**：图1 与周报的「采纳率」**均为 `已采纳 / 建议总数`（含待处理）**，
> 实现见 `reviewagent/commands/_common.py:502` 与 `reviewagent/telemetry/store.py:1696`。
> 因此 2 条已采纳 / 15 条建议 = **13.3%**，而不是「已采纳 / 已处理」口径的 66.7%。
> 两处口径一致，复验脚本亦已按此口径核对通过。

| 维度 | 图1 显示 | telemetry 真值 | 一致 |
|---|---|---|---|
| 总建议数 | 15 | 15 | ✅ |
| 采纳率 | 13.3% | 2 / 15 = 13.3% | ✅ |
| HIGH | 8 待 + 2 采纳 = 10 | high 10 | ✅ |
| MEDIUM | 4 待 + 1 忽略 = 5 | medium 5 | ✅ |
| 状态总计 | 12 / 2 / 1 / 0 | open 12 / applied 2 / dismissed 1 | ✅ |

**跨系统 1:1：** telemetry 15 条 ↔ GitLab 15 个 inline discussion，无多无缺；`note_id ⊆ discussion_id` 缺失 0；GitLab resolve 状态 vs telemetry `state` 不一致数 = 0。

**图2 分类分布：** applicability `replace` 12 / `text_only` 2 / `delete` 1；severity high 10 / medium 5。

**三条交互闭环（真实浏览器点击，非 API 模拟）：**

| 交互 | 结果 | telemetry 落库 |
|---|---|---|
| Apply suggestion | note 1117，commit `af38c5b` | `state=applied` / `adoption_source=ui_apply` |
| `/dismiss` | note 1125+1126（机器人回执） | `state=dismissed` |
| `/adopt` 正向 | note 1139+1140（机器人「✅ 已采纳建议」） | `state=applied` / `adoption_source=adopt_command` / `validation_status=ok` |
| `/adopt` 反向 | note 1127+1128（明确拒绝「未检测到代码修改」） | `validation_status=target-unchanged`，状态保持 `open` |

### 4.4 其余功能实跑

| 功能 | 验证方式 | 结果 |
|---|---|---|
| 周报 | `scripts/weekly_report.py --project-id 31 --week-offset 0` | 3 个 collector 全 ok；artifact `weekly-2026-W46.json`(7387B)/`.md`(2237B)；本周 26 条建议（MR!2 的 11 + MR!3 的 15），high 20 / medium 6，累计采纳率 7.7% |
| `/describe` 显式刷新 | 浏览器 MR 评论框发 `/describe` | run id=70 `triggered_by=note / success / 58s`；MR 描述按当前 head 重生成；push 触发的 describe 被 `already_described` 跳过 |
| `/improve` 增量 | V2(0新/11跳过) → V3(3新) → V4(1新) → adopt(0新) | dedup 稳定生效 |
| reconciler | `reconcile_late_detect.py --project-id 31 --mr-iid 3 --dry-run` | `scanned=0 applied=0 errors=0`；`--all` → 无需 reconcile |
| 冷却 | 连续触发 | 4 次 `webhook.skip cooldown … cmd=describe` |
| 限流 | `max_review_calls_per_mr=30`，MR !3 计数 10 | 未触发；用合成阈值复验 `skip=True` 逻辑正确 |
| 人工修复链路 | commit `b93af25`（超时外置）+ `82de89a`（blk_id） | push 后 auto_detect **未抢跑**，增量复检按预期发新建议 |

---

## 5. 验证点清单

> 「单元」= L1；「E2E」= L2；「实跑」= L3；`—` 表示该层不适用。

### 5.1 API 层（20 个端点）

| 端点 | 单元 | E2E | 实跑 | 备注 |
|---|---|---|---|---|
| `GET /health` | ✅ | ✅ | ✅ | |
| `GET /runs` | ✅ | ✅ | ✅ | |
| `GET /runs/{run_id}` | ✅ 新增 | — | ✅ | 404 分支已覆盖 |
| `GET /mr/{p}/{i}` | ✅ 新增 | — | ✅ | provider 字段已覆盖 |
| `GET /mr/{p}/{i}/suggestions` | ✅ 新增 | ✅ | ✅ | 含 state/adoption_source 标签 |
| `GET /mr/{p}/{i}/runs` | ✅ 新增 | — | ✅ | |
| `GET /mr/{p}/{i}/stats` | ✅ 新增 | — | ✅ | |
| `GET /mr/{p}/{i}/timeline` | ✅ 新增 | ✅ | ✅ | 三表 UNION |
| `GET /mrs` | ✅ 新增 | ✅ | ✅ | 时间窗按 created_at |
| `GET /mrs/{p}/{i}` | ✅ 新增 | — | ✅ | 别名路由 |
| `GET /summary` | ✅ 新增 | — | ✅ | |
| `GET /metrics/overview` | ✅ 新增 | — | ✅ | |
| `GET /metrics/severity` | ✅ 新增 | — | ✅ | |
| `GET /metrics/rules` | ✅ | ✅ | ✅ | 真实 rule_key 而非 severity |
| `GET /metrics/authors` | ✅ | ✅ | ✅ | |
| `GET /dismissals` | ✅ | ✅ | ✅ | rule_key 过滤已覆盖 |
| `GET /dismissals/by-rule` | ✅ 新增 | — | ✅ | |
| `GET /mrs/{p}/{i}/dismissals` | ✅ 新增 | — | ✅ | |
| `GET /weekly-reports` | ✅ 新增 | — | ✅ | |
| `GET /weekly-reports/{name}` | ✅ 新增 | — | ✅ | 路径穿越拦截 + 404 |
| `_parse_iso` 归一 | ✅ 新增 | — | — | 非法值 → 400（见 D6） |

### 5.2 Webhook 与命令派发

| 验证点 | 单元 | E2E | 实跑 |
|---|---|---|---|
| 签名校验（缺 header / token 不匹配 → 401） | ✅ | — | ✅ |
| 机器人回环识别 | ✅ | — | ✅ |
| MR 事件（open/update/close/merge/draft/非 opened） | ✅ | ✅ | ✅ |
| Push 事件（ref 校验 / 定位 MR / 入队 push_commands） | ✅ | ✅ **新增** | ✅ |
| Note 事件：非 MR note / bot 自触发 | ✅ | — | ✅ |
| Note 事件：命令解析 `/describe` `/improve` | ✅ | ✅ | ✅ |
| Note 事件：未知命令 → ignored | ✅ | — | ✅ |
| Note 事件：`/adopt` `/dismiss` 线程绑定 | ✅ | ✅ | ✅ |
| Note 事件：DiffNote 无 discussion_id → file:line 兜底 | ✅ | — | ✅ |
| Note 事件：MR-level note 多/零 open suggestion 提示 | ✅ | — | — |
| 系统 DiffNote「changed this line」→ applied | ✅ | ✅ | ✅ |
| 系统 note「resolved」→ sync_resolved | ✅ | — | ✅ |
| 冷却（MR 级 / suggestion 级） | ✅ | ✅ **新增** | ✅ |
| 限流 `max_review_calls_per_mr` | ✅ | ✅ **新增** | ✅ |
| 链锁（`reviewagent:lock:*` 永不删） | ✅ | ✅ | — |

### 5.3 命令与发布

| 验证点 | 单元 | E2E | 实跑 |
|---|---|---|---|
| `/describe` 发布标题+描述 | ✅ | ✅ | ✅ |
| `/describe` already 守卫 | ✅ | ✅ | ✅ |
| `/improve` 规划（配额/抽样/权重/过滤） | ✅ 20+ witness | ✅ | ✅ |
| `/improve` 规则键提取与引用 | ✅ | ✅ | ✅ |
| `/improve` 去重（fingerprint / cohort / line） | ✅ | ✅ **新增** | ✅ |
| **applicability 三态**（replace/delete/text_only） | ✅ 11 例 | ✅ **新增** | ✅ |
| text_only 不得带 suggestion 块（否则冒出 Apply 按钮） | ✅ | ✅ **新增** | ✅ |
| delete 保留 Apply + 显式删除警告 | ✅ | ✅ **新增** | ✅ |
| general 分支落 telemetry | ✅ | ✅ | ✅ |
| Apply 风险检查（apply_risk） | ✅ | — | ✅ |
| 图1 汇总表构建与实时刷新 | ✅ | ✅ | ✅ |

### 5.4 建议动作（adopt / dismiss / apply）

| 验证点 | 单元 | E2E | 实跑 |
|---|---|---|---|
| `/adopt` 校验通过 → applied | ✅ | ✅ | ✅ |
| `/adopt` 校验失败 → 拒绝 + 状态不变 | ✅ | ✅ | ✅ |
| `_target_region_changed` 目标区域变更检测 | ✅ | — | ✅ |
| `_strict_token_adoption_match` 严格 token 匹配 | ✅ | — | ✅ |
| `/dismiss` → dismissed + reason 落库 | ✅ | ✅ | ✅ |
| Apply（UI）→ applied | ✅ | ⚠️ 仅响应形态 | ✅ |
| auto_detect_applied 自动探测 | ✅ | — | ✅ |
| race 保护 / 幂等 | ✅ | — | ✅ |
| `suggestion_actions` 计数过滤（失败采纳不计入） | ✅ | — | ✅ |

### 5.5 周报

| 验证点 | 单元 | E2E | 实跑 |
|---|---|---|---|
| 采集器（telemetry / merged_mrs / repo_scan） | ✅ | ✅ | ✅ |
| LLM 摘要生成 | ✅ | — | ✅ |
| artifact（json/md/xlsx）落盘 | ✅ | ✅ | ✅ |
| **外发门控（显式 `--push` 且未开 dry_run 才真发）** | ✅ 3 例 | ✅ **新增** | ✅ |
| 分片（`weekly_md_chunk_limit`） | ✅ | — | ✅ |
| 时区周边界 | ✅ | ✅ | — |
| 标题/emoji/dashboard 链接 | ✅ | — | ✅ |

### 5.6 遥测存储

| 验证点 | 单元 | E2E | 实跑 |
|---|---|---|---|
| 6 张表写入与字段级对账 | ✅ | ✅ | ✅ |
| `last_activity_at` MAX 单调 | ✅ | — | ✅ |
| 孤儿 run 清理 | ✅ | — | ✅ |
| token/成本/时长/规则键/建议数落库 | ✅ | ✅ | ✅ |
| 规则键 Top-N 计数 | ✅ | ✅ | ✅ |

### 5.7 场景包与规则

| 验证点 | 单元 | E2E | 实跑 |
|---|---|---|---|
| profile 加载/合并/校验/指纹 | ✅ | ✅ | ✅ |
| 规则包解析（表格/结构化/applies_to） | ✅ | — | ✅ |
| 规则键命名空间（builtin vs repo） | ✅ | ✅ | ✅ |
| 仓库规则递归扫描 + 行数预算 | ✅ | ✅ | ✅ |
| 动态规则（DYN-*）加载与校验 | ✅ | — | ✅ |
| prompt 模板渲染与路径逃逸防护 | ✅ | — | ✅ |

### 5.8 基础设施与运维

| 验证点 | 单元 | E2E | 实跑 |
|---|---|---|---|
| 日志脱敏 / 不可信内容净化 / worktree 隔离 | ✅ | — | ✅ |
| 部署单元（systemd/launchd plist） | ✅ | — | ✅ |
| LLM provider 适配（qoder_sdk / qodercli / opencode） | ✅ | ✅ | ✅ |
| 性能基线（replay / perf） | ✅ | — | ✅ |
| prompt golden | ✅ | — | ✅ |

---

## 6. 防漏测机制（回归护栏）

| 机制 | 位置 | 作用 |
|---|---|---|
| schema ↔ catalog 双向对齐 | `tests/test_config_catalog.py` | 新增配置字段未登记生效证据 → 测试红 |
| behavior ↔ witness 双向对齐 | `tests/test_config_catalog.py` | 登记了但不实现的 witness → 测试红 |
| **live ↔ 断言绑定/记账** | `tests/test_config_catalog.py::test_live_fields_are_bound_or_accounted` | 55 个 live 字段必须**要么**有专属 E2E 断言（已绑定 15），**要么**进缺口台账（已记账 40）—— 新增 live 字段两者都不做 → 红（本轮新增） |
| 证据引用有效性 | `tests/test_verify_e2e_smoke.py` + `config_audit._evidence_problem` | 符号失效 → 红；**数字锚点越界/指向空行 → 红（本轮增强）** |
| API 端点登记守卫 | `tests/test_telemetry_api_contract.py::test_every_route_is_registered` | 新增端点未进契约表 → 红（本轮新增） |
| E2E flow 注册守卫 | `tests/test_verify_e2e_smoke.py::test_flow_registry_covers_all_features` | 新增 flow 未登记 → 红（本轮扩展） |
| E2E 外发安全闸 | `tests/test_verify_e2e_smoke.py::test_real_push_is_opt_in_only` | E2E 真发钉钉必须显式 opt-in（本轮新增） |
| **model 语义单一性** | `tests/test_review_run_model_fields.py` | `model`（provider 回报）与 `model_configured`（配置 id）混用 → 红；skipped 运行冒充模型 → 红（本轮新增） |
| **成本信号语义** | `tests/test_e2e_cost_signal_audit.py` | 恒等式改为「provider 回报模型 ⇒ 必须有成本信号」等 4 条语义规则（E11 本轮新增） |
| **E2E 改状态流程隔离** | `flows.STATE_MUTATING_FEATURES` + `run_e2e.py` 收尾阶段 | 改状态的 flow（apply_closure / reconcile_resolved）必须排在审计类断言之后 —— 否则污染审计基线产生假失败（本轮新增） |
| **清理脚本删除安全** | `tests/test_cleanup_e2e_projects.py` | 保护名单失效 / 命名匹配放宽 → 误删真实业务项目 p31 → 红（本轮新增，26 例） |
| 架构边界 | `tests/architecture/` | 语言字面量、规则命名空间越界 → 红 |

---

## 7. 缺陷台账

### 7.1 第一轮新发现并修复（D5–D9）

| 编号 | 缺陷 | 影响 | 修复 | 回归 |
|---|---|---|---|---|
| **D5** | E2E 字段级审计不认识 applicability 三态，把 `text_only` 建议误判为「缺少 suggestion 块」 | **真实业务 MR !3 上 2/15 条假阳性** —— `--tier full` 会在**正确行为上报红**，E2E 门禁失效 | `scripts/e2e/telemetry_audit.py::_improved_code_problems` 改为按三态分别校验；`audit_suggestions` 增加 applicability 取值校验 | `tests/test_e2e_telemetry_audit.py` +7 例 |
| **D6** | `_parse_iso` 对**任意 10 字符串**直接当日期放行（`since=not-a-date` 返回 200 而非 400） | 非法时间参数静默通过 → 查询按错误时间窗返回数据，而不是明确报错 | `reviewagent/api/router.py::_parse_iso` 用 `strptime` 严格校验 `YYYY-MM-DD` | `tests/test_telemetry_api_contract.py` |
| **D7** | catalog 的**数字行号锚点不被校验**，改代码后静默漂移到无关行 | `reporting/runner.py` 的 11 个行号锚点**全部漂移**，证据目录却仍显示"绿" —— 生效证据体系形同虚设 | ① runner.py 相关条目改用**符号锚点**（会被真校验）；② `_evidence_problem` 增加「数字锚点必须指向非空行且不越界」校验；③ `cli.py:66` 修正为符号锚点 | `tests/test_verify_e2e_smoke.py::test_witness_evidence_refs_resolve` |
| **D8** | E2E 周报流程在 round≥2 带 `--push`，而本地 `REVIEWAGENT_WEEKLY_DINGTALK_DRY_RUN=false` 且带真实钉钉 webhook URL | **E2E 会把周报真推到团队群**（外发副作用，不可撤销） | ① E2E 默认一律 dry_run，真发需显式 `REVIEWAGENT_E2E_ALLOW_REAL_PUSH=1`；② 新增 `weekly_notify_gate` 流程断言 `--push` + `DRY_RUN=true` 仍是 dry_run；③ 周报流程解析 stdout 断言 delivery 为 dry_run | `tests/test_verify_e2e_smoke.py` +2 例 |
| **D9** | `/describe` 改写 GitLab 上的 MR 标题后，**不回写** telemetry 的 `mr_activity.title`（该字段写的是检视开始前的旧标题） | telemetry 与 GitLab **永久漂移**：dashboard 一直显示旧标题；同一 MR 第二次 describe 换个说法就对不上；E2E `telemetry:mr_activity` 审计持续报错（**全量档实跑 29/30 的唯一失败项**） | 新增 `Store.update_mr_title()` + `events.emit_mr_title()`；`DescribeCommand._publish` 改完标题即同步（GitLab 改标题失败时保持旧值） | `tests/test_describe_title_sync.py`（4 例） |

### 7.1b 第二轮（重构后）新发现并修复（D10–D14）

明细见 **§12.2**；一句话版本：

| 编号 | 缺陷 | 影响 | 修复 |
|---|---|---|---|
| **D10** | E2E 断言 `conftest_not_reviewed` 把「conftest 一律不检视」写死 | 策略改为「conftest 参与检视」后，正确行为被判失败（全量档 29/30） | 断言改为**策略驱动**（按 `improve_skip_test_paths` 判定违规），改断言不改业务代码 |
| **D11** | 「删除型」判定排在 `resolved is not True` 提前 return 之后 | 用户点 Apply 删了代码但没 resolve 线程 → 永远无法自动判定为已采纳 | delete_range 判定提到 resolved 分支之前求值 |
| **D12** | 删除型采纳证据 =「原文案不在文件里」 | 用户只是**改写**那段代码也被判采纳 → 假阳性、采纳率虚高 | 新增 `_deleted_region_confirmed()`：行级 diff 的 delete 操作码判定「真被删」 |
| **D13** | `text_only` 被纳入「删除语义」 | text_only 无可机检落地形态，「原代码消失」不构成采纳证据 | delete_range 只对 `applicability == "delete"`（及历史无该列的等价形态）生效 |
| **D14** | `cohort_key` 粒度只有「文件 + 标题」 | 同文件里**不同位置**、标题文字相同的不同问题被并成一个 cohort → 图1/周报总数少算、采纳率虚高，且被并掉的建议仍是 `open`（图1 与 GitLab 可见数自相矛盾） | cohort 键加入**代码内容指纹**：`_cohort_key(file, header, existing_code)`（仍不用行号 —— 行号会漂移） |
| **D15** | 单元测试**依赖部署环境变量**（`REVIEW_PROFILE` / `QODER_SDK_EDITION` / `LLM_PROVIDER` 等） | 带着 `config/instance.env` 跑 `pytest` → **41 failed**（干净环境只有 10 failed）：多出的 31 例全是污染造成的假失败（`QODER_SDK_EDITION` 让 `Config.from_env()` 在 import 期抛 ValueError → **整个测试文件 collection error**）。门禁数字取决于开发者 shell 状态 → 不可信 | `tests/conftest.py` 增加 `_DEPLOY_ENV_KEYS` 清理清单，**在 import 期**（`Config.from_env()` 绑定配置之前）+ 每个测试前各清一次；新增 `tests/test_conftest_env_isolation.py`（4 例）守门 |
| **D16** | 「收缩建议」走 general 分支（**刻意不给 Apply 按钮**），却落库记 `replace`，且正文复用含「点上方『应用建议』按钮」的页脚 | ① 落库 `replace` 与正文形态矛盾（replace 的契约是「有 suggestion 块」）→ E2E 字段级审计报红 `replace 正文缺 suggestion 块` / `improved_code diverges from posted note`；② 正文**提示用户去点一个不存在的按钮**（D1 的反向问题）；③ 落库 `improved_code` 让 auto_detect 把它当可 Apply 目标 | general 分支与 `text_only` **同构**：正文改用无 Apply 引导的页脚、落库 `applicability=text_only`、不落 `improved_code`（`PublishCallbacks` 新增 `help_footer_text_only`） |

### 7.1c 第三轮（「总结修复 + 补齐剩余项」）新发现并修复（E1–E13）

本轮先逐项核实上一轮遗留清单（**每条都做了实证核实，不靠推测**），确认属实后才动手。

#### 7.1c.1 真正的缺陷修复

| 编号 | 缺陷 | 实锤证据 | 修复 | 回归 |
|---|---|---|---|---|
| **E8** | `review_runs.model` 一列**两种口径混用**：skipped 行走配置 id（`deepseek/deepseek-v4-flash`）、成功行走 provider 回报值（`DeepSeek-Flash`） | 真实库直查：success 状态下两种取值并存（110 条 `DeepSeek-Flash` vs 58 条 `deepseek/deepseek-v4-flash`）；根因是 `_common.py` 用 `or self.model` 把配置 id 冒充成「实际模型」 | 拆两列：`model`（**provider 实际回报**，未回报为 NULL）+ `model_configured`（请求时配置 id，success/skipped/failed 都记）；skipped 运行的 `model` 置空（没调 LLM，任何模型名都是假的） | `tests/test_review_run_model_fields.py`（8 例，含 store 层 + 命令层 + HTTP 层） |
| **E11** | token 断言**只做恒等式**（`total == prompt + completion`），恒成立 → 永远抓不到任何真实缺陷 | 恒等式由 `finish_run` 的同一行 SQL 写出，**不可能不成立**；而 p31 上 33 个 run 的 `total_tokens` 全部为 0（同时 17 个 `cost_credits > 0`）—— 恒等式对此完全无感 | `audit_review_runs` 改为语义断言：① provider 回报了模型 ⇒ **必须**有成本信号；② 无模型回报 ⇒ **必须**既无 token 也无 credits；③ 计量值不得为负；④ **整体**至少一个 success run 带成本信号（防「计量链路整体静默失效」）。依赖 E8 的 `model` 语义拆分才能成立 | `tests/test_e2e_cost_signal_audit.py`（10 例） |

#### 7.1c.2 覆盖补齐（实现本就正确，但此前**无测试**，改坏了不会有任何测试变红）

| 编号 | 补齐项 | 为什么这类缺口危险 | 补法 |
|---|---|---|---|
| **E10** | MR 事件的 `close` / `merge` / `draft` / 未知 action 分支 | 这几条分支的正确语义是「**不**触发检视」—— 改坏（close 误触发 review、draft 漏跳过）不会有任何测试变红，且都属于「静默不响应」类故障 | `tests/test_webhook_mr_action_branches.py`（13 例）：close/merge 不 enqueue、state≠opened 不 enqueue、close 仍 upsert MR 元信息、draft 不触发且**不写 diff_head 锁**（对照组证明锁逻辑确实跑到）、未知 action 跳过、bot 自触发最先短路 |
| **E12** | 跨轮次 supersede 的**完整链路** | 此前只有单点覆盖（只验「旧条变 superseded」/「已修好则不动」），缺「V1 applied → V2 发布 → supersede → cohort 归并不双计 → generation 递增 → 反向链正确」的端到端断言 | `tests/test_cross_round_supersede.py`（12 例）：主链路 + generation 递增 + 归并只留最新 + 审计计数 + 4 个边界（dismissed 也 supersede / open **不**supersede / 已修好不 supersede / GitLab 故障非致命 / 其他 cohort 不受影响）+ 三代链 |
| **E13** | 周报 live 字段（标题 / emoji / dashboard 链接 / 分片数） | E2E 只断言「json + md 文件存在」，产物**内容**全靠人眼看 —— 标题写错、emoji 丢、dashboard 没渲染成 markdown 链接、分片数对不上，都不会变红 | `flows._audit_weekly_live_fields` + `tests/test_weekly_live_field_audit.py`（13 例）：artifact 字段非空、markdown 首行标题含 emoji+标题+week_label、dashboard 必须是 markdown 链接形态、分片数 ≥ 1、JSON 损坏报红不抛异常、多周残留取最新 |

#### 7.1c.3 本轮新增的 E2E 流程（补真实链路断言）

| 编号 | 流程 | 覆盖的真实链路 | 结果 |
|---|---|---|---|
| **E1** | `apply_closure` | 真改文件 → `fixtures.commit_files` commit 到源分支 → 等 telemetry `state=applied` → 断言该 note 有 adopted action | ✅ 实跑通过（`state=applied adopted_actions=1 applicability=replace`） |
| **E3** | `reconcile_resolved` | GitLab API resolve discussion → 跑 `scripts/sync_resolved_suggestions.py --project-id --mr-iid` → 断言 DB `state=resolved` | ✅ 实跑通过（首次试跑因缺 `--mr-iid` 致 `sync_exit=2`，已修参数） |
| **E6** | provider 诊断日志 | usage 出现但 token 全为 0 时打 `qoder_sdk.usage_tokens_unavailable` warning（含 usage 原始键） | ✅ 已实现（§7.3 待办的第 ① 项） |
| **E7** | 时间线 `effective` 列 | 时间线端点新增 `effective` 列，Python 侧复用 `store.is_effective_action` 单一事实源（不在 SQL 里复制规则，防漂移） | ✅ 42 例契约测试通过 |

**流程顺序修复**：会改状态的 flow（`apply_closure` / `reconcile_resolved`）必须排在审计类断言（config:*/telemetry:*/config_real:*）**之后** —— 否则 apply_closure 改文件导致 MR head 变化，会污染审计基线产生**假失败**（修复前实测 16/20）。已在 `flows.STATE_MUTATING_FEATURES` + `run_e2e.py` 的收尾阶段实现。

**push 路径排查结论（非缺陷）**：E2E 用 `reviewagent-e2e-bot` PAT 提交 → push actor 是机器人 → `router._handle_push` 的 `locks.is_bot(actor)` 跳过 —— 这是**设计行为**。真实用户 push 时 actor 非 bot 会正常触发（真实业务 MR !4 实测 `push.auto_detect_applied applied=1`）。

#### 7.1c.4 O5 清理脚本

GitLab 上 E2E 临时项目堆积（实测 **34 个 `deletion_scheduled`** + 4 个正常态残留 + root 名下的历史遗留）。
新增 `scripts/ops/cleanup_e2e_projects.py`：
- **默认 dry-run**，真删需显式 `--apply`；
- 只删**名字精确匹配 E2E 命名模式**且 **owner 是当前 token 所属用户** 的项目；
- **保护名单优先**：任何路径含 `real-biz` / `ssd-review-demo` / `ssd-dynamic-proof` / `manual-review` / `testmate-extensions` 一律不删 —— p31 真实业务项目与 MR !3 的来源副本都在其中；
- 已 `deletion_scheduled` 的跳过（GitLab 自己会清）。

回归：`tests/test_cleanup_e2e_projects.py`（26 例）—— 含「命名不完全匹配就不删」「保护名单每个片段都必须生效」「p31 项目永不可删」的守卫。

### 7.1d 第四轮（降噪规则包落地 + 真实业务 A/B）新发现并修复（N1–N6）

> 详细证据、逐条实锤与三轮对照见 **§15**。本节只做台账索引。

#### 7.1d.1 真正的缺陷修复

| 编号 | 缺陷 | 实锤证据 | 修复 | 回归 |
|---|---|---|---|---|
| **N1** | `rules/general_rules.md:86` 残留「所有 severity 都产 suggestion」—— 与降噪边界包的 `low` 两问自检**直接矛盾**（LLM 收到两套互斥指令） | 规则包正文 + `improve.md` 交叉比对 | 改为「确有影响时所有 severity 都可产 suggestion；其中 `low` 须先通过 low 两问自检」 | `tests/test_noise_boundary_contract.py::test_general_rules_has_no_unbounded_mandate` |
| **N2** | `prompts/improve.md` 的「一律产出 suggestion」/「每个可疑 bug 都必须有一条 suggestion」与边界包冲突 | 提示词全文比对 | 改为「每个**有影响**的问题」，并注明强制例外不受降噪影响 | 同上（`test_improve_prompt_has_no_unbounded_mandate`） |
| **N4** | **过度降噪**：边界开启时一条**会误导**的过期注释被整条裁掉，而关闭边界的基线反而报了出来 —— 边界比基线还漏报 | 真实业务 A/B（同一份 diff，MR !5 边界开 / MR !6 边界关，head 均为 `00347d8`）：基线产出 `R-OTHER:stale_comment`，边界开启时两轮均未产出 | 根因：「仍要报」只写在不报清单的**表格单元格里**，不在模型优先读的「强制例外」清单中 → 把「会误导的过期注释」**提升为强制例外的独立条目**，并在「判定顺序」第 1 条写明「命中强制例外的候选先跳过不报清单」 | `test_misleading_comment_is_first_class_exception` + 真实业务第 3 轮复检**找回该条**（`R-OTHER:stale_comment`「注释与实现矛盾」） |
| **N6** | **数据面**：`cohort_key` 口径迁移把 MR !4 的独立问题数从 23 修正为 30（DB 对了），但**已发布的「检视汇总」note 仍停留在旧口径 23** → 图1 与图2 对不上 | MR !4 L3 从 20/20 掉到 **16/20**（4 项全在图1 对账）；GitLab 上实际 inline 评论一直是 30 条 | 新增 `scripts/ops/refresh_overviews.py`（默认 dry-run；`--apply` 用幂等的 `publish_overview` 重刷，不触发检视） | `tests/test_refresh_overviews.py`（9 例）+ 对 p31 跑一次后 **L3 恢复 20/20** |

#### 7.1d.2 覆盖补齐（实现本就正确，但此前**无测试**）

| 编号 | 补齐项 | 为什么这类缺口危险 | 补法 |
|---|---|---|---|
| **N3** | 契约测试只查「边界文件存在」，**没查边界是否真的进了 prompt** | 装配 / 渲染环节一旦静默丢弃边界包 → 降噪完全失效，而所有文件级测试照样绿 | `test_boundary_pack_is_actually_injected_into_improve_prompt`（查 `ImproveCommand._rule_block_text()` 实际内容与位置）+ `test_low_must_pass_boundary_self_check` |
| **N5** | 边界与「仓库自定义规则」的**相对位置**没被锁 | 顺序改坏会出现两种静默失效：边界形同虚设，或仓库规则被误裁 | `test_boundary_gates_general_rules_only`：锁死两条分支的物理位置（无仓库规则 → 边界最前；有仓库规则 → 仓库规则 → 边界 → 通用规则，且仓库规则段的「命中即产 suggestion」必须保留） |

---

### 7.2 前序缺陷（D1–D4，已修复）
| 编号 | 缺陷 | 修复 |
|---|---|---|
| **D1** | applicability 单点：空建议块仍渲染 Apply 按钮 | 引入三态 `replace/delete/text_only`（新增 `reviewagent/review/applicability.py`） |
| **D2** | general 分支发现未落 telemetry（GitLab 可见但图1 不计、无法 `/adopt`） | 抽出 `_record_published_suggestion()`，post/general 共用 |
| **D3** | improve prompt 契约缺 `applicability` 字段 | `profiles/ssd-python-autotest/prompts/improve.md` 补字段说明/示例/约束 |
| **D4** | 周报门控 `push or not dry_run` → **手动本地执行也真发钉钉** | 新增 `_should_send_notice()`，真发收紧为「显式 `--push` 且未开 dry_run」；调度路径（launchd `--push`）行为不变 |

### 7.3 根因定位：token 用量恒为 0（**非 ReviewAgent 缺陷**）

**现象**：`review_runs.total_tokens / prompt_tokens / completion_tokens` 全部为 0。

**排查链路**：
1. `LLMResult` 有 `prompt_tokens` / `completion_tokens` 字段 ✅
2. `commands/_common.py:299` 取 `self._last_oc_result.prompt_tokens` 传入 ✅
3. `events.emit_run_finished` → `store.finish_run` 写库 ✅
4. 真实日志 `qoder_sdk.usage agent=improve model=DeepSeek-Flash pt=0 ct=0 credits=9.0089 ctx_ratio=0.5849` ❌

**结论**：`credits` 与 `context_usage_ratio` 取自**同一个 usage 字典**，二者非零而 `input_tokens`/`output_tokens` 为零 →
**上游 Qoder 服务端的 usage 响应未回填 token 字段**，ReviewAgent 侧管道完整。

SDK 侧 `_merge_usage` 确实读 `input_tokens`/`output_tokens`（SDK `types.py` 的 `NonNullableUsage` 有这两个必填字段），
说明只要服务端回填，链路即可透传。

**待办**：① ~~加一条 provider 诊断日志（打印 usage 原始键）~~ → ✅ **本轮已做（E6）**：`reviewagent/llm/qoder_sdk_provider.py` 收集 `usage_keys`，当 usage 出现但 token 全为 0 时打 `qoder_sdk.usage_tokens_unavailable` warning（含 usage 原始键）；
② 若上游长期不提供，把 `total_tokens` 语义从「0」改为「不可用（NULL）」，避免消费者把 0 当真实值。

**本轮补充的认知（E11 追查结果）**：追查时发现 p31 上「零信号 success run」其实分两类，**不能一刀切**：
- **全复用早退**（`reviewagent/review/runner.py` 的 `all_reused` 分支）：本轮**根本没调 LLM**，token/credits 为 0 是**正确**的 —— 这类 run 的 `model` 也是 NULL。
- **真调了但上游不回填**：有 `cost_credits`，`total_tokens` 为 0 —— 这才是需要关注的。

E8 把 `model` 拆成「provider 回报值」后，这两类**首次可区分**（前者 `model` 为空，后者有值），
E11 的断言正是建立在这个区分之上。② 的 NULL 语义改造因此**降级为 provider 诊断日志**（E6）——
跨模块改语义风险大、收益有限，先用日志确认上游到底回了哪些键。

**E6 日志 + `scripts/ops/probe_qoder_usage.py` 探针的结论（本轮最终定位，修正上文「未回填」表述）**：

上游 usage **明确返回** `input_tokens=0` / `output_tokens=0` —— **不是缺键，也不是 None**，而是**服务端回填的 0 值**。
探针原始输出（3 份 usage 全部一致）：

```
input_tokens        = 0        ← 服务端回填的 0（键存在）
output_tokens       = 0        ← 同上
credits             = 0.9262815847999999   ← 非零
original_credits    = 0.9262815847999999   ← 非零
context_usage_ratio = 0.24130555555555555   ← 非零
cache_read_input_tokens      = 0
cache_creation_input_tokens  = 0
```

**修正**：上文「上游**未回填** token 字段」表述不准 —— 键**在**，值是**服务端给的 0**。
两者处置不同：
- 「键缺失」→ 要 SDK/协议层适配；
- 「值为 0」→ **上游侧修好即可**，ReviewAgent 侧无需改动。

**对 ReviewAgent 的结论不变**：管道完整，`total_tokens=0` 是**上游语义**（该模型不按 token 计费），
唯一可信计量是 `cost_credits`。E11 的断言正是按这个口径写的（credits 有值即视为「有成本信号」）。
`total_tokens` 改 NULL 语义（待办 ②）仍然可选，但优先级已降到低 —— 因为它不改变任何决策。

---

### 7.1e 第五轮（安全类降噪 + 全面完整验证 2 遍）新发现并修复（N7–N10）

> 详细证据、逐条实锤与两轮对照见 **§16**。本节只做台账索引。

| 编号 | 缺陷 | 实锤证据 | 修复 | 回归 |
|---|---|---|---|---|
| **N7** | **规则键缺失**：模型偶发在 `rationale` 里不写规则前缀 → 落库 `rule_keys` 为空 → 周报「问题类型」归不了类、去重退化、无法分诊 | 真实业务 MR !7（边界开、干净单轮 16 条）L3 图2「规则键引用」15/16，1 条 `rule_keys` 为空 | 提示词侧加硬约束：`improve.md` 的 self-check 新增「`rationale` **首句以规则键开头**（`R-*` / `R-OTHER:<描述>` / `R-OTHER-IMPACT:<描述>` / 仓库规则键原样复制）；**写不出键就说明这条不该报**（不要硬凑假键）」 | `test_suggestion_rationale_must_cite_rule_key`（契约）+ 真实业务复验：MR !8（全新、同内容）**空键 0 条**、L3 **20/20** |
| **N8** | **部署态提示词漂移**：`profiles/<profile>/prompts/improve.md` 是 agent 的 system prompt，实际由 `.qoder/agents/improve.md` 物化供给；`sync_qoder_agents.py` 按 **mtime** 增量写，**漏跑一次就静默不生效** | 实测：改完 `improve.md`（N7）未同步，部署态仍是旧版（309 行 vs profile 406 行），`grep 规则键开头` 在 `.qoder/agents/improve.md` 中 **0 命中** → N7 修好了但真实检视里不生效 | 新增 `scripts/ops/check_agent_drift.py`（按**内容**比对，不看 mtime；缺失/漂移 → 退出码 1）+ 接进 `scripts/restart_local.sh`（sync 之后立刻校验，漂移即**中止启动**）+ `sync_qoder_agents.py` 抽出 `expected_agent_text()` 作为「写盘 / 校验」共用渲染函数 | `tests/test_check_agent_drift.py`（6 例，含「mtime 更新但内容陈旧仍须报出」）+ 部署链路实测（漂移守卫已生效，`exit=0`） |
| **N10** | **E2E 的 Apply 模拟用错范围语义**：`apply_closure` 特性按 DB 的 `target_line_end` 打补丁，而该字段存的是**新代码跨度**（`n_lines = len(improved_code)`）—— 1→N 多行替换时它比**原代码跨度**大 → 补丁**多删**原代码之外的行，把文件改坏 → `auto_detect` 认不出「已落地」→ 特性误报红 | E2E 全量档 `apply_closure` 失败（`state=open adopted_actions=0 path=synthetic-update`）。根因锚点：project 56 的同内容建议报 `L22-23`（existing 2 行 = improved 2 行 → 恰好相等）；project 57 报 `L22-25`（existing 2 行 / improved 4 行 → 差 2 行）→ 多删 2 行 | `_apply_suggestion_to_file` 改为按 **`existing_code` 的实际行数**替换 —— 这正是 GitLab 真实 Apply 的语义（`policies/python.py` 的 `n_replace = len(existing_code.splitlines()) - 1`，建议块旧侧行数）；existing 为空才退回 `target_line_end` 跨度 | `tests/test_e2e_apply_patch_range.py`（5 例，含 1→N 核心回归）+ 变异验证 + 修复后 2 轮完整验证 E2E 全量档 **32/32 / 32/32** |
| **N9** | **并发写 MR 标题导致 telemetry 与 GitLab 漂移**：链式路径持 per-MR 锁，但 note 路径（`/describe`）走 RQ 单命令入口 **不取锁**；cooldown 只挡**入队**不挡**执行** → 排队中的 note 任务与链式任务**重叠跑** → 两个 describe 并发写标题时，「GitLab 标题」与「telemetry 标题」的**最后写入者可能不同** | 第 2 轮完整验证 E2E 全量档 **31/32**（`telemetry:mr_activity` 报红：telemetry 标题 ≠ GitLab 标题）。worker 日志实测：01:52:22 note describe（**无 `chain.lock_acquired`**）+ 01:52:24 链式 describe 并发；01:52:58 写 GitLab(T1)+回写 telemetry(T1)，01:53:00 写 GitLab(T2) → **GitLab=T2 / telemetry=T1** | 单命令入口 `run_describe` / `run_improve` 改为先取**同一把** per-MR 链锁再执行（`_run_locked`）；链式路径继续直接调 `_run_command`（锁不可重入，再取会自锁）；拿不到锁 → **拒绝执行**，不裸跑 | `tests/test_note_command_lock.py`（5 例：执行期持锁 / 超时拒绝执行 / 异常释放 / 链式路径只取一次锁）+ 变异验证（绕锁 → 3 例红）+ 修复后 2 轮完整验证 E2E 全量档 **32/32** |

> **N8 的教训**：`§15` 轮验证的是「规则包（`noise_boundary.md` / `general_rules.md`）进 chunk prompt」，
> 而 `improve.md` 走的是**另一条链路**（agent system prompt → `.qoder/agents/`）。
> 只验证前者不足以证明「提示词改动已生效」—— 静态检查、单测、契约测试**全都不会报**。
>
> **N9 的教训**：`cooldown` 是**入队**闸门，不是**执行**闸门；「同一 MR 的命令串行」这件事，
> 链式路径做到了、note 路径没做到 —— 只有把两个 sink 的写序拉齐才暴露出来。
> 单跑一次全量档未必复现（本轮第 1 轮 32/32、第 2 轮才 31/32），
> 这正是「完整验证连跑 2 遍」的价值。

---

## 8. 缺口台账（未覆盖 / 待补）

> 全量清点 434 个功能点，其中 174 个在清点时**无单元测试**。下列为按风险排序的**重点缺口**（低风险内部辅助函数不计）。

| 优先级 | 缺口 | 影响 | 计划 |
|---|---|---|---|
| ~~高~~ | ~~**Apply 闭环未真正闭环**：E2E `ui_apply` 只断言 webhook 响应形态，不断言代码真被改、suggestion 变 applied~~ | — | ✅ **本轮已修（E1）**：`apply_closure` flow 真改文件 → commit 到源分支 → 等 state 变 applied → 断言该 note 有 adopted action |
| ~~高~~ | ~~**去重 / cohort supersede 无 E2E**~~ | — | ✅ 去重 flow 已有；**跨轮次 supersede 本轮在单元/集成层补齐（E12，12 例）** —— 之所以放单元层：E2E 里「LLM 第 2 轮又报同一问题」取决于 LLM 随机输出，无法确定性构造；真实风险在状态机与归并计数 |
| ~~高~~ | ~~**reconciler 无 E2E**~~ | — | ✅ **本轮已修（E3）**：`reconcile_resolved` flow —— GitLab API resolve discussion → 跑 `sync_resolved_suggestions.py` → 断言 DB `state=resolved` |
| ~~中~~ | ~~**命令派发只覆盖一半**：MR 事件的 `close`/`merge`/`draft` 分支仍无 E2E~~ | — | ✅ **本轮已修（E10，13 例）**：这三条分支的正确语义是「**不**触发检视」，E2E 无法稳定构造「不该发生的事」，故在单元层覆盖 `tests/test_webhook_mr_action_branches.py`（含 4 个变异体验证） |
| ~~中~~ | ~~**周报其余 live 字段无断言**~~ | — | ✅ **本轮已修（E13，13 例）**：`flows._audit_weekly_live_fields` 断言 artifact 的标题/emoji/dashboard_url + markdown 首行标题 + dashboard markdown 链接 + 分片数 |
| 中 | **catalog 的 55 条 live 字段无「必须有全量档断言」绑定** | 新增 live 字段后全量档不写断言，目录仍全绿 —— 整个 witness 体系的防漏测承诺点在 live 类上是空的 | **本轮已补**：引入 `LIVE_CHECKS`（15 条已绑定专属断言）+ `LIVE_GAPS`（40 条显式记账，含原因）+ 守门测试；台账只应随新断言落地而缩小 |
| ~~中~~ | ~~**token 采集只做恒等式**（`total == prompt + completion`），未断言 `> 0`、未覆盖 `cost_credits`~~ | — | ✅ **本轮已修（E11，10 例）**：见 §7.1c。恒等式恒成立 → 改断言「provider 回报了模型 ⇒ 必须有成本信号」+「无模型 ⇒ 必须无信号」+「不得为负」+「整体至少一个 success run 带信号」 |
| 中 | **`suggestion_actions.action='adopted'` 语义过宽**：校验失败也记 `adopted` | 统计口径**已正确**（`_is_effective_action` 排除 `same-head`/`content-unavailable`/`target-unchanged`/`already-*`），但**原始 SQL** 与时间线端点仍会看到「尝试」 | **本轮已缓解**：时间线端点新增 `effective` 列（Python 侧复用 `store.is_effective_action` 单一事实源，不在 SQL 复制规则）；原始列仍保留「尝试」语义 |
| ~~低~~ | ~~**`review_runs.model` 字段两种口径混用**~~ | — | ✅ **本轮已修（E8，8 例）**：拆成 `model`（provider 实际回报，未回报为 NULL）+ `model_configured`（请求时配置 id）；skipped 运行的 `model` 置空 |
| 低 | **`/review` 命令 README 标注未实现** | 文档与实现不一致 | 实现或从 README 移除 —— **属功能决策，本轮不擅动** |
| 低 | **`weekly_target_project_id=34` 在 GitLab 返回 404**（实际存在 29、31） | 定时周报目标项目无效 | **需业务确认**应指向哪个项目 |
| 低 | **reconciler 无常驻进程**：本机 launchctl 只有 weekly | 周期对账只能手工跑 | 加载 `scripts/com.jarvs.reviewagent.reconciler.plist` |
| 低 | 大量内部辅助函数无单测（174 项中的多数） | 回归防护弱 | 按 §6 护栏逐步补 |

### 8.1 缺口分布（按模块）

全量清点 **434 个功能点**，其中 **174 个**在清点时无单元测试。分布：

| 模块 | 无单测功能点 | 风险判断 |
|---|---|---|
| `reviewagent/webhook/` | 46 | **最高** —— 系统入口。已补命令解析/鉴权/fail-open 契约测试（22 例），仍有 MR 事件各 action 分支、Push 各分支未覆盖 |
| `reviewagent/commands/` | 42 | **高** —— 业务主干。核心路径已由单测 + E2E + 真实业务三重覆盖，剩余多为异常分支 |
| `reviewagent/reporting/` | 27 | 中 —— 周报链路已实跑验证，剩余为 collector 内部与渲染细节 |
| `reviewagent/telemetry/store.py` | 18 | 中 —— 写入路径大多已覆盖，剩余为 `list_*` 查询与少量辅助函数 |
| `reviewagent/api/router.py` | 14 | **已补** —— 本轮新增 40 例 HTTP 契约测试，20 个端点全覆盖（契约表 18 + 单独用例 2） |
| `reviewagent/workers/` | 13 | 中 —— 队列与任务封装 |
| `reviewagent/scenario/` | 13 | 低 —— profile/规则装配的内部校验分支 |
| `reviewagent/reconciler/` | 1 | 低 —— 但功能重要性高（见上表「reconciler 无 E2E」） |

**说明**：该清点为 2026-11-11 的快照；本轮新增的 78 例已覆盖其中 API 层全部 14 项 + webhook 前门 10 项。
`reviewagent/review/` 模块的清点在采集时被截断，其核心缺口（applicability 三态）已由本轮
`tests/test_suggestion_applicability.py`（11 例）+ `tests/test_e2e_telemetry_audit.py`（7 例）覆盖。

---

## 9. 剩余风险

| 风险 | 说明 | 缓解 |
|---|---|---|
| token 用量不可用 | 上游 provider 不回填，成本只能靠 `cost_credits` 估算 | §7.3 待办 |
| GitLab 上遗留 E2E 临时项目 `reviewagent-e2e-20261108094607`（id=3） | 2026-11-08 某次中断的 E2E 运行未清理 | 框架正常路径会自动删除（最近两次均已清干净）；id=3 属 **root** 名下，bot 无权删 —— 需人工在 UI 清理 |
| 周报目标项目错配 | `weekly_target_project_id=34` 无效 | 待业务确认 |
| 周报外发语义变更 | D4 修复后真发需显式 `--push`；若某部署依赖「仅靠配置外发」需改回 | launchd 路径已透传 `--push`，行为不变 |
| E2E 全量档需写权限 token | 建临时项目要求 `api` scope | 当前 `reviewagent-e2e-bot` 具备 |
| E2E 真发能力 | 显式 opt-in 后仍可真发钉钉 | 默认关闭 + 单测守卫 |
| GitLab 19.5 UI 行为 | 锚点行内容被改后该评论从 Changes diff 视图移除；「行被删除」的评论在 diff 视图完全不渲染 | GitLab 行为，非 ReviewAgent 缺陷；只能经 API/Overview 访问 |
| E2E 夹具与场景包耦合 | 夹具内容与 `review.yaml` 的 `priority_paths` 绑定，改场景包需同步改夹具与 `tests/test_e2e_real_config.py` | 已在本文档 §3.4 标注 |
| 既有测试环境依赖 | `tests/test_qodercli_subprocess_fallback.py` 10 例需本机 `qodercli.js`，当前缺失 | 长期失败项，需 CI 补装依赖或标记 skip |
| **跨 head 的陈旧建议无法自动判定失效** | MR !3 实测 1/15 条建议的**目标代码在当前 head 已被重构移除**，GitLab 仍把它锚在当前是空行的第 6 行；按设计决策 `0b96f7b`「新 head 不隐藏未解决建议」，系统**刻意不**自动 supersede → 该建议长期计入「待处理」 | 缓解：用户 resolve 线程或回复 `/adopt`；`supersede_stale_open_suggestions` 已实现但按该决策不调用（有回归测试守着）；若后续要改，需同时改「图1 不隐藏未解决建议」的产品口径（见 §12.5 O2） |
| **GitLab 临时项目堆积** | 实测 **34 个 `deletion_scheduled`**（GitLab 19.5 异步删除未真正完成）+ 4 个正常态 E2E 残留（id=16/21/41/47）+ root 名下历史遗留（id=3） | 框架侧已发起删除；**本轮新增 `scripts/ops/cleanup_e2e_projects.py`**（默认 dry-run、保护名单含 p31 真实业务项目）可一键清理当前用户名下的残留；`deletion_scheduled` 的需换 GitLab 版本或人工在 UI 清 |
| **钉钉机器人 token 建议轮换** | 2026-11-11 复验过程中一条 `grep` 命令未脱敏，把 `instance.env` 里的钉钉 `access_token` 打进了会话工具输出 | 该 token 仍只在 `config/instance.env`（gitignore + 600 权限），**未入库、未进代码/文档**；但因已在会话中出现，建议轮换钉钉机器人 token |

---

## 10. 复现指南

```bash
cd /Users/jarvs/ReviewAgent
set -a && . config/instance.env && set +a

# 单元测试（1568 passed / 10 failed(qodercli.js 缺失) / 2 skipped；带不带 instance.env 数字一致）
.venv/bin/python -m pytest -q

# E2E 快速档（21/21，无需外部依赖）
.venv/bin/python scripts/e2e/run_e2e.py --tier fast --logdir logs/e2e

# E2E 全量档（临时 GitLab 项目 + 真实 LLM；约 5-15 分钟）
.venv/bin/python scripts/e2e/run_e2e.py --tier full --round 1 --logdir logs/e2e
# 只跑单个用例
.venv/bin/python scripts/e2e/run_e2e.py --tier full --only applicability,cooldown --round 1

# 真实业务核对（p31 / MR !3 = 重构前配置产出；MR !4 = 重构后新配置产出）
curl -s -H "PRIVATE-TOKEN: $GITLAB_PERSONAL_ACCESS_TOKEN" \
  "http://127.0.0.1:8929/api/v4/projects/31/merge_requests/3/discussions?per_page=100"
sqlite3 -header -column data/telemetry.db \
  "select id,note_id,target_line,severity,state,applicability,adoption_source from suggestions where project_id=31 and mr_iid=3;"
```

> L3 逐格核对脚本（§12.3 的 20 项检查，两个样本通用）：
> `VERIFY_PID`/`VERIFY_MR` 通过位置参数传入 —— `.venv/bin/python /tmp/l3_verify_p31.py 31 3` / `31 4`。
> 该脚本只读，不改仓库与线上状态；结论与原始数据落在 `/tmp/l3-verify-p{PID}-mr{MR}.json`。

### 关键锚点

| 对象 | 锚点 |
|---|---|
| MR !3 | `http://127.0.0.1:8929/review-bot-v2/ssd-real-biz-full/-/merge_requests/3`（重构前配置产出） |
| MR !4 | `.../merge_requests/4`（**重构后新配置**产出，23 条；head `91d23b95`） |
| 图1（检视汇总） | MR !3 → note `1109`；MR !4 → note `1228` |
| 改进总览 V1/V2/V3/V4/V5 | note `1110` / `1124` / `1131` / `1137` / `1141` |
| 日志 | `logs/server-3000.log`、`logs/worker-w1~w3.log`、`logs/worker-weekly.log`（grep 须带 `project=31 mr=3`） |
| 周报产物 | `data/weekly_reports/weekly-2026-W{44,45,46}.json`（+ `.md`） |
| E2E 产物 | `logs/e2e/report-<stamp>.json`、`logs/e2e/run-<slug>-r<n>-<stamp>.log` |

---

## 11. 独立复验记录（2026-11-11 19:04）

在完成全部修复与文档后，**从零重跑一遍**验证（干净重启服务栈 → 三层测试 → 真实业务逐项核对 →
运行态验证每个修复），不复用上一次结论。

| 环节 | 方法 | 结果 |
|---|---|---|
| 服务栈 | `scripts/restart_local.sh` 干净重启 | web/redis/worker×3/weekly 全部在线；`/readyz` 200；Redis PONG；launchd weekly 已加载 |
| **L1 回归（含基线对照）** | `pytest -q`；再 `git stash` 回 HEAD 跑同一批对比 | 现在 **1528 passed / 10 failed / 2 skipped**；HEAD 基线 **1436 passed / 10 failed / 2 skipped** —— 失败集合完全相同（`qodercli.js` 缺失），**+92 且零回归** |
| **L2 快速档** | `run_e2e.py --tier fast` | **21/21** witnesses（`report-20261111T110854Z.json`） |
| **L2 全量档** | `run_e2e.py --tier full --round 1`（新建临时项目 34） | **30/30**（`report-20261111T110859Z.json`）；本次 `applicability` 分布含 `text_only:1`，文字说明路径真实走到 |
| **L3 图1** | 脚本**自动解析**图1 表格并与 telemetry 逐格比对 | 6/6 项零偏差（总数/3 个严重度行/总计行/采纳率） |
| **L3 图2** | 按 **inline 锚点**（`position`）筛出 15 条行内讨论，与 telemetry 双向比对 | 9/9 项通过：15↔15 严格 1:1、无缺失无多余、resolve 状态与 state 零不一致、三态与正文形态全一致 |
| **D5 运行态** | 用真实 MR !3 的 15 条数据跑修复后的 E2E 审计 | 假阳性 **0**（修复前为 2/15） |
| **D6 运行态** | 对运行中的服务发非法时间参数 | `since=not-a-date` / `2026-13-45` / `until=garbage` 全部 **400**；合法日期 200 |
| **D7 运行态** | 校验 91 条证据引用 | **0 条失效**；关键条目已改为符号锚点（会被真校验） |
| **D8 运行态** | 手动跑周报 + 门控真值表 + 调度路径检查 | 无 `--push` → `delivery: dry_run`；`_should_send_notice(push=False)=False`、`(push=True)=True`；launchd 仍透传 `--push`（调度行为不变） |
| **D9 运行态** | 真实触发 `/describe` 到 MR !3，比对 DB 与 GitLab 标题 | 修复前两者**不一致**（实时数据复现）；修复后 **完全一致** |
| 文档自检 | 脚本抽取文档中的数字声明逐条比对实际 | 15 项全一致（据此修正了「端点数 19→20」与「基线」两处） |
| 安全 | 权限/gitignore/泄漏扫描/PAT 状态 | `instance.env` 600 + gitignore；代码/文档/未跟踪文件**无真实凭据**（仅测试假密钥）；临时 root PAT 已撤销；当前仅 `reviewagent-e2e-bot` 一个 PAT |

### 复验中发现并已修正的文档问题

| 问题 | 修正 |
|---|---|
| 文档写「19 个 telemetry 端点」，实际注册 **20** 个 | 已改为 20（契约表 18 + 单独用例 2，登记守卫覆盖全部） |
| 文档基线只写 1450，未说明 HEAD 基线 | 已补：HEAD 基线 1436（实测 stash 对照），本轮净增 +92 |
| 「采纳率」口径未说明，易被误读为「已采纳/已处理」 | 已在 §4.3 加口径说明（= 已采纳/总数，含待处理；图1 与周报一致） |
| 复验中一条 grep 未脱敏，钉钉 `access_token` 进了会话输出 | 已记入 §9 剩余风险，建议轮换该 token（该值未入库、未进代码/文档） |

---

## 12. 用户重构后复验记录（2026-11-12）

> 触发原因：工作区出现一轮**大规模未提交重构**（103 files changed, +2317/−872），涉及 prompts 块化整合、
> rules 目录迁移、`test_paths` 扩展、`min_severity` 调整 —— 属**行为变更**，必须重新走一遍三层验证。

### 12.1 重构内容核实（逐项确认，行为符合预期）

| 重构项 | 重构后状态 | 核实结论 |
|---|---|---|
| prompts 块化 | 14 个碎片文件 → `prompts/blocks/{review,source,dynamic}.md` 三块 | ✅ 实时渲染无未替换占位符；p31 真实仓库规则上下文 44 005 字符 / 17 个规则文件 / 15 个 `SSD-RULE-*` 键，含 `<instruction_files>` 与 `<rule_index>` |
| rules 目录迁移 | `dynamic_rules.yaml` → `rules/`；`rule_actions.yaml` 由 `reporting/` → `rules/`；删除 `rule_keys`/`capabilities`/`security`/`reporting` 段 | ✅ 场景包指针全部指向新位置，无悬空引用 |
| `profile.yaml: test_paths` | `[tests/, test_*.py]` → `[testcase/, testlib/, test_components/, test_*.py, conftest.py]` | ✅ MR !3 的 4 个变更文件在新旧口径下测试占比 25% → 100%；因配置显式 `review_mode: test`，最终结论一致 |
| `review.yaml: min_severity` | `medium` → `low` | ✅ **实质行为变更并已实测生效**：MR !4 产出 1 条 `low` 级建议（`test_gc_status_e2e.py:50`，已被 `/dismiss`）并在图1 LOW 行可见 |
| `review.yaml: file_filter.skip_paths` | `tests/ tmp_scripts/ docs/ tools/`（目录黑名单） | ✅ `conftest.py` **不再被跳过**（`priority_paths` 亦含 `/conftest.py`）—— 与旧「文件名黑名单含 conftest.py」策略冲突，见 **D10** |
| `review.yaml: scoring.review_mode` | `test` | ✅ 重构前该值**只算不用**（`self._review_mode` 赋值未消费）；重构后**新增真实消费点**：`review_mode == "test" and cfg.improve_priority_weight_test_feature > 0`（test 模式才计测试特征密度权重，business 模式不读文件内容），且 `_is_test_file` 改用策略 `classify_file` |
| `rule_key_prefix` | 显式值 → 回落 schema 默认 `auto` | ✅ 与重构前**等值**，无行为变化 |
| 其余 | `test_sample_paths: [testcase/]`、`max_suggestions: 20`、`full_files: 5` | ✅ 与 E2E `config_real:*` 断言一致 |
| 场景包指纹 | `a1a53cd6cd5444d2` | ✅ 服务与磁盘一致（`/readyz` 已确认） |

**D5–D9 五个前序修复在重构后全部完整**（逐条 grep 确认代码路径未被重构覆盖）。

### 12.2 本轮新发现并修复的缺陷（D10–D14）

| 编号 | 缺陷 | 影响 | 修复 | 回归 |
|---|---|---|---|---|
| **D10** | E2E 全量档断言 `config:conftest_not_reviewed` 把「conftest 一律不检视」**写死** | 场景包策略改为「conftest 不跳过 —— fixture 是测试业务的真实契约面」后，正确行为被判失败：全量档 **29/30**（唯一失败项） | 断言改为**策略驱动**：`config_audit._paths_skipped()` 按 `cfg.improve_skip_test_paths` 判定「命中 skip 却仍有建议」才算违规（`conftest_skip_policy_respected`）；**改断言不改业务代码** | `tests/test_verify_e2e_smoke.py` +2 例（策略跳过→违规 / 未跳过→放行）；变异验证：`_paths_skipped` 恒 False → 2 例红；退回旧语义 → 1 例红 |
| **D11** | `auto_detect_applied` 中「删除型」判定排在 `resolved is not True` 的**提前 return 之后** | delete 型建议 `improved_code` 为空 → `exact_match` 恒 False → 用户点 GitLab **Apply** 删掉代码（但没手动 resolve 线程）后，永远无法被自动标记为已采纳 → 建议长期挂在「待处理」，图1 与采纳率分母持续虚高 | 把 delete_range 判定**提到 resolved 分支之前**求值 | `tests/test_auto_detect_applied.py::test_delete_kind_applied_detected_when_discussion_unresolved`；变异：退回提前 return → 该例红 |
| **D12** | 「删除型采纳」的证据是「原文案**不在**当前文件里」，而非「目标区段**真被删除**」 | 用户只是把那段代码**改写**（`return 1` → `return 2`）时原文案同样消失 → 被判为「已删除 → 已采纳」→ **假阳性采纳**，采纳率虚高 + 状态失真 | 新增 `_deleted_region_confirmed()`：用 `difflib.SequenceMatcher` 的**行级 delete 操作码**判定「整段被删」；`'replace'`（改写）不算删除；目标行越界时退化为「原文案消失 **且** 当前文件行数变少」 | `tests/test_auto_detect_applied.py::test_delete_kind_not_applied_when_block_still_present` + `tests/test_auto_detect_late_apply.py::test_deletion_kind_modified_in_place_does_not_flip`；变异：退回旧判定 → 2 例红 |
| **D13** | `text_only` 建议被一并纳入「删除语义」，可用「原代码消失」当采纳证据 | text_only 的语义是「文字说明怎么改」，**没有可机检的落地形态**；原代码消失 ≠ 用户按建议改完（可能只是重构掉了）→ 误判 applied | delete_range 判定**只对删除语义生效**：`applicability == "delete"`，或历史数据（无该列）且 `improved_code` 空 + `existing_code` 非空；`text_only` 一律排除 | `tests/test_auto_detect_applied.py::test_text_only_kind_not_treated_as_delete_range`；变异：去掉 applicability 门槛 → 该例红 |
| **D16** | 「收缩建议」(general 分支) 刻意不给 Apply 按钮，但落库记 `replace`、正文复用含 Apply 引导的页脚 | ① 落库形态与正文形态矛盾 → E2E 审计报红（`replace 正文缺 suggestion 块` + `improved_code diverges from posted note`）—— **审计报的是真问题**，不是假阳性；② 正文提示用户去点**不存在的**「应用建议」按钮（D1 的反向问题）；③ 落库 `improved_code` 使 auto_detect 可能把收缩建议误当可 Apply 目标 | general 分支与 `text_only` 同构：正文用 `callbacks.help_footer_text_only`（无 Apply 引导）、落库 `applicability=TEXT_ONLY`、不落 `improved_code`；`improve.py` 传入 `HELP_TEXT_FOOTER_TEXT_ONLY` | `tests/test_general_shrink_contract.py`（新建，4 例：正文无 Apply 引导 / 落库 text_only / 不落 improved_code / 建议不丢）；变异：改回 applicability → 1 例红；改回 improved → 1 例红；改回普通页脚 → 1 例红 |

| **D15** | 单元测试依赖部署环境变量；`conftest.py` 只对 baseline 变量用 `setdefault`，**不清**外部带进来的部署变量 | 带着 `config/instance.env` 跑单测 → **41 failed**（干净环境 10 failed）：`QODER_SDK_EDITION` 使 `Config.from_env()` 在 import 期抛 ValueError → `tests/test_qoder_sdk_provider_errors.py` **整个文件 collection error**（61 例）；`REVIEW_PROFILE` 使隐式场景包用例失效。**门禁数字随 shell 状态漂移** | `tests/conftest.py` 新增 `_DEPLOY_ENV_KEYS`（19 个部署变量），**import 期** `os.environ.pop` + 每个测试前 `monkeypatch.delenv`；清理清单不含 baseline 变量（`QODERCLI_MODEL` 等仍在 baseline 里） | `tests/test_conftest_env_isolation.py`（新建，4 例）：清单覆盖 instance.env 全部部署键 / 运行期确已清除 / import 期确实清过（守 `os.environ.pop` 那一层）/ baseline 未被误清；变异：去掉 import 期 pop → 该例红 |

| **D14** | `cohort_key` 只用「文件 + 标题」，**不含代码内容** | 同文件里**不同位置**、标题文字相同的不同问题被算作同一个 cohort → `list_latest_by_cohort` 只留最新一条 → **图1/周报总建议数少算**、采纳率分母偏小（虚高），且被并掉的几条在 DB 里仍是 `open`、GitLab 上仍可见 → 「图1 待处理 9 条 / GitLab 12 条」自相矛盾。E2E 实测：3 条同标题建议（L7/L12/L17，同一文件）→ 图1 显示总建议数 10、实际 12 | 新增 `_cohort_key(file_path, header, existing_code)`：粒度 = 文件 + 归一化标题 + **代码内容指纹**（仍不用行号 —— 行号会随用户改动漂移，内容才是「同一问题」的稳定特征）；内容变了就不归并 → 图1 多算一条（与 GitLab 可见数一致，方向安全） | `tests/test_cohort_key_granularity.py`（新建，4 例：粒度 3 例 + publish 端到端 1 例）；变异：退回 `(file+header)` → 2 例红 |

配套改动：`store.list_open_suggestions()` / `store.list_resolved_suggestions()` 的 SELECT 补出 `applicability` 列（原先取不到，三态信息在自动采纳路径上完全不可用）。

### 12.3 L3 真实业务复验（两个样本）

**样本 A：p31 MR !3**（重构前配置产出，15 条建议，head `82de89a`）

| 检查项 | 结果 |
|---|---|
| 图1 逐格（总数 / 采纳率 / HIGH / MEDIUM / LOW / 总计） | ✅ **6/6 零偏差**（15 条，采纳率 13.3% = 2/15，口径含待处理） |
| 图2 discussion 级 1:1 | ✅ telemetry 15 ↔ GitLab bot discussion 15，双向无缺失无多余；被 cohort 归并 0 条 |
| resolve 状态 × telemetry state | ✅ 零不一致（`/adopt`→applied+resolved、`/dismiss`→dismissed+resolved 均自洽） |
| applicability 三态 | ✅ 取值合法（replace 12 / delete 1 / text_only 2），且与正文形态**逐条一致**（text_only 无 suggestion 块、delete 空块、replace 块与 `improved_code` 一致） |
| 规则键引用 | ✅ 15/15 落库且见于正文（`SSD-RULE-*` 与内置 `R-*` 两类） |
| 变更集归属 / 文件可达 | ✅ 全部落在 MR 变更的 4 个文件内 |
| 旧 head 建议的锚点是否仍成立 | ⚠️ 14 条建议的 `head_sha` 落后于当前 head；其中 **1 条**（`c9487c54`）的**目标代码段在当前 head 已被重构移除 → 锚点失效**（见 O2）；其余 13 条目标段仍在、位移均在 ±8 行容差内 |
| **L3 合计** | **19/20**（唯一未过项即上述锚点失效，属设计决策下的已知限制，非缺陷） |

**样本 B：p31 MR !4**（**重构后新配置**产出，23 条建议，head `91d23b95`）

| 检查项 | 结果 |
|---|---|
| 图1 逐格 | ✅ **6/6 零偏差**（23 条，采纳率 8.7% = 2/23） |
| **`min_severity: low` 是否真生效** | ✅ **是** —— LOW 行 1 条（dismissed），旧配置（medium）下不会出现 |
| applicability 三态 | ✅ text_only 3 / replace 17 / delete 3，取值合法且与正文形态全一致 |
| 规则键引用 | ✅ 23/23 |
| 被 cohort 归并 | 7 条 —— **GitLab 上仍可见但不进图1**，汇总里**已显式披露**「♻️ 同问题被多轮重复发布，7 条已被合并到最新版本」（见 O4） |
| 旧 head 建议的锚点是否仍成立 | ✅ 22 条建议的 `head_sha` 落后于当前 head，但**目标代码段全部仍可定位**，位移均在 ±8 行容差内 |
| **L3 合计** | **20/20** |

### 12.4 本轮 L1 / L2 结果

| 环节 | 结果 |
|---|---|
| **L1 回归** | **1568 passed / 10 failed / 2 skipped**；10 个失败仍是同一批 `qodercli.js` 缺失环境问题；HEAD 基线 **1436 / 10 / 2** → **+132 且零回归**；**带/不带 `config/instance.env` 数字完全一致**（D15 修复后） |
| **L2 快速档** | **21/21** |
| **L2 全量档**（D10 修复后） | **30/30**（`logs/e2e/report-20261111T163735Z.json`） |
| **L2 全量档**（D11–D14 修复后） | 见 §12.6 |

### 12.5 观测项（**非缺陷**，但需知情）

| 编号 | 观测 | 说明与依据 |
|---|---|---|
| **O1** | 同一建议的 `head_sha` 与行号在 telemetry / GitLab 两侧「对不上」 | MR !3 经 3 次 push（`7e7bd8ce` / `b93af256` / `82de89a`），14/15 条建议的 `head_sha` 落后于当前 head。**GitLab 会在新 commit 后重锚定 note 的 position**（例：telemetry `L33` → GitLab `L31`，位移 −2），telemetry 记的是「发布时 head 的行号」—— **两个坐标系都正确**，不是数据错误。`/adopt` 与 auto-detect 均用 LCS 对齐 / 行级 diff 容忍位移（实测位移在 ±8 行容差内） |
| **O2** | 1 条建议的**目标代码在当前 head 已被彻底重构移除** | `c9487c54`（text_only，提示 `GC_POLL_TIMEOUT = 300` 硬编码超时）—— 该常量在后续 push 中已被改成函数入参，GitLab 仍把它锚在第 6 行（当前是空行）。按设计决策（`0b96f7b`「新 head 不隐藏未解决建议」）系统**不自动 supersede**，该建议会长期计入「待处理」。缓解：用户 resolve 线程或回复 `/adopt`；`supersede_stale_open_suggestions` 已实现但按该决策**刻意不调用**（有回归测试守着） |
| **O3** | 同一文件同一行可出现多条 open 建议 | dedup 是**规则感知**的：不同 `rule_keys` 即使 `(file, line±2)` 重叠也不互斥（防误杀）→ 属设计行为，非重复发布 |
| **O4** | GitLab 上可见的建议数 > 图1 总数 | 同一问题多轮发布时旧条被 **cohort 归并**（进 DB 历史、留在 GitLab、不进图1），汇总显式披露「♻️ N 条已被合并到最新版本」；MR !4 实测 N=7 |
| **O5** | GitLab 上堆积 `deletion_scheduled` 的 E2E 临时项目 | GitLab 19.5 的异步项目删除未真正完成（框架侧已发起删除）；本轮实测 **38 个 `deletion_scheduled`** + root 名下的 id=3。**本轮新增 `scripts/ops/cleanup_e2e_projects.py`** 可清理当前用户名下的正常态残留（默认 dry-run，保护名单含 p31 真实业务项目），已实删 3 个；`deletion_scheduled` 的需换 GitLab 版本或人工在 UI 清 |

### 12.6 D11–D14 修复后的 E2E 全量档

| 环节 | 结果 |
|---|---|
| **L2 快速档**（D14 修复后） | **21/21**（`logs/e2e/report-20261111T170019Z.json`） |
| **L2 全量档**（D11–D14 修复 + 重启服务栈） | **30/30**（`logs/e2e/report-20261111T170023Z.json`）—— `ui_apply` / `adopt` / `dismiss` 三条交互闭环未受影响；`config:conftest_skip_policy_respected` 实测到 `conftest 建议=['conftest.py']`（conftest 确实参与检视，且未被误判违规） |
| **D14 运行时验证** | 修复前：临时项目 37 的 3 条同标题建议（L7/L12/L17）被并成一个 cohort → 图1「总建议数 10」而 GitLab/DB 实为 **12**；修复后 `dedup` 断言通过，图1 与实际条数一致 |

### 12.7 零回归基线对照（git stash 实测）

| 状态 | 结果 |
|---|---|
| HEAD 基线（`313680b`，stash 后跑同一批） | **1436 passed / 10 failed / 2 skipped** |
| 当前工作区（含用户重构 + 本轮全部修复与新增测试） | **1568 passed / 10 failed / 2 skipped** |
| 差异 | **净增 +132，失败集合完全相同**（10 例均为本机缺 `qodercli.js` 的环境问题），**零回归** |

### 12.8 本轮交付物清单

| 类别 | 文件 | 说明 |
|---|---|---|
| 文档 | `docs/VERIFICATION.md` | 新增 §12（重构后复验记录，含 D10–D14 与 O1–O5 观测项） |
| 生产代码 | `scripts/e2e/config_audit.py` | D10：conftest 断言改为策略驱动（`_paths_skipped` + `conftest_skip_policy_respected`） |
| 生产代码 | `scripts/e2e/catalog.py` | D10：断言名同步（`E2E_ASSERTION_SUFFIXES["config:"]`） |
| 生产代码 | `reviewagent/commands/suggestion_actions.py` | D11/D12/D13：`_deleted_region_confirmed()` + delete_range 提前求值 + applicability 门槛；新增 `SequenceMatcher` 行级 diff 判定 |
| 生产代码 | `reviewagent/telemetry/store.py` | D11–D13：`list_open_suggestions` / `list_resolved_suggestions` 补出 `applicability` 列 |
| 生产代码 | `reviewagent/review/publishing.py` | D14：新增 `_cohort_key(file, header, existing_code)`，cohort 粒度加入代码内容指纹 |
| 测试基建 | `tests/conftest.py` | D15：`_DEPLOY_ENV_KEYS` 清理清单（import 期 + 每测试前） |
| 生产代码 | `reviewagent/commands/improve.py` | D16：传入 `help_footer_text_only`（无 Apply 引导版页脚） |
| 单元测试 | `tests/test_cohort_key_granularity.py`（新建，4 例） | D14 |
| 单元测试 | `tests/test_conftest_env_isolation.py`（新建，4 例） | D15 |
| 单元测试 | `tests/test_general_shrink_contract.py`（新建，4 例） | D16 |
| 单元测试 | `tests/test_auto_detect_applied.py`（+3 例） | D11（正/负例）、D13 |
| 单元测试 | `tests/test_auto_detect_late_apply.py`（+1 例，并修正 2 处与自身文档意图矛盾的 mock） | D12 负例 |
| 单元测试 | `tests/test_verify_e2e_smoke.py`（+2 例） | D10 |


---

## 13. 多轮稳定性验证（同一份代码连跑 3 遍）—— **第二轮修复后的记录**

> **本节是历史记录**：对应「D10–D16 修复后」的那一轮验证（L1 1568、全量档 30/30）。
> **最新一轮**（第三轮：补齐 E8–E13 等剩余项后连跑 3 遍）见 **§14**。
> 本节数字保留为历史对照，不随新基线改写。

> 目的: 证明结论**可复现**、无偶发。每轮都是**完整三层验证**：L1 回归 + L2 E2E 快速档/全量档 + L3 真实业务复验 + 文档数字自检。
> 轮次脚本：`/tmp/verify_round.py <n>`（产出 `/tmp/verify-round-<n>.json`）；E2E 每轮用独立临时 GitLab 项目，跑完自动清理；L3 为**只读**复验，不触发新检视。

### 13.1 三轮逐项对照

| 指标 | 第 1 轮 | 第 2 轮 | 第 3 轮 | 一致性 |
|---|---|---|---|---|
| 服务健康 / 指纹 | ✅ a1a53cd6cd5444d2 | ✅ a1a53cd6cd5444d2 | ✅ a1a53cd6cd5444d2 | ✅ 一致 |
| L1 passed | 1568 | 1568 | 1568 | ✅ 一致 |
| L1 failed | 10 | 10 | 10 | ✅ 一致 |
| L1 skipped | 2 | 2 | 2 | ✅ 一致 |
| L1 失败集合 | test_qodercli_subprocess_fallback.py | test_qodercli_subprocess_fallback.py | test_qodercli_subprocess_fallback.py | ✅ 一致 |
| L2 快速档 | 21/21 | 21/21 | 21/21 | ✅ 一致 |
| L2 全量档 | 30/30 | 30/30 | 30/30 | ✅ 一致 |
| L2 全量档失败项 | 无 | 无 | 无 | ✅ 一致 |
| L3 MR !3 | 19/20 | 19/20 | 19/20 | ✅ 一致 |
| L3 MR !3 未过项 | 图2-旧head建议锚点在当前head仍可定位 | 图2-旧head建议锚点在当前head仍可定位 | 图2-旧head建议锚点在当前head仍可定位 | ✅ 一致 |
| L3 MR !4 | 20/20 | 20/20 | 20/20 | ✅ 一致 |
| L3 MR !4 未过项 | 无 | 无 | 无 | ✅ 一致 |
| MR !3 applicability | {'replace': 12, 'delete': 1, 'text_only': 2} | {'replace': 12, 'delete': 1, 'text_only': 2} | {'replace': 12, 'delete': 1, 'text_only': 2} | ✅ 一致 |
| MR !4 applicability | {'text_only': 3, 'replace': 17, 'delete': 3} | {'text_only': 3, 'replace': 17, 'delete': 3} | {'text_only': 3, 'replace': 17, 'delete': 3} | ✅ 一致 |
| MR !4 严重度分布 | {'high': 19, 'medium': 3, 'low': 1} | {'high': 19, 'medium': 3, 'low': 1} | {'high': 19, 'medium': 3, 'low': 1} | ✅ 一致 |
| 文档数字自检 | ✅ | ✅ | ✅ | ✅ 一致 |
| 全量档报告 | report-20261111T173238Z.json | report-20261111T173924Z.json | report-20261111T174620Z.json | ⚠️ 有差异 |
| 单轮耗时(s) | L1 23.7 / fast 0.4 / full 380.5 | L1 22.6 / fast 0.4 / full 389.1 | L1 23.8 / fast 0.4 / full 351.3 | ⚠️ 有差异 |

### 13.2 结论

**三轮核心指标完全一致**（L1 通过/失败/跳过数与失败集合、E2E 快速档与全量档通过数、L3 两个样本的通过数、文档自检结果全部相同）→ 结论**可复现，无偶发**。

### 13.3 三轮中暴露并修复的缺陷

| 轮次 | 现象 | 判定 | 处理 |
|---|---|---|---|
| 修复 D15 前（第 0 轮试跑） | L1 突然变成 **1529 passed / 41 failed** —— 多出 31 例全是环境变量污染（`QODER_SDK_EDITION` 让 `Config.from_env()` 在 import 期抛 ValueError → 整个测试文件 collection error；`REVIEW_PROFILE` 让隐式场景包用例失效） | **真缺陷 D15**：单测数字取决于开发者的 shell 状态 → 门禁不可信 | `tests/conftest.py` 增加 `_DEPLOY_ENV_KEYS`（import 期 + 每测试前清理）+ 新增 4 例守门测试；修复后**带/不带 `config/instance.env` 数字完全一致** |
| 修复 D16 前（第 1 轮试跑） | E2E 全量档 **28/30**：`applicability` 与 `telemetry:suggestions` 同时报`replace 正文缺 suggestion 块` / `improved_code diverges from posted note` | **真缺陷 D16**（审计报的是真问题，不是假阳性）：「收缩建议」走 general 分支刻意不给 Apply 按钮，却落库记 `replace`、正文复用含「点上方『应用建议』按钮」的页脚 | general 分支与 `text_only` 同构（无 Apply 引导页脚 + 落库 `text_only` + 不落 `improved_code`）+ 4 例新测试 |
| **正式 3 轮**（D15、D16 修复后） | 见 §13.1 | 全部一致 | 无需再修 |


---

## 14. 第三轮验证（补齐剩余项后连跑 3 遍）

> 触发原因：用户要求「总结修复，看看这几轮还有哪些问题没修复，有就修复，再进行 3 轮完整验证」。
> 本轮先**逐项核实**上一轮遗留清单（每条都做实证核实，不靠推测），确认属实后修复；再按同一脚本连跑 3 遍完整验证。
> 轮次脚本：`/tmp/verify_round.py <n>`（产出 `/tmp/verify-round-<n>.json`）；E2E 每轮用独立临时 GitLab 项目；L3 为**只读**复验，不触发新检视。

### 14.1 本轮修复清单（详见 §7.1c）

| 编号 | 类型 | 内容 | 测试 |
|---|---|---|---|
| **E8** | 真缺陷 | `review_runs.model` 一列两义 → 拆 `model`（provider 实际回报）+ `model_configured`（配置 id）；skipped 运行的 `model` 置空 | 8 例 |
| **E10** | 覆盖补齐 | MR 事件 `close`/`merge`/`draft`/未知 action 分支（此前完全无覆盖） | 13 例 |
| **E11** | 真缺陷 | token 断言只做恒等式 → 改为 4 条成本信号语义断言 | 10 例 |
| **E12** | 覆盖补齐 | 跨轮次 supersede 完整链路（此前只有单点覆盖） | 12 例 |
| **E13** | 覆盖补齐 | 周报 live 字段（标题/emoji/dashboard 链接/分片数） | 13 例 |
| **E1/E3** | E2E 流程 | `apply_closure`（真实 Apply 闭环）、`reconcile_resolved`（reconciler 链路） | E2E 实跑 |
| **E6/E7** | 诊断/接口 | provider usage 诊断日志；时间线 `effective` 列（复用 store 单一事实源） | — |
| **O5** | 运维 | GitLab E2E 临时项目堆积 → 新增 `scripts/ops/cleanup_e2e_projects.py`（默认 dry-run + 保护名单） | 26 例 |

### 14.2 三轮逐项对照

| 指标 | 第 1 轮 | 第 2 轮 | 第 3 轮 | 一致性 |
|---|---|---|---|---|
| 服务健康 / 指纹 | ✅ a1a53cd6cd5444d2 | ✅ a1a53cd6cd5444d2 | ✅ a1a53cd6cd5444d2 | ✅ 一致 |
| L1 passed | 1655 | 1655 | 1655 | ✅ 一致 |
| L1 failed | 10 | 10 | 10 | ✅ 一致 |
| L1 skipped | 2 | 2 | 2 | ✅ 一致 |
| L1 失败集合 | test_qodercli_subprocess_fallback.py | test_qodercli_subprocess_fallback.py | test_qodercli_subprocess_fallback.py | ✅ 一致 |
| L2 快速档 | 21/21 | 21/21 | 21/21 | ✅ 一致 |
| L2 全量档 | 32/32 | 32/32 | 32/32 | ✅ 一致 |
| L2 全量档失败项 | 无 | 无 | 无 | ✅ 一致 |
| L3 MR !3 | 19/20 | 19/20 | 19/20 | ✅ 一致 |
| L3 MR !3 未过项 | 图2-旧head建议锚点在当前head仍可定位 | 图2-旧head建议锚点在当前head仍可定位 | 图2-旧head建议锚点在当前head仍可定位 | ✅ 一致 |
| L3 MR !4 | 20/20 | 20/20 | 20/20 | ✅ 一致 |
| L3 MR !4 未过项 | 无 | 无 | 无 | ✅ 一致 |
| MR !3 applicability | {'replace': 12, 'delete': 1, 'text_only': 2} | {'replace': 12, 'delete': 1, 'text_only': 2} | {'replace': 12, 'delete': 1, 'text_only': 2} | ✅ 一致 |
| MR !4 applicability | {'text_only': 3, 'replace': 17, 'delete': 3} | {'text_only': 3, 'replace': 17, 'delete': 3} | {'text_only': 3, 'replace': 17, 'delete': 3} | ✅ 一致 |
| MR !4 严重度分布 | {'high': 19, 'medium': 3, 'low': 1} | {'high': 19, 'medium': 3, 'low': 1} | {'high': 19, 'medium': 3, 'low': 1} | ✅ 一致 |
| 文档数字自检 | ❌¹ | ❌¹ | ❌¹ | ✅ 一致 |
| 全量档报告 | report-20261112T002531Z.json | report-20261112T003339Z.json | report-20261112T004136Z.json | ⚠️ 有差异 |
| 单轮耗时(s) | L1 25.4 / fast 0.5 / full 449.6 | L1 33.7 / fast 0.6 / full 439.9 | L1 32.5 / fast 0.6 / full 430.2 | ⚠️ 有差异 |

### 14.3 结论

> ¹ 文档数字自检在三轮**跑验证时**显示 ❌，是因为自检项「文档含 §14 第三轮验证记录」要求本章节
> **已经存在** —— 而 §14 是在三轮跑完后才写入的（写完复跑即 **22/22 一致**，见下）。
> 这是自检脚本与章节生成顺序的**时序问题**，不是内容缺陷；三轮本身的核心指标完全一致。

**三轮核心指标完全一致**（L1 通过/失败/跳过数与失败集合、E2E 快速档与全量档通过数、L3 两个样本的通过数、文档自检结果全部相同）→ 结论**可复现，无偶发**。

### 14.4 盘点时判定为「非缺陷 / 不擅动」的候选项

上一轮遗留清单里的其余候选项，本轮**逐条做了实证核实**。结论如下 —— 把它们明确记下来，避免下一轮重复盘点时又把它们当成「未修复的缺陷」。

| 编号 | 候选缺口 | 核实结论 | 处置 |
|---|---|---|---|
| **E2** | `/review` 命令未实现 | README **已如实标注**「未实现」—— 文档与实现一致，不是缺陷 | **功能决策，不擅动**（属新增功能，超出「修复剩余项」范围） |
| **E4** | 「跨 head 的陈旧建议无法自动判定失效」（O2） | 真实业务 MR !3 实测 1/15 条建议的目标代码在当前 head 已被重构移除；按设计决策 `0b96f7b`「新 head 不隐藏未解决建议」**刻意不**自动 supersede | **产品口径决策**，不擅动（改动会同时影响「图1 不隐藏未解决建议」的口径） |
| **E5** | token 恒为 0 | 已定位为**上游语义**：usage 的 `input_tokens`/`output_tokens` **键存在但值为 0**（`scripts/ops/probe_qoder_usage.py` 探针实证），非 ReviewAgent 管道缺陷 | **已用 E6 诊断日志 + 探针钉死**；`total_tokens` 改 NULL 语义优先级降为低（不改变任何决策） |
| **E9** | E2E 里 push 事件被 `is_bot` 拦截 | E2E 用 `reviewagent-e2e-bot` PAT 提交 → actor 是机器人 → `router._handle_push` 的 `locks.is_bot(actor)` 跳过 —— **设计行为** | **非缺陷**；真实用户 push 路径已由真实业务 MR !4 实证（`push.auto_detect_applied applied=1`） |

### 14.5 本轮的**关键认知修正**

| 原结论 | 修正后 | 依据 |
|---|---|---|
| 「上游 Qoder 服务端 usage **未回填** token 字段」 | 「上游**明确回填了 0 值**」—— 键存在（`input_tokens` / `output_tokens`），值为 0；而 `credits` / `context_usage_ratio` 非零 | E6 诊断日志打出 `usage_keys` 发现 token 键**存在**；`scripts/ops/probe_qoder_usage.py` 探针取回原始 usage 字典确认 |
| 「p31 上零信号的 success run = 计量丢失」 | 「分两类：**全复用早退**（本轮根本没调 LLM，token=0 正确）与**真调了但上游回 0**」 | `reviewagent/review/runner.py` 的 `all_reused` 早退分支；E8 拆分 `model` 后两者首次可区分 |
| 「E2E 里 push 被 `is_bot` 拦截 = 缺陷」 | 「**设计行为**」：E2E 用机器人 PAT 提交 → actor 是机器人 → 跳过；真实用户 push 正常触发 | 真实业务 MR !4 实测 `push.auto_detect_applied applied=1` |
| 「E2E 全量档 4 项失败 = apply_closure 的问题」 | 「**顺序问题**」：会改状态的 flow 排在审计断言之前 → 改文件导致 MR head 变化 → 污染审计基线产生**假失败** | `flows.STATE_MUTATING_FEATURES` + `run_e2e.py` 收尾阶段；修复后全量档全绿 |

### 14.6 遗留（需业务/运维决策，本轮不擅动）

| 项 | 说明 |
|---|---|
| `weekly_target_project_id=34` 在 GitLab 返回 404 | 实际存在 29 / 31 —— 定时周报目标项目无效，**需确认指向哪个** |
| `/review` 命令未实现 | README 已如实标注 —— 属功能决策，未擅自实现 |
| GitLab 上 38 个 `deletion_scheduled` 项目 | GitLab 19.5 异步删除未真正完成 —— 需换版本或人工在 UI 清 |
| root 名下历史遗留项目 id=3 | 机器人无权限删除 —— 需 root 账号在 UI 清理 |
| reconciler 无常驻进程 | 本机 launchctl 只有 weekly —— 需确认是否加载 `scripts/com.jarvs.reviewagent.reconciler.plist` |
| 钉钉机器人 token 建议轮换 | 2026-11-11 复验时一条未脱敏 `grep` 把它打进了会话工具输出；该 token **未入库、未进代码/文档**，但建议轮换 |


---

## 15. 第四轮验证（降噪规则包落地 + 真实业务 A/B 对照）

> 触发原因：用户新增「降噪规则包」（`noise_boundary.md`），要求「加入到实际场景中进行验证，
> 部署后进行全量验证，重点关键规则是否生效，检视效果是否理想，检视意见是否正常，
> 数据采集是否正常，边界场景是否完善，有问题 fix」。
>
> 本轮做法：**同内容受控 A/B** —— 在真实业务仓库 p31 上开两份 **完全相同** 的 MR
> （`feature/nb-verify-on-20261112` → MR !5，`feature/nb-verify-off-20261112` → MR !6，
> 两者 head 均为 `00347d8`），一份跑「边界开启」、一份跑「边界关闭」（= `HEAD` 基线），
> 逐条比对检视结果；随后在 MR !5 上跑完意见链路、数据采集、L3 复验，最后连跑 3 轮完整验证。

### 15.1 降噪包落地核实（接线 / 装配顺序 / 注入位置）

| 项 | 核实结论 |
|---|---|
| 规则包文件 | `profiles/ssd-python-autotest/rules/noise_boundary.md`（frontmatter `priority: 900` / `render: verbatim` / `applies_to.languages: [python]`） |
| 接线 | `profile.yaml` → `rules: [rules/noise_boundary.md, rules/general_rules.md]` |
| 装配顺序（实测 `assemble_base().sources`） | `noise_boundary.md → general_rules.md` ✅（900 > 800，按 priority 降序） |
| `verbatim_body` 内位置（实测） | `⛔ 检视边界` @4 → `🟡 优先 3 通用规则` @1550 → 规则表 @2549 |
| `rules_general` 分支（无仓库规则） | 边界在**最前**（@4） |
| `rules_repo` 分支（有仓库规则） | 仓库规则 @0 → **边界 @474** → 通用规则 @2020；仓库规则段「命中即产 suggestion」原样保留 |
| chunk prompt 全文位置 | 优先1 跨文件 @5 → 优先2 仓库规则 @38 → **边界 @512** → 优先3 通用规则 @2058 |

**结论**：边界包**物理位置**落在「跨文件影响」「仓库自定义规则」之后、「通用规则」之前 —— 与它
「只裁剪通用 / 风格层、强制例外优先」的语义完全自洽（强制例外被排在边界之前，模型先读到它们）。

### 15.2 本轮修复清单（详见 §7.1c）

| 编号 | 类型 | 内容 | 落地 |
|---|---|---|---|
| **N1** | 真缺陷（提示词自相矛盾） | `rules/general_rules.md:86` 残留「所有 severity 都产 suggestion」——与边界包的 `low` 两问自检**直接矛盾** | 改为「确有影响时所有 severity 都可产 suggestion；其中 `low` 须先通过 low 两问自检」 |
| **N2** | 真缺陷（提示词自相矛盾） | `prompts/improve.md` 的「一律产出 suggestion」/「每个可疑 bug 都必须有一条 suggestion」与边界包冲突 | 改为「每个**有影响**的问题」，并注明「属强制例外，不受降噪影响」 |
| **N3** | 覆盖补齐 | 契约测试只查「文件存在」，**没查边界是否真的进了 prompt** | `test_boundary_pack_is_actually_injected_into_improve_prompt` + `test_general_rules_has_no_unbounded_mandate` + `test_low_must_pass_boundary_self_check`（5 例 → 8 例） |
| **N4** | 真缺陷（**过度降噪**，本轮 A/B 实测发现） | 边界开启时一条**会误导**的过期注释被整条裁掉，而关闭边界的基线反而报了出来 → 边界比基线还漏报 | 根因：「仍要报」只写在不报清单的**表格单元格里**，不在模型优先读的「强制例外」清单中 → 把 `R-OTHER:stale_comment`（会误导）**提升为强制例外的独立条目**，并在「判定顺序」第 1 条写明「命中强制例外的候选先跳过不报清单」；同步新增契约测试 `test_misleading_comment_is_first_class_exception` |
| **N5** | 覆盖补齐 | 装配环节静默丢弃边界包时，所有文件级契约测试照样绿 | `test_boundary_gates_general_rules_only` 锁死两条分支的物理位置关系（仓库规则 → 边界 → 通用规则） |
| **N6** | 真缺陷（**数据面**：迁移后汇总口径滞后） | `cohort_key` 口径迁移把 MR !4 的独立问题数从 23 修正为 30（DB 对了），但**已发布的「检视汇总」note 仍停留在旧口径 23** → 图1 与图2 对不上，L3 掉到 16/20 | 新增 `scripts/ops/refresh_overviews.py`（默认 dry-run，`--apply` 幂等重刷）+ `tests/test_refresh_overviews.py`（9 例）；对 p31 跑一次后 **L3 恢复 20/20**（详见 §15.8） |

> **N4 的修复有真实业务实证**：修复后第 3 轮检视（同一份 diff）把该条找回来了
> —— `R-OTHER:stale_comment`「注释与实现矛盾」（`gc_probe.py:48`，low），
> 理由「注释称返回值是『布尔标识』，但实现返回 `disk.gc_state`，按注释使用会把任意非空字符串当作 True」。

### 15.3 真实业务 A/B：同一份 diff，边界开 vs 边界关

样本：`testlib/lib_baw/gc_probe.py`（新增）+ `testcase/test_gc/test_gc_noise_probe.py`（新增），
覆盖 仓库规则命中 / 强制例外 / 各类噪音样本；两份 MR 内容**逐字节相同**（head `00347d8`）。

| 发现类别 | 边界开 MR !5 | 边界关 MR !6 | 差异解读 |
|---|---|---|---|
| 仓库自定义规则 `SSD-RULE-*` | 9 | 4 | 边界把「仓库规则明确要求」写进强制例外 → **召回 +5**（allure 字段 / 用例 docstring / 硬编码超时 / 裸 print / 分层隔离） |
| 跨文件 caller 失配 `R-OTHER-IMPACT:*` | 11 | 7 | 强制例外 → **+4** |
| 安全：命令注入 `R-SHELL` | 2 | 0 | **基线完全漏掉命令注入**；边界把「安全漏洞」写成强制例外后被拉回 |
| 假通过 / 假失败（真问题） | 4 | 1 | 边界开的 4 条全是真问题（必然假失败 / 异常断言过宽 / 吞异常 / 无效断言） |
| 断言消息 / 断言强度（`R-ASSERT` 低价值） | 0 | 3 | 边界裁掉；其中「`None != ""` 造成假通过」那条在 **N4 修复后的第 3 轮**被强制例外拉回 |
| 硬编码临时路径 `R-TMP` | 1 | 1 | 一致 |
| 标识符拼写 `R-OTHER:typo`（low） | 1 | 1 | 一致 —— **low 两问自检没有把标识符拼写误杀** |
| 会误导的过期注释（low） | 1 | 1 | **修复前边界开 = 0**（N4），修复后与基线一致 |
| 死代码 `R-OTHER:dead_code`（low） | 1 | 1 | 一致 |
| 缺导入 `R-OTHER:missing_import` | 1 | 0 | 边界开补回 |
| **合计** | **30** | **19** | **噪音更少 + 信号更多** |

> 计数说明：MR !5 的 30 条来自 4 轮 improve（第 4 轮是 `/adopt` 触发的自动复检）；
> 其中 `gc_probe.py:78` 一条同时引用 `SSD-RULE-LIB-LAYER-ISOLATION` 与 `R-SHELL`，分类表里跨两类各计一次。

**降噪效果判定**：
- **噪音被裁掉**：基线里的 3 条「断言补 msg / 断言强度不足」类低价值建议，边界开启后 0 条；
- **信号被加强**：命令注入（安全）、3 条仓库规则、缺导入、4 条 caller 失配在基线里**没有**或更少，
  边界开启后全部补齐；
- **没有误伤信号**：边界开启后的 30 条里，**纯文本措辞 / 等价改写 / 泛化建议 / 命名口味 = 0 条**；
- **唯一的过度降噪（会误导的注释）已修复并复验通过**。

### 15.4 边界场景矩阵（逐项核对）

> ⚠️ **口径已于第五轮变更（2026-11-13）**：本表「强制例外：命令注入（安全）」一行按**当时**口径
> 记为「报」。第五轮起，**安全类在本场景不报**（内网测试台内部代码，无外部攻击面），
> `R-SHELL` 已从强制例外移入不报清单 —— 该行与 §15.3 的「安全：命令注入」行属**历史口径**，
> 保留原样以免篡改历史记录；**现行口径见 §16.3**。
> 硬件数据类（`R-DATA` / `R-NVME`）与仓库自定义规则**任何时候都不受降噪影响**。

| 场景 | 期望 | 实测 | 结论 |
|---|---|---|---|
| 仓库规则：测试函数缺 allure 字段 | 报 | ✅ `SSD-RULE-CASE-ALLURE-FIELDS`「缺失 allure 字段」 | 通过 |
| 仓库规则：用例 docstring 缺必须段 | 报 | ✅ `SSD-RULE-CASE-DESCRIPTION`「补齐用例 docstring」 | 通过 |
| 仓库规则：裸 print | 报 | ✅ `SSD-RULE-COMMON-NO-BARE-PRINT` | 通过 |
| 仓库规则：硬编码超时 / 临时路径 | 报 | ✅ `SSD-RULE-COMMON-NO-HARDCODED-CONFIG`、`R-TMP` | 通过 |
| 仓库规则：BAW 跳层 | 报 | ✅ `SSD-RULE-LIB-LAYER-ISOLATION` | 通过 |
| 强制例外：命令注入（安全） | 报 | ✅ `R-SHELL`（**基线未报**） | 通过 |
| 强制例外：裸 except 吞异常 | 报 | ✅ `R-ERR` + `SSD-RULE-COMMON-NO-LOG-EXC` | 通过 |
| 强制例外：假通过（`None != ""`） | 报 | ✅ `R-ASSERT`「修复无效断言」 | 通过 |
| 强制例外：假失败（枚举与字符串比较） | 报 | ✅ `R-OTHER:false_failure` / `R-OTHER-IMPACT:type_contract` | 通过 |
| 强制例外：caller 失配（方法 / 属性 / 接口缺失） | 报 | ✅ `R-OTHER-IMPACT:missing_method` / `missing_attribute` / `missing_api` / `schema_drift` | 通过 |
| low 两问自检：标识符拼写 | 报（两问都能答上） | ✅ `R-OTHER:typo`（low） | 通过 |
| low 两问自检：会误导的过期注释 | 报（修复后） | ✅ `R-OTHER:stale_comment`（low，N4 修复后） | 通过 |
| 噪音：纯文本措辞 / 注释拼写 | 不报 | ✅ 0 条 | 通过 |
| 噪音：等价改写（`total = total + x` / `if-else` 返回布尔） | 不报 | ✅ 0 条 | 通过 |
| 噪音：泛化建议（建议补测试 / 加类型注解） | 不报 | ✅ 0 条 | 通过 |
| 噪音：命名口味 | 不报 | ✅ 0 条 | 通过 |
| 噪音：描述类（MR 标题 / 描述本身） | 不报 | ✅ 0 条 | 通过 |

### 15.5 检视意见链路（/adopt / /dismiss）

| 步骤 | 结果 |
|---|---|
| 机器人发汇总 note | ✅ 「## 改进总览 V1/V2」+「## 检视汇总」表（严重度 × 待处理/已采纳/已忽略/已关闭） |
| inline 评论 | ✅ 30 条全部落地，锚点行号与 diff 一致 |
| `/adopt`（未改代码） | ✅ 机器人回复「未检测到这条建议对应位置的代码修改，暂不能标记为手工采纳」并记 `validation_status=same-head`（**设计行为**：要求先提交对应位置的修改） |
| `/adopt`（先改代码再采纳） | ✅ `state=applied`、`applied_at` 落库、`adoption_source=adopt_command`、机器人回复「✅ 已采纳建议 (检测到目标行有修改)」 |
| `/dismiss` | ✅ `state=dismissed`、`dismissed_at` 落库 |
| `/adopt` 自动复检 | ✅ 触发第 4 轮 improve（`review_runs.id=264`），新发现 2 条并入汇总 |
| 图1 / 图2 一致性 | ✅ L3 复验 **20/20 通过**（见 §15.6） |

### 15.6 数据采集核对（MR !5）

| 采集项 | 结果 |
|---|---|
| `review_runs` | ✅ 6 条运行记录：`command` / `status` / `started_at` / `finished_at` / `model` / `model_configured` / `llm_provider` / `rule_keys_cited` / `suggestion_count` / `cost_credits` / `duration_ms` 全部落库 |
| `suggestions` | ✅ 30 行；`severity`（high 22 / medium 5 / low 3）、`state`（open 27 / applied 1 / dismissed 2）、`rule_keys`（19 个不同键）、`label`（security / potential bug / cross-file impact / code quality）、`severity_source=rule` 全部落库 |
| applicability 三态 | ✅ `replace 18 / text_only 11 / delete 1`，取值合法且与正文形态一致 |
| `suggestion_actions` | ✅ 4 条动作行（adopted ×2 / dismissed ×2），含 `actor_username` / `reason` / `validation_status` / `head_sha_posted` / `head_sha_current` |
| 规则键引用可追溯 | ✅ L3「图2-规则键引用」**30/30 通过**（每条 suggestion 的 `rule_keys` 都能在评论正文里找到） |
| 图1 汇总 ↔ DB | ✅ 总数 30 / 采纳率 3.3% / HIGH 20·1·1·0·22 / MEDIUM 0·0·0·5 / LOW 2·0·1·0·3 —— 与 DB **逐格相等** |
| 图2 ↔ GitLab | ✅ 30 条 1:1（无缺失 / 无多余），resolve 状态零不一致，同 head 行号锚点严格相等 |

### 15.7 本轮的配置护栏发现（O 编号）

| 编号 | 说明 | 处置 |
|---|---|---|
| **O6** | **单文件建议上限（默认 5 条/文件）会掩盖后续候选** —— 首轮检视实测 `per_file_limit max_per_file=5 truncated=5`，第 6 条起只进汇总不落库，导致「硬编码路径」「会误导注释」两条真实问题**看不到** | 测边界效果时必须临时解除上限（本轮用 `IMPROVE_MAX_SUGGESTIONS_PER_FILE=50` + `IMPROVE_MAX_SUGGESTIONS=100`，**验证结束后已回滚**，生产默认仍是 5 / 15） |
| **O7** | token 计数仍恒为 0 —— 上游 usage 回填 0 值（沿用 §14 E5/E6 结论，非本轮引入） | 记录，不处理 |
| **O8** | `instance.env` 新增变量**必须**同步进 `tests/conftest.py::_DEPLOY_ENV_KEYS`，否则 `test_deploy_env_keys_cover_instance_env` 会红 —— 本轮临时加上限变量时**实测触发**了该护栏 | 临时变量已回滚，回归恢复全绿 |

### 15.8 本轮新发现的**数据面缺陷**（N6）—— 迁移后汇总口径滞后，已修

**现象**：本轮重启服务后，`telemetry.store` 的 `cohort_key` 口径迁移
（`PRAGMA user_version=1`，新算法 = file+header+**内容指纹**）首次在本机执行，
把 p31 MR !4 的历史建议从 **23 个 cohort 重新分成 30 个** —— 这本身是**修正**：
旧算法（file+header）会把「同文件、同标题、不同位置」的独立问题误并成一条，
导致**汇总少算 7 条、采纳率虚高**（迁移注释里写明的正是这个 bug）。

**但迁移只回填 DB，不会回头刷新已经发布在 GitLab 上的「## 检视汇总」note**
→ MR !4 出现「图1 汇总 = 23」而「图2 实际 inline 评论 = 30」的不一致，
L3 复验从 20/20 掉到 **16/20**（4 项全在图1 对账）。

**修复**：
1. 新增运维脚本 `scripts/ops/refresh_overviews.py` —— 默认 `--dry-run` 只报告
   「DB 口径 ≠ 已发布口径」的 MR，`--apply` 才用 `publish_overview`（幂等的
   find/update 型）重刷汇总正文；**不触发任何检视**（不调 LLM）；
2. 对 p31 跑了一次：`MR !4: DB=30 已发布=23 → 已重刷（note 1228）`，
   重刷后 **L3 恢复 20/20**（图1 总数 30 / 采纳率 6.7% / HIGH 25·1·0·0·26 / 总计 27·2·1·0·30 与 DB 逐格相等）；
3. 新增测试 `tests/test_refresh_overviews.py`（9 例，锁住解析与判定逻辑 + 锚点一致性）。

> ⚠️ **口径变更提示**：MR !4 的「总建议数」由 23 变为 30、采纳率由 8.7% 变为 6.7%，
> 是**迁移修正后的正确口径**（图2 与 GitLab 一直是 30 条 1:1）。
> §12 / §14 里记录的 23 / 8.7% 是**迁移前**的旧口径历史值，保留原样以免篡改历史记录。

### 15.9 本轮回归基线（3 轮完整验证用同一脚本）

| 项 | 值 |
|---|---|
| L1 单元回归 | **1686 passed / 0 failed / 3 skipped**（HEAD 基线 1436 → 净增 **+250**；本轮新增 11 例：契约 5 例 + 运维脚本 9 例，另含上一轮 E 轮次用例） |
| 场景包指纹 | `08cced18342641ffa2a7301f04ada8d343eef5d1366ad7a07317d9dace37c91b`（含降噪边界包 + N4 修复后的规则正文） |
| 服务健康 | `/readyz` → `{"status":"ok","profile":"ssd-python-autotest"}`；磁盘/运行态指纹一致（`stack._check_profile_freshness()` → True） |
| 配置护栏 | 临时验证用的建议上限覆盖**已回滚**，`config/instance.env` 恢复生产默认（单文件 5 / 总数 15） |

### 15.10 三轮逐项对照（同一脚本 `/tmp/verify_round.py <n>` 连跑 3 遍）

| 指标 | 第 1 轮 | 第 2 轮 | 第 3 轮 | 一致性 |
|---|---|---|---|---|
| 服务健康 / 指纹 | ✅ `08cced18342641ff` | ✅ `08cced18342641ff` | ✅ `08cced18342641ff` | ✅ 一致 |
| L1 passed | 1686 | 1686 | 1686 | ✅ 一致 |
| L1 failed | 0 | 0 | 0 | ✅ 一致 |
| L1 skipped | 3 | 3 | 3 | ✅ 一致 |
| L1 失败集合 | 无 | 无 | 无 | ✅ 一致 |
| L2 快速档 | 21/21 | 21/21 | 21/21 | ✅ 一致 |
| L2 全量档 | 32/32 | 32/32 | 32/32 | ✅ 一致 |
| L2 全量档失败项 | 无 | 无 | 无 | ✅ 一致 |
| L3 MR !3 | 19/20 | 19/20 | 19/20 | ✅ 一致 |
| L3 MR !3 未过项 | 图2-旧head建议锚点在当前head仍可定位 | 同左 | 同左 | ✅ 一致（O2 产品口径，非缺陷） |
| L3 MR !4 | 20/20 | 20/20 | 20/20 | ✅ 一致 |
| MR !3 三态分布 | {'replace': 12, 'delete': 1, 'text_only': 2} | 同左 | 同左 | ✅ 一致 |
| MR !4 三态分布 | {'replace': 24, 'delete': 3, 'text_only': 3} | 同左 | 同左 | ✅ 一致 |
| MR !4 严重度分布 | {'high': 26, 'medium': 3, 'low': 1} | 同左 | 同左 | ✅ 一致 |
| 文档数字自检 | ✅ 26/26 | ✅ 26/26 | ✅ 26/26 | ✅ 一致 |
| 全量档报告 | report-20261112T162426Z.json | report-20261112T163648Z.json | report-20261112T164821Z.json | ⚠️ 文件名差异（时间戳） |
| 单轮耗时(s) | L1 241.0 / fast 0.5 / full 505.8 | L1 197.7 / fast 0.5 / full 491.6 | L1 197.7 / fast 0.5 / full 434.9 | ⚠️ 有差异（耗时） |

**统一签名** `(L1 passed, failed, skipped, 失败集合, fast passed/total, full passed/total, L3 mr3, L3 mr4)
= (1686, 0, 3, (), 21, 21, 32, 32, 19, 20)` —— 三轮**完全一致** → 结论**可复现，无偶发**。

> 与 §14 的差异说明：L1 基线由 1655 → **1686**（本轮新增 11 例契约测试 + 上一轮 E 轮次用例）；
> MR !4 的总数由 23 → **30**、三态与严重度分布同步变化 —— 原因见 §15.8（cohort 口径迁移修正）。

### 15.11 本轮结论

1. **降噪规则包已真实生效**：装配顺序 `noise_boundary → general_rules` 实测正确，边界包真实进入
   chunk / batch prompt（契约测试 + 位置实测双重锁定）。
2. **检视效果理想**：同一份 diff 的受控 A/B 显示 —— 边界开启后**噪音被裁**（断言消息/强度类 3 条 → 0，
   纯文本措辞 / 等价改写 / 泛化建议 / 命名口味全为 0），同时**信号变多**（仓库规则 +5、caller 失配 +4、
   补回基线完全漏掉的**命令注入**安全问题）。
3. **仓库自定义规则不受降噪影响**：allure 字段 / 用例 docstring / 裸 print / 硬编码 / 分层隔离
   全部照常产出（9 条）。
4. **检视意见正常**：汇总 note（V1/V2）+ inline 评论 + `/adopt`（含「先改代码再采纳」两段式）+
   `/dismiss` + 自动复检 全链路打通，L3 **20/20**。
5. **数据采集正常**：`review_runs` / `suggestions` / `suggestion_actions` 三张表字段齐全，
   applicability 三态合法，规则键引用 30/30 可追溯，图1 ↔ DB ↔ GitLab 三方逐格相等。
6. **发现并修掉 6 个问题**（N1–N6），其中 **N4（过度降噪导致漏报会误导注释）** 与
   **N6（迁移后历史汇总口径滞后）** 都是**只有跑真实业务 A/B 才会暴露**的问题 ——
   纯静态检查和单测都发现不了。

---

## 16. 第五轮验证（安全类降噪 + 全面完整验证 2 遍）

> 触发原因：用户指出「检视对象是**仓库内部的自动化测试代码**（跑在内网测试台，无外部攻击面），
> 类似什么注入、安全等问题不需要关注；还有其他常规检视类似这类安全等在这个场景包也需要降噪，
> 看看还有哪些可以放降噪里面，**优化后全面完整验证全部点 2 遍，有问题 fix**」。
>
> 本轮分两件事：
> 1. **口径变更**——把「安全类」与「依赖与环境」纳入降噪包的不报清单，并全面对齐提示词与规则包；
> 2. **全面完整验证 2 遍**——同一个完整验证脚本（L1 + E2E fast/full + L3 + 文档自检）连跑 2 遍，
>    要求两轮**签名完全一致**，且过程中发现的问题当场修复。

### 16.1 口径变更：本场景「不报」的范围

场景前提（写入 `noise_boundary.md` 顶部，作为本节全部裁剪的依据）：

> 被检对象是**仓库内部自动化测试代码**（`testcase/` / `testlib/` / `test_components/` / 测试
> fixture），运行在**内网测试台**上、由流水线调度，既不对外提供服务，也不处理外部用户输入。
> 因此「面向生产服务的安全加固」类检查在本场景**没有可执行的后果**，一律不报。

| 类别 | 本轮新增的「不报」 | 仍要报的例外 |
|---|---|---|
| 安全类 | 命令 / SQL 注入、`shell=True` 拼接外部入参、明文凭据（口令 / token / 私钥写死在代码里）、越权、路径穿越、不安全反序列化（`pickle` / `eval`）、弱哈希 / 非密码学随机、敏感信息打到日志、文件权限过宽 | ——（本场景无对外攻击面，无例外） |
| 依赖与环境 | 三方依赖未锁定版本；强绑系统独有命令而无多系统兼容兜底 | 可选依赖 `import` 失败导致用例无法执行；环境不兼容导致整批用例挂掉 |

**明确不降噪的部分**（防止「安全类一刀切」误伤硬件数据面）：

- `R-DATA` / `R-NVME`（下发载荷 / LBA 地址 / 扇区长度的值域与对齐校验）**仍要照常报** ——
  这类问题会造成**硬件损坏 / 数据错误**，不是「安全加固」，后果可执行；
- 数据损坏、硬件损坏、用例假通过假失败、断言被吞、资源泄漏 / 僵尸进程 / 并发无隔离、
  `R-OTHER-IMPACT:*` caller 失配、会误导的过期注释、仓库自定义规则 —— **全部保留为强制例外**。

> ⚠️ **残留风险（用户明确决策，已知悉）**：降噪后，仓库历史代码里**真实存在**的硬编码 SSH 口令
> （`testlib/lib_baw/gc_operation.py`）不再由检视兜底。本场景无外部攻击面，该决策可接受；
> 但若将来这套代码被挪到有外部访问面的环境，需要重新评估。

### 16.2 改动落地（提示词 / 规则包 / 接线）

| 文件 | 改动 |
|---|---|
| `rules/noise_boundary.md` | 新增「场景前提」段；不报清单新增「安全类」「依赖与环境」两行；强制例外**删掉**「安全漏洞（命令 / SQL 注入、明文凭据、越权）」并改写为「数据损坏、硬件损坏…」；新增「安全类不报 ≠ 硬件数据类不报」提示；「与正向规则的关系」明确 `R-SHELL` 不产出 |
| `prompts/improve.md` | 降噪段重写（优先级去掉「安全」、默认不报加安全类与依赖项、仍要报改为「数据 / 硬件 / 假通过假失败」+ caller 失配）；强制要求段拆出「安全类例外」；**删除** eval / pickle / MD5 跳过理由；锚点示例由 shell 注入改为硬编码 `/tmp` 路径；`R-SHELL` 标签示例替换；self-check 新增「首句以规则键开头」（N7） |
| `rules/general_rules.md` | 严重度口径删「命令注入」；新增「本场景不报」小节（`R-SHELL` / 兜底安全类 / `R-DEP` 两项）；规则表 `R-SHELL` 行的类别列标注「**本场景不报**」；硬件数据类仍要报的提示 |
| `tests/test_noise_boundary_contract.py` | 契约 **5 → 15 例**：新增「安全类在不报清单」「安全不得在强制例外」「general_rules 声明 `R-SHELL` 不报」「improve.md 与边界口径一致」「rationale 必须以规则键开头」等 |
| `scripts/ops/check_agent_drift.py` + `scripts/sync_qoder_agents.py` + `scripts/restart_local.sh` | N8 漂移守卫（见 §7.1e） |
| `tests/test_check_agent_drift.py` | 新增 6 例 |

**checksum / golden 同步**：改动只涉及规则正文与提示词正文 → 规则正文基线（`VERBATIM` sha
`e77f5eaf9ae380d1…`、`general_rules.md` 体量 7580 / 14262 字节 / sha `2d0c3fbbefb54e21…`）
与 4 处 VERBATIM 基线、`test_rules_parser` 体量/sha、`test_planner` / `test_rule_checksum` /
`test_rules_repo` / `test_rules_resolver` 基线**均已同步且实测一致**；
golden 提示词 6 个文件 + `manifest.json` 全量重生成。

### 16.3 边界场景矩阵（第 5 遍口径，真实业务逐条核对）

样本：真实业务仓库 p31 的 `testlib/lib_baw/gc_probe.py` + `testcase/test_gc/test_gc_noise_probe.py`
（含**明文凭据样本** `connect_gc_console`：`SSHClient("10.20.65.25","22","root","dml@lab123456")`）。

| 场景 | 期望 | 实测 | 结论 |
|---|---|---|---|
| 安全：明文凭据写死 | **不报** | ✅ **0 条**（样本行完全未产出 finding） | 通过 |
| 安全：命令注入（`subprocess` 拼串 / `shell=True`） | **不报** | ✅ **0 条** `R-SHELL`；同一行改报**真实的仓库规则问题** `SSD-RULE-LIB-LAYER-ISOLATION`（分层越界） | 通过 |
| 依赖：未锁版本 / 多系统兼容兜底 | **不报** | ✅ 0 条 | 通过 |
| 硬件数据面：`R-DATA` / `R-NVME` | **仍要报** | ✅ 保留在强制例外清单与规则表提示中 | 通过 |
| 强制例外：假通过（吞异常 → 恒 True） | 报 | ✅ `R-ERR`「修复假通过」 | 通过 |
| 强制例外：假失败（异常断言过宽） | 报 | ✅ `R-RAISE`「收窄异常断言」 | 通过 |
| 强制例外：caller 失配（方法 / 属性 / 类型契约） | 报 | ✅ `R-OTHER-IMPACT:schema_drift` ×2、`type_mismatch` | 通过 |
| 仓库自定义规则 | 报 | ✅ `SSD-RULE-CASE-ALLURE-FIELDS` + `SSD-RULE-CASE-DESCRIPTION`、`SSD-RULE-LIB-LAYER-ISOLATION`、`SSD-RULE-COMMON-NO-HARDCODED-CONFIG` | 通过 |
| 会误导的过期注释（low） | 报 | ✅ `R-OTHER:stale_comment`（low） | 通过 |
| 噪音：纯文本 / 等价改写 / 泛化建议 / 命名口味 | 不报 | ✅ 0 条 | 通过 |
| 未分类（需人工判读） | 0 | ✅ 0 条 | 通过 |

**判定**：`/tmp/nb_security_check.py 8` → **PASS**（安全类 0 / 噪音类 0 / 未分类 0 / 信号类 10）。

### 16.4 N7 复验：规则键缺失（全新 MR、干净单轮）

| 项 | MR !7（N7 修复**前**） | MR !8（N7 修复**后**，全新 MR、同内容 `5cfb096`） |
|---|---|---|
| 检视轮次 | 干净单轮 | 干净单轮 |
| 落库建议 | 16 条 | 10 条（生产上限 5/文件、15/总数生效） |
| **`rule_keys` 为空** | **1 条** | **0 条** ✅ |
| L3「图2-规则键引用」 | 15/16 | **11/11 逐键通过** ✅ |
| L3 合计 | 19/20 | **20/20** ✅ |

> MR !8 的 10 条建议里含 **1 条双键建议**（`SSD-RULE-CASE-ALLURE-FIELDS` +
> `SSD-RULE-CASE-DESCRIPTION`，落库为逗号分隔串）—— 见 §16.6「校验口径加固」。

### 16.5 N8 修复：部署态提示词漂移（静默不生效）

**现象**：`improve.md` 改完（406 行）但 `.qoder/agents/improve.md` 仍是旧版（309 行）——
`grep 规则键开头` 在部署态 **0 命中** → N7 的修复在真实检视里**根本没生效**。

**为什么前面的验证没发现**：§15 轮验证的是「**规则包**进 chunk prompt」（`noise_boundary.md` /
`general_rules.md` 走 `review/assembly.py` 现场装配），而 `improve.md` 走的是**另一条链路**
（agent system prompt → `sync_qoder_agents.py` → `.qoder/agents/*.md` → ACP server 启动时读入）。
静态检查、单测、契约测试**一条都不会报**。

**修复**：
1. `scripts/sync_qoder_agents.py` 抽出 `expected_agent_text(src)` —— 「写盘」与「校验」共用同一渲染函数
   （避免守卫自己算出一份不同的期望值）；
2. 新增 `scripts/ops/check_agent_drift.py` —— 按**内容**比对（**不看 mtime**，因为
   `sync_qoder_agents.py` 的增量写就是按 mtime 判定的，用 mtime 比对等于没比）；
   缺失 / 内容漂移 → 退出码 1；部署目录里的多余文件（历史遗留 `improve_agent.md`）只报 warning；
3. 接进 `scripts/restart_local.sh` —— sync 之后立刻校验，**漂移即中止启动**，
   绝不允许带着旧 system prompt 起 worker。

**实测**：`check_agent_drift.py` → `exit=0`（1 warning = 历史遗留 `improve_agent.md`）；
`restart_local.sh` 启动日志已包含该校验步骤。

### 16.6 校验口径加固：L3「规则键引用」逐键核对

核对 MR !8 时发现：落库的 `rule_keys` 列存的是**逗号分隔字符串**
（`store.record_suggestion` 用 `",".join(rule_keys)`），而 L3 的「图2-规则键引用」检查
**只取逗号串里的第一个键**去正文里找 → 多键建议的**第二个键从未被核对过**。

- 已把该检查改为**逐键核对**（每个逗号分隔片段都要在正文里出现）；
- 加固后重跑：MR !5 **33/33 键通过**（31 条建议）、MR !6 **20/20**、MR !7 **16/16**、
  MR !8 **11/11** —— 全部通过，**没有发现新的规则键失配**；
- 结论：**「模型把两个键塞进一个数组元素」是假象** —— 逗号串本来就是该列的存储格式，
  消费方（`rule_key_counts` / `suggestion_exists_at_line` / 周报翻译）都按逗号拆分，行为正确。
  因此**没有**为此改产品代码（曾起过一版拆分逻辑，核对存储格式后确认是无效改动，已撤销）。

### 16.6b N9：并发写 MR 标题导致 telemetry 与 GitLab 漂移（第 2 轮才暴露）

**现象**：第 2 轮完整验证的 E2E 全量档 **31/32**，唯一失败项是
`telemetry:mr_activity`：

```
title='新增 `service.py` 的 `total`、`save` 并调整 `handle`、`run` 返回结构'
!= gitlab '扩充 `service` 工具函数并调整 `handle`/`run`/`Item` 签名'
```

两个标题都是 LLM 生成的（E2E 建 MR 时的初始标题是 `E2E 真实变更 r1`）→ **describe 跑了两次**。

**根因**（worker 日志实锤）：

| 时刻 | 事件 | 是否持 per-MR 锁 |
|---|---|---|
| 01:52:22 | `worker.run_describe project=56 mr=1`（note 触发的 `/describe`） | **否** —— 日志里没有 `chain.lock_acquired` |
| 01:52:24 | `chain.lock_acquired` → 链式 describe | 是 |
| 01:52:58 | describe#1 生成标题 T1 → 写 GitLab(01:52:59) → 回写 telemetry | — |
| 01:53:00 | describe#2 生成标题 T2 → 写 GitLab | — |

- 链式路径 `run_mr_chain` **持 per-MR 锁**；note 路径走 RQ 单命令入口
  `run_describe` → `_run_command`，**不取锁**；
- `should_skip_cooldown` 是 **入队** 闸门（`SET NX EX`），**不挡执行** ——
  note 任务在队列里排了几十秒才开始跑，与链式任务**重叠**；
- 两个 describe 并发时，各自「写 GitLab → 回写 telemetry」这两步之间的**交错顺序**可以不同：
  #1 写完 GitLab(T1)、#2 写完 GitLab(T2)、#2 回写 telemetry(T2)、#1 回写 telemetry(T1)
  → **GitLab=T2 / telemetry=T1**，永久漂移（dashboard 显示旧标题）。

**修复**：`reviewagent/workers/tasks.py` 新增 `_run_locked()` —— 单命令入口
（`run_describe` / `run_improve`）**先取同一把 per-MR 链锁再执行**：

- 与链式路径是**同一把**锁 → 同一 MR 的命令**执行**串行 → 两个 sink 的「最后写入者」必然一致；
- 这把锁**不可重入** → 链式路径必须继续直接调 `_run_command`（已如此，回归测试锁死）；
- 拿不到锁（阻塞 300s 超时）→ **拒绝执行**，绝不能「裸跑」退回并发写的老路；
- 异常路径也释放锁（`finally`）。

**回归**：`tests/test_note_command_lock.py`（5 例）+ 变异验证（把 `run_describe` 改回
`_run_command` → 3 例转红）+ 修复后 2 轮完整验证 E2E 全量档 **32/32 / 32/32**。

> 本轮 L1 由 1698 → **1708**（+5 例 N9 回归 + 5 例 N10 回归测试）。

### 16.6c N10：E2E 的 Apply 模拟用错范围语义（`apply_closure` 误报红）

**现象**：E2E 全量档 `apply_closure` 失败：
`state=open adopted_actions=0 applied_commit_sha=none path=synthetic-update`。

**根因**：E2E 要「真实模拟一次 Apply」——把建议的 improved_code 写进文件 → commit → push
→ 期望 `auto_detect_applied` 把这条建议判成 applied。补丁范围取自 DB 的 `target_line_end`，
而该字段存的是**新代码跨度**：

```python
# reviewagent/review/publishing.py
n_lines = len(nc.split("\n"))                                   # nc = improved_code
target_line_end = (new_line + n_lines - 1) if n_lines > 1 else new_line
```

而 GitLab 的 Apply 按钮替换的是**建议块的旧侧**，旧侧行数由
`reviewagent/review/policies/python.py` 决定：

```python
n_replace = max(0, len(existing_lines) - 1)   # existing_lines = existing_code 的行
```

**1→N 多行替换时两者不相等**（新代码比原代码长）→ 补丁**多删**原代码之外的行，
把文件改坏 → `auto_detect` 自然认不出「建议代码已落地」。

**实证锚点**（同一份 fixture，两轮 E2E 的建议内容不同）：

| 轮次 | 建议 | `existing_code` 行数 | `improved_code` 行数 | 落库 `target_line`–`target_line_end` | 结果 |
|---|---|---|---|---|---|
| project 56 | `except:` 吞异常 | 2 | 2 | 22–23（恰好相等） | ✅ `applied` |
| project 57 | 同一处 | 2 | 4 | 22–25（**多 2 行**） | ❌ 多删 2 行 → 文件改坏 → 未判 applied |

**修复**：`scripts/e2e/flows.py::_apply_suggestion_to_file` 改为按
**`existing_code` 的实际行数**替换（`existing_code` 为空才退回 `target_line_end` 跨度），
与 GitLab 真实 Apply 同语义。同时在 `publishing.py` 的 `n_lines` 处补注释，写明
`target_line_end` 的语义是「新代码跨度」，**不可**当作「原代码被替换的跨度」使用
（生产路径只用它放宽匹配窗口，范围偏宽只是更宽松，无破坏性）。

**回归**：`tests/test_e2e_apply_patch_range.py`（5 例，含 1→N 核心回归）。

### 16.7 全面完整验证 2 遍

验证脚本：`/tmp/verify_round.py <n>` —— 每轮跑 **L1 单元回归 + E2E 快速档 + E2E 全量档 +
L3 真实业务复验（MR !3 / !4 / !8，只读）+ 文档数字自检**。

| 指标 | 第 1 轮 | 第 2 轮 | 一致性 |
|---|---|---|---|
| 服务健康 / 指纹 | ✅ `1fcce9900a8b4aee` | ✅ `1fcce9900a8b4aee` | ✅ 一致 |
| L1 passed | 1708 | 1708 | ✅ 一致 |
| L1 failed | 0 | 0 | ✅ 一致 |
| L1 skipped | 2 | 2 | ✅ 一致 |
| L1 失败集合 | 无 | 无 | ✅ 一致 |
| L2 快速档 | 21/21 | 21/21 | ✅ 一致 |
| L2 全量档 | 32/32 | 32/32 | ✅ 一致 |
| L2 全量档失败项 | 无 | 无 | ✅ 一致 |
| L3 MR !3 | 19/20 | 19/20 | ✅ 一致（O2 产品口径，非缺陷） |
| L3 MR !4 | 20/20 | 20/20 | ✅ 一致 |
| L3 MR !8 | 20/20 | 20/20 | ✅ 一致（N7 复验锚点） |
| 文档数字自检 | 通过 | 通过 | ✅ 一致 |

**统一签名** `(L1 passed, failed, skipped, 失败集合, fast passed/total, full passed/total,
L3 mr3, L3 mr4, L3 mr8) = (1708, 0, 2, (), 21, 21, 32, 32, 19, 20, 20)` —— 两轮**完全一致**。

即：L1 单元回归 **1708 passed / 0 failed / 2 skipped**，E2E 快速档 21/21、全量档 32/32，
L3 真实业务复验 MR !3 19/20、MR !4 20/20、MR !8 20/20。

> **E2E 报告文件名的坑（复现须知）**：报告名 `report-<档位开始时刻>.json`，而 fast 档只跑
> 0.5s —— 它与随后的 full 档**常常在同一秒启动 → 共用同一个文件名 → full 覆盖 fast**。
> `verify_round.py` 在两档**各自跑完后立刻**读回（fast 在 full 之前读），所以读到的数值始终正确；
> 但事后按文件名反查时会看到「同一个文件里 tier=full」。核对时以 mtime + tier 字段为准。

**L1 基线口径说明**（避免与 §15 的 1686 直接相减造成误解）：
- §15 记录的 1686 / 0 / 3 是**更早的树状态**测得（此后 `new_review_v2` 又落地了 2 个提交）；
- 本轮的可复现对照是 **HEAD（`7bb7ab7`）收集 1670 例 → 当前收集 1710 例 = 净增 +40 例**，
  与本轮新增用例数**逐项对上**（降噪契约 15 + 汇总重刷 9 + 漂移守卫 6 + 并发锁回归 5 + Apply 补丁范围 5）；
- skipped 由 3 降为 2：`test_conftest_env_isolation.py::test_deploy_env_keys_cover_instance_env`
  的 skip 条件是「本地部署文件不存在」，本机 `config/instance.env` 存在 → 该护栏**实际执行并通过**
  （§15.7 O8 记录的护栏仍在生效）。

### 16.8 本轮结论

1. **口径变更已按用户决策落地**：安全类（注入 / 明文凭据 / 越权 / 路径穿越 / 不安全反序列化 /
   弱哈希 / 敏感信息外泄 / 文件权限过宽）与依赖环境类（未锁版本 / 多系统兼容兜底）在本场景不报；
   硬件数据类（`R-DATA` / `R-NVME`）与全部强制例外**不受影响**。
2. **真实业务两遍复验一致**：MR !8（全新、同内容）安全类 0 / 噪音 0 / 未分类 0 / 信号类 10，
   L3 **20/20**；`rule_keys` 空值由 1 → **0**。
3. **全面完整验证 2 遍签名一致**：L1 1708/0/2、E2E 21/21 + 32/32、L3 19/20 + 20/20 + 20/20，
   可复现、无偶发。
4. **发现并修掉 4 个问题**（N7–N10）：N7 是**模型侧**规则键缺失（提示词硬约束 + 真实复验）；
   N8 是**部署链路**提示词静默不生效（内容级漂移守卫 + 启动 fail-fast）；
   N9 是**并发**写 MR 标题导致 telemetry 与 GitLab 漂移（note 路径补 per-MR 锁）；
   N10 是 **E2E 的 Apply 模拟用错范围语义**（按 existing_code 跨度替换）。
   四者都**不是**静态检查或单测能发现的 —— 只有真跑检视、核对部署态、且**连跑多遍**
   （本轮实测：第 1 轮 32/32、第 2 轮 31/32 暴露 N9；N10 则取决于模型给出的建议是否
   1→N 多行替换 —— 同一份代码第 1 轮触发、第 2 轮不触发），才会暴露。
