# ReviewAgent

GitLab 代码检视（MR review）自动化平台，基于 LLM agent（qodercli / opencode）+ GitLab webhook 集成。

> 设计核心：**薄 Python 壳 + 厚 agent**。Python 端只做接入、调度、落库与代码污染防护；真正的"理解代码"全部交给 LLM agent（通过 `reviewagent/llm/` 适配层调用，默认 qodercli subprocess，可选 opencode HTTP API），`profiles/<profile>/prompts/` 里的 agent prompt 才是核心交付物。

---

## 设计哲学

| 工作 | 实现方 |
|---|---|
| webhook 鉴权、路由、立即 200、入队 | Python（薄） |
| GitLab API 调用（拉 diff / 发评论 / 解析应用） | Python（薄封装） |
| 任务队列（RQ / Redis） | Python |
| SQLite 落库（telemetry） | Python（薄 emitter） |
| git 污染防护（bare repo + tmpfs worktree） | Python |
| **diff 切片 / severity 分类 / 行号抽取 / JSON 输出** | **LLM agent（厚，via qodercli / opencode）** |
| **中文 MR 描述生成** | **LLM agent** |
| **可 Apply 的 inline 改进建议** | **LLM agent** |
| **周报聚合与生成** | **LLM agent + 薄 Python** |

Python 端不做任何代码理解工作。LLM 调用通过 `reviewagent/llm/client.py:get_client()` 统一入口，根据 `LLM_PROVIDER` 配置分发到 qodercli（subprocess）或 opencode（HTTP API）。

---

## 当前已实现功能

| 功能 | 触发方式 | 状态 |
|---|---|---|
| `/describe` | MR open / update · Note `/describe` | ✅ |
| `/improve` | Note `/improve` | ✅ |
| 自动检视链 | MR open / update（有新 commit）· Push（新 commit） | ✅ |
| 建议采纳/驳回 | 对 inline suggestion 回复 `/adopt [理由]` · `/dismiss [理由]` | ✅ |
| GitLab UI Apply 自动识别 | 用户在 UI 点 "Apply suggestion" 或 push 改写代码 | ✅ |
| Telemetry API（`/api/v1/telemetry/*`） | — | ✅ |
| 周报（JSON + Markdown，可推钉钉，含固有代码全量扫描） | 脚本 / 定时 | ✅ |
| `/review`（深度代码检视） | — | ❌ 未实现 |

> 注：`/review`（深度代码检视）尚未实现——`commands/review.py` 未编写，webhook 不识别 `/review`，`prompts/` 下亦无对应规划。

---

## 支持的命令

| 命令 | 触发场景 | 行为 |
|---|---|---|
| `/describe` | MR 评论 | 调 `pr-describer` agent，改写 MR 标题 + 中文 Description |
| `/improve` | MR 评论 | 调 `code-improver` agent，逐文件发可 Apply 的 inline 建议评论 |
| `/adopt [理由]` | 对某条 inline suggestion 的回复 | 验证代码确已改动 → 标记 adopted，resolve discussion |
| `/dismiss [理由]` | 对某条 inline suggestion 的回复 | 标记 dismissed，resolve discussion，记入 telemetry |
| （自动） | MR open / update（含新 commit）· Push | `pr_commands=describe,improve` / `push_commands=describe,improve` |

死循环防护：bot 自评论会被忽略；`MR_COOLDOWN_SECONDS=60` 限频；`MAX_REVIEW_CALLS_PER_MR` 上限（默认 30，达上限后不再自动检视，提示手动 `/improve`）。

---

## 架构

