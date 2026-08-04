# ReviewAgent

GitLab 代码检视（MR review）自动化平台，基于 [opencode](https://opencode.ai) agent + GitLab webhook 集成。

> 设计核心：**薄 Python 壳 + 厚 agent**。Python 端只做接入、调度、落库与代码污染防护；真正的"理解代码"全部交给 opencode agent（HTTP API），`reviewagent/prompts/*.md` 里的 agent prompt 才是核心交付物。

---

## 设计哲学

| 工作 | 实现方 |
|---|---|
| webhook 鉴权、路由、立即 200、入队 | Python（薄） |
| GitLab API 调用（拉 diff / 发评论 / 解析应用） | Python（薄封装） |
| 任务队列（RQ / Redis） | Python |
| SQLite 落库（telemetry） | Python（薄 emitter） |
| git 污染防护（bare repo + tmpfs worktree） | Python |
| **diff 切片 / severity 分类 / 行号抽取 / JSON 输出** | **opencode agent（厚）** |
| **中文 MR 描述生成** | **opencode agent** |
| **可 Apply 的 inline 改进建议** | **opencode agent** |
| **周报聚合与生成** | **opencode agent + 薄 Python** |

Python 端不做任何代码理解工作。

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
| 周报（JSON + Markdown + XLSX，可推钉钉，含固有代码全量扫描） | 脚本 / 定时 | ✅ |
| `/review`（深度代码检视） | — | ❌ 未实现 |

> 注：`prompts/` 里已为 `/review` 预留规划，但 `commands/review.py` 尚未编写，webhook 也不识别 `/review`。

---

## 支持的命令

| 命令 | 触发场景 | 行为 |
|---|---|---|
| `/describe` | MR 评论 | 调 `pr-describer` agent，改写 MR 标题 + 中文 Description |
| `/improve` | MR 评论 | 调 `code-improver` agent，逐文件发可 Apply 的 inline 建议评论 |
| `/adopt [理由]` | 对某条 inline suggestion 的回复 | 验证代码确已改动 → 标记 adopted，resolve discussion |
| `/dismiss [理由]` | 对某条 inline suggestion 的回复 | 标记 dismissed，resolve discussion，记入 telemetry |
| （自动） | MR open / update（含新 commit）· Push | 按 `pr_commands` / `push_commands` 串行跑 `describe → improve` |

死循环防护：bot 自评论会被忽略；`MR_COOLDOWN_SECONDS` 限频；`MAX_REVIEW_CALLS_PER_MR` 上限（默认 30，达上限后不再自动检视，提示手动 `/improve`）。

---

## 架构

```
GitLab (MR / Push / Note)
   │  webhook (X-Gitlab-Token 鉴权)
   ▼
FastAPI /webhook  ── 立即 200 ──► RQ 队列 (Redis)
   │                                  │
   │                            RQ Worker (review / review-v2)
   │                                  │  ← 只处理 improve/describe/suggestion
   │                  ┌───────────────┼───────────────────┐
   │                  ▼               ▼                   ▼
   │           GitLab API        git worktree        opencode serve (:4096)
   │         (diff / comment)   (bare repo)         (HTTP API: session+message)
   │                  │               │                   │
   │                  └───────────────┴───────────────────┘
   │                          SQLite telemetry.db ◄── 落库
   ▼
GET /api/v1/telemetry/*  ── 看板 / 统计

周报（含 opencode LLM 变更摘要 / 质量扫描）走**独立队列 `review-weekly`（`review-v2-weekly`）**
+ **独立 worker 进程**，与主 review 队列物理隔离：周报 job 含三次 LLM 调用（三小节各一次）、可能跑数分钟，
不会阻塞 improve/describe。Redis / opencode / 模型 / SQLite 等底层资源全部共享（"共有资源"）。
```

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
├── opencode/client.py     # HTTP API（POST /session + /session/:id/message）
├── prompts/               # agent prompt（Markdown frontmatter；核心交付物）
│   ├── describe.md        #   pr-describer（MR 标题 + 中文描述）
│   ├── improve.md         #   /improve 命令编排 prompt
│   ├── improve_agent.md   #   code-improver（opencode primary agent，发 inline 建议）
│   ├── weekly_inspection_summary.md  # 周报一：本周检视概况叙事（LLM）
│   ├── weekly_change_summary.md      # 周报二：main 变更汇总（LLM）
│   ├── weekly_quality_scan.md        # 周报三：代码质量全量扫描，含固有代码评估（LLM）
│   ├── _general_rules_block.md        # 可复用规则 block
│   ├── loader.py          # prompt 加载器（frontmatter 解析）
│   └── __init__.py
├── commands/
│   ├── describe.py        # /describe 工作流
│   ├── improve.py         # /improve 工作流（可 Apply 的 inline 建议）
│   ├── suggestion_actions.py  # /adopt /dismiss 处理
│   └── _common.py
├── webhook/
│   ├── auth.py            # X-Gitlab-Token 比对
│   ├── parsers.py         # payload 解析 + 命令提取
│   ├── locks.py           # bot 白名单 / cooldown / max_review_calls / applied 探测
│   └── router.py          # MR / Push / Note 三类 hook 路由
├── workers/tasks.py       # RQ 任务（enqueue_* / run_*）
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

## 快速开始

- 本地 macOS 开发 / 运维 → 见 [`docs/QUICKSTART.md`](docs/QUICKSTART.md)
- 86 服务器生产部署（含全部踩坑） → 见 [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md)
- 本地 v2 环境（与 pr-agent v1 隔离的那套） → 见 [`docs/V2_ENVIRONMENT.md`](docs/V2_ENVIRONMENT.md)

---

## 配置

单一 `.env`（不入 git，模板见 `.env.example`）。关键变量：

| 分组 | 变量 | 说明 |
|---|---|---|
| GitLab | `GITLAB_URL` `GITLAB_PERSONAL_ACCESS_TOKEN` `GITLAB_WEBHOOK_SECRET` `GITLAB_BOT_USERNAME` | 必填；PAT 最低 `api` scope |
| opencode | `OPENCODE_URL` `OPENCODE_MODEL` `OPENCODE_USERNAME` `OPENCODE_PASSWORD` `OPENCODE_TIMEOUT` | 默认 `http://localhost:4096`，模型 `deepseek/deepseek-v4-flash`；`OPENCODE_TIMEOUT` 默认 900s |
| Redis/RQ | `REDIS_URL` `RQ_QUEUE_NAME` `RQ_WEEKLY_QUEUE_NAME` `RQ_WORKER_TIMEOUT` `RQ_WORKER_COUNT` `RQ_WORKER_CLASS` | 队列名两环境不同（`review`/`review-v2`）；`RQ_WEEKLY_QUEUE_NAME` 默认 `{RQ_QUEUE_NAME}-weekly`；`RQ_WORKER_COUNT` 控制本地并发 worker 数；macOS 使用 `ReviewAgentSpawnWorker` 避免 RQ fork crash |
| 存储 | `REVIEWAGENT_DATA_DIR` `REVIEWAGENT_LOG_LEVEL` | 默认 `./data` |
| 限制 | `MR_COOLDOWN_SECONDS` `MAX_REVIEW_CALLS_PER_MR` `MAX_DIFF_CHARS` `OPENCODE_MAX_DIFF_CHARS` | 防循环 / 超大 diff 跳过 |
| 仓库规则 | `REPO_CONTEXT_FILES` `REPO_CONTEXT_RULES_DIR` `RULE_KEY_PREFIX` `REPO_CONTEXT_MAX_LINES` | 从目标仓库读 `AGENTS.md` / `.agents/rules/*.md`；`REPO_CONTEXT_MAX_LINES` 规则文件最大行数（默认 2000） |
| improve | `IMPROVE_PARALLEL_WORKERS` `IMPROVE_MAX_FILES` `IMPROVE_MAX_SUGGESTIONS` `IMPROVE_MIN_SCORE` | 并行度 / 限流 |
| 检视过滤 | `REVIEW_EXCLUDE_EXTENSIONS` | 不送审的文件扩展名（默认含 `.md`/`.txt`/图片等） |
| 周报-总开关 | `REVIEWAGENT_WEEKLY_ENABLED` `REVIEWAGENT_WEEKLY_TARGET_PROJECT_ID` `REVIEWAGENT_WEEKLY_TARGET_BRANCH` `REVIEWAGENT_WEEKLY_TIMEZONE` | 总开关（默认 true）；目标项目 id（0=跳过 merged_mrs/repo_scan 段）/ 分支（默认 `main`）/ 时区（默认 `Asia/Shanghai`） |
| 周报-采集 | `REVIEWAGENT_WEEKLY_COLLECTORS` `REVIEWAGENT_WEEKLY_NOTIFIER` | 启用的采集段（默认 `telemetry,merged_mrs,repo_scan`）；通知器（默认 `dingtalk`） |
| 周报-调度 | `REVIEWAGENT_WEEKLY_CRON_SCHEDULE` | cron 触发时间，`OnCalendar` 格式（默认 `Mon 09:00`；当前无 systemd timer，由 `scripts/run_weekly_report.sh` 或外部 cron 调用） |
| 周报-钉钉 | `REVIEWAGENT_WEEKLY_DINGTALK_WEBHOOK_URL` `REVIEWAGENT_WEEKLY_DINGTALK_SECRET` `REVIEWAGENT_WEEKLY_DINGTALK_DRY_RUN` `REVIEWAGENT_WEEKLY_DINGTALK_RETRY` `REVIEWAGENT_WEEKLY_MD_CHUNK_LIMIT` `DINGTALK_WEBHOOK` | webhook URL（命名前缀版为准，`DINGTALK_WEBHOOK` 为兼容回退）；加签 secret；`DRY_RUN` 默认 true（只 log 不推送）；重试次数（默认 3）；Markdown 分块上限（默认 18000） |
| 周报-标题 | `REVIEWAGENT_WEEKLY_REPORT_TITLE` `REVIEWAGENT_WEEKLY_REPORT_EMOJI` | 周报标题（默认 `SSD自动化代码检视周报`）/ emoji（默认 📊） |

---

## 当前状态与路线图

### 已完成（截至 2026-08-02）
- Phase 1 全套：骨架、污染防护、opencode HTTP 客户端、webhook 接入、RQ 任务、GitLab 客户端、`/describe` 端到端。
- `/improve` + 可 Apply 的 inline suggestion（`/adopt` `/dismiss` + GitLab UI Apply 自动识别）。
- Telemetry API（`/api/v1/telemetry/*`：health / runs / mr / suggestions / stats / timeline / metrics / dismissals / weekly-reports）。
- 周报生成（JSON + MD + XLSX，钉钉推送支持，默认 dry_run）。**三个小节均由 opencode agent 生成、各一次 LLM 调用**：`weekly_inspection_summary`（本周检视概况叙事）、`weekly_change_summary`（main 变更主题归纳）、`weekly_quality_scan`（代码质量全量扫描，含**固有代码全局评估**，不限制规则命中），prompt 见 `reviewagent/prompts/`。上线前需先 `python scripts/sync_agents.py` 同步 agent 并**重启 opencode serve** 使其加载；LLM 失败时自动回退到确定性拼装，周报不崩。cron 可用 `--enqueue`（`WEEKLY_ENQUEUE=true`）把整份周报作为 RQ job 入队，由 worker 异步执行（含三次 LLM 调用）。

### 计划 / 进行中
- **`/review` 命令**：深度代码检视（设计见 prompts 规划），目前尚未实现 `commands/review.py`。
- **多项目扩展**：当前按 project 维度接，Phase 5 计划批量接入 + per-project 配置。
- **监控告警**：webhook 5xx / token 超限 / 服务下线。

### 已知限制
- opencode 调用为同步阻塞，单任务耗时 = 模型推理时间（实测 `/describe` ~25s，`/improve` 可达数分钟）。
- diff 过大（> `MAX_DIFF_CHARS`）会跳过检视；opencode prompt 内联 diff 截断到 `OPENCODE_MAX_DIFF_CHARS`，超出触发一次减半重试。
- 86 服务器上 opencode 用 `setsid` 后台启动，**未进 systemd，机器重启会丢**。

---

## 安全与维护待办

1. **历史密钥轮换**：启动脚本已统一从 `.env` 读取凭据；Git 历史和 `docs/DEPLOYMENT.md` 中出现过的旧 secret 仍需保持废弃状态。
2. **opencode 接入 systemd**（86 服务器），避免重启丢失。
3. **监控告警**与上一步一并考虑。
4. **多项目扩展**（见路线图）。

---

## 相关文档

- [`docs/QUICKSTART.md`](docs/QUICKSTART.md) — 本地开发 / 86 运维 / 重建
- [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) — 86 服务器部署全记录（含踩坑）
- [`docs/V2_ENVIRONMENT.md`](docs/V2_ENVIRONMENT.md) — 本地 v2 环境（与 pr-agent v1 隔离）