```
  ┌──────────────────────────┐          ┌──────────────────┐
  │ GitLab (MR/Push/Note)    │          │ cron / 定时脚本    │
  └────────────┬─────────────┘          └────────┬─────────┘
               │ webhook                         │ enqueue
               ▼                                 ▼
  FastAPI /webhook ── 立即 200       Queue: weekly
               │                     周报 (3×LLM)
               ▼                          │
  Queue: review                           ▼
  describe / improve / suggestion    Worker ×1 (独立)
               │                          │
               ▼                          ▼
  Worker ×N (并发)                reporting/ (3 个 collector)
       │         │                  各 1× LLM 调用
       ▼         ▼                       │
  /describe  /improve                    ▼
  1×LLM     diff→按文件切片         ┌────┴────┐
           →并行 LLM 调用          ▼         ▼
           (max_workers=3)     钉钉通知   SQLite 归档
       │         │
       ▼         ▼
  GitLab API   git worktree      LLM 适配层 (llm/)
  (拉 diff /   (bare repo +      ├── qodercli (subprocess)
   发评论)      tmpfs 隔离)      └── opencode (HTTP API)
                         │
                         ▼
              SQLite telemetry.db
              GET /api/v1/telemetry/*
```

**关键设计**:
- **两条触发链**: webhook 链 (MR review) 与定时链 (周报) 独立触发、独立队列、独立 worker，互不阻塞
- **Worker 并发**: review 队列 N 个 worker (默认 3)，多 MR 可同时处理
- **improve 并行**: 按文件切片 → ThreadPoolExecutor 并行调 LLM（qodercli subprocess 或 opencode HTTP）
- **代码防护**: bare repo + tmpfs worktree，agent 只读临时目录
- **死循环防护**: bot 自评论忽略 + cooldown + MAX_REVIEW_CALLS_PER_MR 上限

---

## 目录结构

```
reviewagent/
├── config.py              # 业务配置（frozen dataclass + 环境变量，单例 config）
├── repo_context.py        # 仓库规则动态解析（RuleNameResolver.from_repo）
├── logging_setup.py       # loguru
├── main.py                # FastAPI app（/webhook、/health、/docs、/api/v1/telemetry/*）
├── git/
│   ├── workspace.py       # bare repo + worktree + 代码污染防护
│   └── diff_lines.py      # diff 行号映射（suggestion 行号校正）
├── gitlab/client.py       # python-gitlab 薄封装
├── llm/                   # LLM 适配层（统一接口，多 provider）
│   ├── base.py            #   BaseLLMProvider + LLMResult
│   ├── client.py          #   get_client() 工厂（按 config.llm_provider 分发）
│   ├── opencode_provider.py  # opencode HTTP API provider
│   ├── qodercli_provider.py  # qodercli subprocess provider
│   ├── qodercli_subprocess.py # qodercli 进程管理 + JSON 解析
│   └── qodercli_errors.py    # 异常类定义
├── opencode/client.py     # opencode HTTP 客户端（llm/opencode_provider 底层）
├── metrics/               # Prometheus-style 计数器（/health 暴露）
│   ├── __init__.py        #   inc() / get_all() 接口
│   └── counters.py        #   计数器注册表
├── scenario/              # 场景包（profile + prompts + rules 的唯一来源）
│   ├── profile.py         #   profile.yaml 加载/校验/指纹
│   ├── prompts.py         #   prompt 加载/模板渲染（按 profile prompts 映射）
│   ├── dynamic.py         #   仓库规则动态发现（AGENTS.md → 链接规则文件）
│   └── rules/             #   RuleSet 解析、渲染、键规范化
├── commands/
│   ├── describe.py        # /describe 工作流
│   ├── improve.py         # /improve 工作流（可 Apply 的 inline 建议）
│   ├── suggestion_actions.py  # /adopt /dismiss 处理
│   └── _common.py         # 公共逻辑（LLM 调用、telemetry 落库）
├── webhook/
│   ├── auth.py            # X-Gitlab-Token 比对
│   ├── parsers.py         # payload 解析 + 命令提取
│   ├── locks.py           # bot 白名单 / cooldown / max_review_calls / applied 探测
│   └── router.py          # MR / Push / Note 三类 hook 路由
├── workers/
│   ├── tasks.py           # RQ 任务（enqueue_* / run_*）
│   └── rq_worker.py       # ReviewAgentSpawnWorker（macOS-safe）
├── telemetry/
│   ├── models.py          # 数据模型
│   ├── store.py           # SQLite 落库（WAL）
│   └── events.py
├── reporting/             # 周报（collectors / renderer / notifiers / runner）
│   ├── collectors/        #   base / telemetry / merged_mrs / repo_scan
│   ├── notifiers/         #   base / dingtalk
│   ├── artifact.py        #   WeeklyArtifact 数据契约
│   ├── renderer.py        #   markdown 渲染 + 分块
│   ├── rule_translate.py  #   规则键 → 中文翻译
│   ├── config.py          #   WeeklyReportConfig
│   └── runner.py          #   run_weekly_job 主入口
└── api/router.py          # Telemetry API 路由
```

---

## LLM Provider 迁移 (qodercli → qoder-sdk)

> **2026-09 起, 默认 LLM Provider 从 `qodercli`（subprocess）切到 `qoder-sdk`（qoder-agent-sdk）**.

### 背景

旧的 `qodercli -p` subprocess 模式 (qodercli_subprocess.py, 19k 字节) 累积了大量
JSON 解析补丁（`_strip_line_number_prefix` / `_extract_inner_json` / `_strip_fence` /
`_unwrap_markdown_wrapper`）应对 DeepSeek-V4-Flash 输出抖动；近 6 个月打 8+ 次补丁。
详见 [docs/LLM_PROVIDER_ADAPTER.md](docs/LLM_PROVIDER_ADAPTER.md) 的 `migration` 段。

`qoder-agent-sdk` (qoder 官方 Python SDK) 直接提供 `query()` 异步迭代 + 结构化
`ResultMessage`，干掉全部解析补丁；真 token / 真 credits / 真实 context 占比一并
带回。

### 切换步骤

1. **装包**：`pip install qoder-agent-sdk>=1.0.0`（已加 `pyproject.toml`）；
   国内站点改用 CN 品牌包 `pip install -e ".[qoder-cn]"`（`QODER_SDK_EDITION=cn`）
2. **改 env**（`LLM_PROVIDER=qoder-sdk` + `QODER_SDK_PAT=...`）:
   ```bash
   # 必填 (PAT 替代 qodercli login 状态)
   QODER_SDK_PAT=pt-44qo...
   LLM_PROVIDER=qoder-sdk
   # 可选 — 留空走默认值
   QODER_SDK_CLI_PATH=                 # 空 → PATH 找 qodercli（cn 站点找 qoderclicn）
   QODER_SDK_MODEL=                    # 空 → QODERCLI_MODEL (默认 Lite；模型名按站点取值)
   QODER_SDK_FALLBACK_MODEL=           # 主模型失败时切换
   QODER_SDK_TIMEOUT=600               # 单次 query 超时 (秒)
   QODER_SDK_MAX_TURNS=0               # 0=不传
   QODER_SDK_PERMISSION_MODE=          # default/acceptEdits/plan/bypassPermissions/yolo/dontAsk/auto
   QODER_SDK_DISALLOWED_TOOLS=write,edit,bash,webfetch,websearch
   ```
3. **无需改任何业务代码**：`commands/describe.py` / `commands/improve.py` / `workers/tasks.py` / `reporting/collectors/*.py` **零改动**，全部走 `get_client().run(...)` 抽象。

### 字段映射（subprocess → SDK）

| subprocess 模式 (`qodercli -p ...`) | SDK 模式 (`QoderAgentOptions`) |
|---|---|
| `--model Lite` | `options.model = "Lite"` |
| `--append-system-prompt {agent.md 内容}` | `options.system_prompt = loader.load(agent)["prompt"]` |
| `--disallowed-tools write,edit,bash,webfetch,websearch` | `options.disallowed_tools = [...]` |
| `-w {workdir}` | `options.cwd = str(workdir)` |
| `--attachment {tmp_diff}` | `options.add_dirs = [workdir]` (diff.patch 已在 workdir) |
| `--no-session-persistence` | SDK 默认即无持久化，无需传 |
| `proc.returncode` + `proc.stderr` 错误判断 | `ResultMessage.subtype` + `is_error` + `errors` |
| `usage.context_usage_ratio` 代理 | `AssistantMessage.usage.context_usage_ratio` 真值 |
| `total_credits` 兜底 | `ResultMessage.total_credits` + `model_usage` |
| 拿不到 `input_tokens` (dfmodel) | `AssistantMessage.usage.input_tokens` 真值 + `cache_creation_input_tokens` / `cache_read_input_tokens` |

### 错误处理架构

**SDK 11 类异常 → ReviewAgent 3 类异常**, 完整映射在 [`qoder_sdk_provider._map_sdk_exception`](reviewagent/llm/qoder_sdk_provider.py):

| SDK 异常 | ReviewAgent 异常 |
|---|---|
| `CLIJSONDecodeError` | `QoderCLIOutputError` (JSON 解析失败) |
| `ModelPolicyTimeoutError` | `QoderCLITimeoutError` (模型策略 callback 超时) |
| `CLINotFoundError` / `ProcessError` / `CLIConnectionError` | `QoderCLIError` (qodercli 进程/连接问题) |
| `AuthNotConfiguredError` / `AuthAccessTokenEnvVarError` / `AuthServiceAccountEnvVarError` | `QoderCLIError` (鉴权失败) |
| `CloudAgentApiError` / `CloudAgentUnsupportedAuthError` | `QoderCLIError` (云端 agent 错误) |
| `UnsupportedCliCapabilityError` | `QoderCLIError` (qodercli 版本太旧) |
| 任何 `QoderSDKError` 子类 (含未列出的) | `QoderCLIError` (catchall) |
| `asyncio.TimeoutError` | `QoderCLITimeoutError` |

**ResultMessage 错误细分**:
- `subtype=error_during_execution` + `is_error=True` → **直接 raise** `QoderCLIError` (上层不会拿到半截结果)
- `subtype=error_max_turns` + `is_error=True` → **log warning, 返回部分数据** (agent 跑满 max_turns 但仍产出部分 JSON, 上层拿到能解析就用)
- `subtype=success` + `is_error=False` → **正常返回** `LLMResult`

**上层 catch 块不变** (`BaseCommand.run`): `except (QoderCLITimeoutError, QoderCLIOutputError, QoderCLIError)` → 包成 `BaseCommandError`。

### 兜底 / 回退

SDK 路径出问题时, **不需要改代码**, env 切回旧路径即可:
```bash
LLM_PROVIDER=qoder-cli  # 旧 subprocess 路径, 完整保留未动
```
两条路径共享同一个 `BaseLLMProvider` 接口 + `LLMResult` 数据结构, 业务代码零感知.

### 验证状态

- **399 单元测试通过** (含 22 个新加的 SDK 错误传播测试, `tests/test_qoder_sdk_provider_errors.py`)
- **E2E 验证**: 真实 GitLab MR 318/319 (`http://127.0.0.1:8929/root/auto-review-test`) 全跑通:
  - `/describe` 21s 改标题 + 描述, credits 0.0352
  - `/improve` 103s 发 2 条 inline DiffNote + 顶部总览 + 检视汇总表, credits 0.1263
- **3 个 pre-existing 失败** (`test_telemetry_section_render.py` 3 个) 在 main 上也失败, 与本次迁移无关
- **6 个集成测试超时** (`test_auto_detect_*` / `test_dedup_*` / `test_last_activity_at` / `test_webhook_diff_head_lock`) 需真实 GitLab/Redis 服务, 本机跑不完


## 快速开始

服务器部署（Linux + systemd，一键部署并设置开机自启）：

```bash
# 首次：生成 config/instance.env（600）后退出，填入 GitLab URL / PAT / Webhook Secret
sudo bash deploy.sh
sudo bash deploy.sh          # 再执行一次：装依赖、起 Redis、装 unit、enable、跑验证
sudo bash deploy.sh --check  # 只做前置检查（不修改系统）
```

`deploy.sh` 完成后所有 unit 均已 `systemctl enable`：webhook、`reviewagent-worker@1..RQ_WORKER_COUNT`、
weekly-worker 常驻，weekly.timer 与 reconciler.timer 进入 `timers.target`，重启机器自动拉起。
`deploy/preflight.sh` 与 `deploy/verify.sh` 可单独运行做环境体检与部署后复检。

- 服务器部署细节 → 见 [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md)
- 快速启动（本地开发 / 运维） → 见 [`docs/QUICKSTART.md`](docs/QUICKSTART.md)
- 生效配置与两个指纹 → 见 [`docs/CONFIG.md`](docs/CONFIG.md)（由 `reviewagent config show` 生成）
- 规则包模板（verbatim / structured） → 见 [`docs/RULES.md`](docs/RULES.md)
- 备份 / 恢复 / drain → 见 [`docs/BACKUP_RESTORE.md`](docs/BACKUP_RESTORE.md)
- LLM Provider 适配层设计 → 见 [`docs/LLM_PROVIDER_ADAPTER.md`](docs/LLM_PROVIDER_ADAPTER.md)

---

## 配置

配置分三个平面（**唯一默认值来源**是 [`reviewagent/settings/schema.py`](reviewagent/settings/schema.py)，
env 只在 [`reviewagent/settings/binder.py`](reviewagent/settings/binder.py) 里解析）。
用户改配置只需要看 [`config/`](config/README.md)：

| 平面 | 载体 | 说明 |
|---|---|---|
| 实例平面 | `config/instance.env`（模板 `config/examples/instance.env.example`） | 本机部署参数（GitLab / LLM / Redis / data_dir / profile / 日志 / 周报投递），只写非默认值。**只有 GITLAB_* 三项必填** |
| 场景平面 | `profiles/<name>/`（`REVIEW_PROFILE` 选择）+ `config/profiles/<name>/review.yaml`（用户调参） | prompts / 规则引用由场景包提供；检视范围 / 打分 / 限额 / 上下文在 review.yaml 覆盖；切换场景 = 改 `REVIEW_PROFILE` + 新 data_dir + 同时重启 API 与 worker |
| 规则平面 | 规则包 `.md`（profile 的 `rules:`） | 键命名空间、verbatim/structured 渲染、`reviewagent rules check` 校验 |

启动前校验与人工核对：

```bash
.venv/bin/python -m reviewagent config check --profile ssd-python-autotest   # 缺必填/绑定冲突/值越界 → 退出码 2
.venv/bin/python -m reviewagent config show --diff-default --sources          # 只看与默认不同 + 来源
.venv/bin/python -m reviewagent rules check --profile ssd-python-autotest     # 规则索引 + ruleset_fingerprint
```

完整字段清单与来源见 [`docs/CONFIG.md`](docs/CONFIG.md)（生成文件）。主要分组：

| 分组 | 变量 | 说明 |
|---|---|---|
| GitLab | `GITLAB_URL` `GITLAB_PERSONAL_ACCESS_TOKEN` `GITLAB_WEBHOOK_SECRET` `GITLAB_BOT_USERNAME` | 必填；PAT 最低 `api` scope |
| LLM | `LLM_PROVIDER` `OPENCODE_URL` `OPENCODE_MODEL` `OPENCODE_USERNAME` `OPENCODE_PASSWORD` `OPENCODE_TIMEOUT` | **`qoder-sdk`（推荐）** / `qodercli`（旧 subprocess 兜底） / `opencode`（HTTP API 兜底）；详见 [LLM Provider 迁移](#llm-provider-迁移-qodercli--qoder-sdk) |
| QoderSDK | `QODER_SDK_PAT` `QODER_SDK_CLI_PATH` `QODER_SDK_MODEL` `QODER_SDK_FALLBACK_MODEL` `QODER_SDK_TIMEOUT` `QODER_SDK_MAX_TURNS` `QODER_SDK_PERMISSION_MODE` `QODER_SDK_DISALLOWED_TOOLS` | **LLM_PROVIDER=qoder-sdk 时必填**；`QODER_SDK_PAT` 替换 `qodercli login` 状态；`QODER_SDK_CLI_PATH` 留空走 PATH，配置非空则严格校验；`QODER_SDK_MODEL` 默认 `Lite`（调试）/ `QODERCLI_MODEL`（生产） |
| QoderCLI（旧） | `QODERCLI_NODE_PATH` `QODERCLI_JS_PATH` `QODERCLI_MODEL` `QODERCLI_TIMEOUT` `QODERCLI_MAX_TURNS` `QODERCLI_PERMISSION_MODE` `QODERCLI_FALLBACK_MODEL` | 仅 `LLM_PROVIDER=qodercli` 旧 subprocess 路径用，留作兜底；切到 qoder-sdk 后此组可忽略 |
| Redis/RQ | `REDIS_URL` `RQ_QUEUE_NAME` `RQ_WEEKLY_QUEUE_NAME` `RQ_WORKER_TIMEOUT` `RQ_WORKER_COUNT` | 队列名默认 `review`（周报队列 `review-weekly`）；`RQ_WEEKLY_QUEUE_NAME` 默认 `{RQ_QUEUE_NAME}-weekly`；`RQ_WORKER_COUNT` 控制本地并发 worker 数。`RQ_WORKER_CLASS` 是 `rq` CLI 运维键（不在 schema），写进 systemd unit，见 `docs/DEPLOYMENT.md` |
| 实例 | `REVIEW_PROFILE` `REVIEWAGENT_DATA_DIR` `REVIEWAGENT_LOG_LEVEL` `LOG_FORMAT` `LOG_ROTATION` | profile 名 / 数据目录 / 日志级别 / 日志格式（`text`/`json`）/ 轮转（`single`/`none`）；data_dir ↔ profile 绑定（`instance.json`） |
| 限制 | `MR_COOLDOWN_SECONDS` `MAX_REVIEW_CALLS_PER_MR` `MAX_DIFF_CHARS` `OPENCODE_MAX_DIFF_CHARS` | 防循环 / 超大 diff 跳过（场景平面，profile 可覆盖） |
| 仓库规则 | `REPO_CONTEXT_FILES` `REPO_CONTEXT_RULES_DIR` `RULE_KEY_PREFIX` `REPO_CONTEXT_MAX_LINES` | 从目标仓库默认分支读入口文件（默认 `AGENTS.md`）并跟随其中 Markdown 链接发现规则文件；`REPO_CONTEXT_RULES_DIR` 为空=自动发现、非空=显式目录覆盖；`RULE_KEY_PREFIX=auto` 识别任意 `<大写前缀>-RULE-<键>`；`REPO_CONTEXT_MAX_LINES` 渲染行数预算（默认 2000） |
| improve | `IMPROVE_PARALLEL_WORKERS` `IMPROVE_FULL_FILES` `IMPROVE_MIN_SCORE` `IMPROVE_MIN_SEVERITY` `IMPROVE_REVIEW_MODE` `IMPROVE_KEYWORD_PATHS` `IMPROVE_SKIP_TEST_PATHS` `IMPROVE_PARTIAL_CONTEXT_LINES` `IMPROVE_PATCH_CONTEXT_LINES` `IMPROVE_MAX_SUGGESTIONS` `IMPROVE_MAX_SUGGESTIONS_PER_FILE` `IMPROVE_MIN_SUGGESTIONS_PER_FILE` `IMPROVE_PRIORITY_WEIGHT_DIFF` `IMPROVE_PRIORITY_WEIGHT_KEYWORD` `IMPROVE_PRIORITY_WEIGHT_DENSITY` `IMPROVE_PRIORITY_WEIGHT_TEST_FEATURE` `IMPROVE_MAX_FILES_TO_REVIEW` `IMPROVE_NEW_FILE_FULL` `IMPROVE_TEST_SAMPLE_PATHS` `IMPROVE_TEST_SAMPLE_MAX` | 并行度 / 限流 / 模式 / priority 权重（场景平面，profile 提供默认）；V9 增量复用按文件内容指纹(blob sha)，选优按 (severity,score) 复合键 + priority 加权预算 + 覆盖优先；文件筛选护栏：`MAX_FILES_TO_REVIEW` 送 LLM 文件数硬上限(必保新增+关键路径)、`NEW_FILE_FULL` 新增文件优先(加分+豁免截断)、`TEST_SAMPLE_PATHS`/`TEST_SAMPLE_MAX` 自动 case 抽样 |
| 检视过滤 | `REVIEW_EXCLUDE_EXTENSIONS` | 不送审的文件扩展名（默认含 `.md`/`.txt`/图片等） |
| 周报-总开关 | `REVIEWAGENT_WEEKLY_ENABLED` `REVIEWAGENT_WEEKLY_TARGET_PROJECT_ID` `REVIEWAGENT_WEEKLY_TARGET_BRANCH` `REVIEWAGENT_WEEKLY_TIMEZONE` | 总开关（默认 true）；目标项目 id（0=跳过 merged_mrs/repo_scan 段）/ 分支（默认 `main`）/ 时区（默认 `Asia/Shanghai`） |
| 周报-采集 | `REVIEWAGENT_WEEKLY_COLLECTORS` `REVIEWAGENT_WEEKLY_NOTIFIER` | 启用的采集段（默认 `telemetry,merged_mrs,repo_scan`）；通知器（默认 `dingtalk`） |
| 周报-调度 | `REVIEWAGENT_WEEKLY_CRON_SCHEDULE` | cron 触发时间，`OnCalendar` 格式（默认 `Mon 10:30`；由 systemd timer 或外部 cron 调用） |
| 周报-钉钉 | `REVIEWAGENT_WEEKLY_DASHBOARD_URL` `REVIEWAGENT_WEEKLY_DINGTALK_WEBHOOK_URL` `REVIEWAGENT_WEEKLY_DINGTALK_SECRET` `REVIEWAGENT_WEEKLY_DINGTALK_DRY_RUN` `REVIEWAGENT_WEEKLY_DINGTALK_RETRY` `REVIEWAGENT_WEEKLY_MD_CHUNK_LIMIT` `DINGTALK_WEBHOOK` | 看板地址（渲染到「本周检视概况」末尾，空=不显示）；webhook URL（命名前缀版为准，`DINGTALK_WEBHOOK` 为 schema 别名）；加签 secret；`DRY_RUN` 默认 true（只 log 不推送）；重试次数（默认 3）；Markdown 分块上限（默认 18000） |
| 周报-标题 | `REVIEWAGENT_WEEKLY_REPORT_TITLE` `REVIEWAGENT_WEEKLY_REPORT_EMOJI` | 标题（空 = 用 `profile.reporting.weekly_title`，内置场景为 `SSD自动化代码检视周报`）/ emoji（默认 📊） |

> 场景平面字段（限制 / 仓库规则 / improve / 检视过滤）优先在
> `config/profiles/<name>/review.yaml` 里调整，环境变量只作临时调试；
> 实例平面字段放 `config/instance.env`。完整说明见 [`config/README.md`](config/README.md)。

## 端到端验证

```bash
# 快速档：进程内跑完整配置生效性审计（92 字段 witness + 21 组行为 delta），无需外部服务
.venv/bin/python scripts/e2e/run_e2e.py --tier fast

# 全量档：临时 GitLab 项目/分支/MR + 真实 LLM/队列，跑 8 个功能并按真实产物校验配置
.venv/bin/python scripts/e2e/run_e2e.py --tier full --round 1
```

全量档除真实 suggestions 外，还会用 MR `/changes` 与 51 文件抽样分支的 compare
结果直接跑 `planner.plan()`，逐条验证 `review_mode` / `priority_paths` /
`full_files` / `test_sample_*`，并从仓库默认分支真实拉取 `AGENTS.md` 与 rules_dir
（`scripts/e2e/real_config.py`）。

两档共用同一份 witness 目录（`scripts/e2e/catalog.py`），报告写到
`logs/e2e/report-<时间戳>.json`；`--only describe,improve` 可只跑部分功能，
`--keep` 保留临时 GitLab 项目，`scripts/e2e/verify_e2e.py` 是全量档兼容入口。

---

## 当前状态与路线图

### 已完成
- Phase 1 全套：骨架、污染防护、LLM 客户端、webhook 接入、RQ 任务、GitLab 客户端、`/describe` 端到端。
- `/improve` + 可 Apply 的 inline suggestion（`/adopt` `/dismiss` + GitLab UI Apply 自动识别）。
- LLM Provider 适配层（`reviewagent/llm/`）：**qoder-sdk（qoder-agent-sdk，默认推荐）** + qodercli subprocess（旧路径兜底） + opencode HTTP API, 配置一键切换; SDK 异常完整映射到 3 个 ReviewAgent 异常类, 上层业务代码零改动.
- Telemetry API（`/api/v1/telemetry/*`：health / runs / mr / suggestions / stats / timeline / metrics / dismissals / weekly-reports）。
- 周报生成（JSON + MD，钉钉推送支持，默认 dry_run）。三段 LLM 调用。
- 跨次建议去重（fingerprint）+ 跨文件引用分析 + 评分过滤。

### 计划 / 进行中
- **`/review` 命令**：深度代码检视，目前尚未实现（无 prompt 规划、无命令实现）。
- **多项目扩展**：当前按 project 维度接，后续计划批量接入 + per-project 配置。
- **监控告警**：webhook 5xx / token 超限 / 服务下线。

### 已知限制
- LLM 调用为同步阻塞，单任务耗时 = 模型推理时间（实测 `/describe` ~25s，`/improve` 可达数分钟）。
- diff 过大（> `MAX_DIFF_CHARS`）会跳过检视；prompt 内联 diff 截断到 `OPENCODE_MAX_DIFF_CHARS`，超出触发一次减半重试。
- ~~qodercli subprocess 模式每次调用启动新进程，有约 1-2s 启动开销（可接受）。~~
- **已迁移到 qoder-sdk**：每次 `query()` 走 SDK 协议，免去 subprocess 启动开销；JSON 解析补丁全部干掉（19k → 1k 行）；真 token / 真 credits / 真实 context 占比可用。

---

## 安全与维护待办

1. **历史密钥轮换**：启动脚本已统一从 `config/instance.env` 读取凭据；Git 历史中出现过的旧 secret 仍需保持废弃状态。
2. **监控告警**：webhook 5xx / 服务下线告警。
3. **多项目扩展**（见路线图）。

---

## 相关文档

- [`docs/QUICKSTART.md`](docs/QUICKSTART.md) — 本地开发 / 服务器运维
- [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) — 服务器部署、切换场景与升级顺序
- [`docs/CONFIG.md`](docs/CONFIG.md) — 生效配置（生成）与 `profile_fingerprint` / `config_fingerprint` 说明
- [`docs/RULES.md`](docs/RULES.md) — 规则包最小模板（verbatim / structured）与 `rules check`
- [`docs/BACKUP_RESTORE.md`](docs/BACKUP_RESTORE.md) — `VACUUM INTO` 备份、drain、恢复校验
- [`CHANGELOG.md`](CHANGELOG.md) — 用户可见变更与 Known limits
- [`docs/LLM_PROVIDER_ADAPTER.md`](docs/LLM_PROVIDER_ADAPTER.md) — LLM Provider 适配层设计与验证
- [本 README 的 LLM Provider 迁移章节](#llm-provider-迁移-qodercli--qoder-sdk) — qodercli → qoder-sdk 切换指南 / 字段映射 / 错误处理
