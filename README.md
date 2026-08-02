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
| 周报（JSON + Markdown + XLSX，可推钉钉） | 脚本 / 定时 | ✅ |
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
   │                                  │
   │                  ┌───────────────┼───────────────────┐
   │                  ▼               ▼                   ▼
   │           GitLab API        git worktree        opencode serve (:4096)
   │         (diff / comment)   (bare repo)         (HTTP API: session+message)
   │                  │               │                   │
   │                  └───────────────┴───────────────────┘
   │                          SQLite telemetry.db ◄── 落库
   ▼
GET /api/v1/telemetry/*  ── 看板 / 统计
```

---

## 目录结构

```
reviewagent/
├── config.py              # 业务配置（frozen dataclass + 环境变量，单例 config）
├── logging_setup.py       # loguru
├── main.py                # FastAPI app（/webhook、/health、/docs、/api/v1/telemetry/*）
├── git/
│   ├── workspace.py       # bare repo + worktree + 代码污染防护
│   └── diff_lines.py      # diff 行号映射（suggestion 行号校正）
├── gitlab/client.py       # python-gitlab 薄封装
├── opencode/client.py     # subprocess 改 HTTP API（POST /session + /session/:id/message）
├── prompts/               # agent prompt（Markdown frontmatter；核心交付物）
│   ├── describe.md        #   pr-describer
│   ├── improve.md         #   code-improver
│   └── _general_rules_block.md
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
│   ├── collectors/        #   telemetry / merged_mrs / repo_scan
│   ├── notifiers/         #   dingtalk
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
| opencode | `OPENCODE_URL` `OPENCODE_MODEL` `OPENCODE_USERNAME` `OPENCODE_PASSWORD` `OPENCODE_TIMEOUT` | 默认 `http://localhost:4096`，模型 `minimax/MiniMax-M2.7` |
| Redis/RQ | `REDIS_URL` `RQ_QUEUE_NAME` `RQ_WORKER_TIMEOUT` | 队列名两环境不同（`review` / `review-v2`） |
| 存储 | `REVIEWAGENT_DATA_DIR` `REVIEWAGENT_LOG_LEVEL` | 默认 `./data` |
| 限制 | `MR_COOLDOWN_SECONDS` `MAX_REVIEW_CALLS_PER_MR` `MAX_DIFF_CHARS` `OPENCODE_MAX_DIFF_CHARS` | 防循环 / 超大 diff 跳过 |
| 仓库规则 | `REPO_CONTEXT_FILES` `REPO_CONTEXT_RULES_DIR` `RULE_KEY_PREFIX` | 从目标仓库读 `AGENTS.md` / `.agents/rules/*.md` |
| improve | `IMPROVE_PARALLEL_WORKERS` `IMPROVE_MAX_FILES` `IMPROVE_MAX_SUGGESTIONS` `IMPROVE_MIN_SCORE` | 并行度 / 限流 |
| 周报 | `REVIEWAGENT_WEEKLY_*` `DINGTALK_*` | 见 `reviewagent/reporting/config.py` |

---

## 当前状态与路线图

### 已完成（截至 2026-08-02）
- Phase 1 全套：骨架、污染防护、opencode HTTP 客户端、webhook 接入、RQ 任务、GitLab 客户端、`/describe` 端到端。
- `/improve` + 可 Apply 的 inline suggestion（`/adopt` `/dismiss` + GitLab UI Apply 自动识别）。
- Telemetry API（`/api/v1/telemetry/*`：health / runs / mr / suggestions / stats / timeline / metrics / dismissals / weekly-reports）。
- 周报生成（JSON + MD + XLSX，钉钉推送支持，默认 dry_run）。

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

1. **密钥外置（生产前必做）**：`scripts/run_webhook.sh` 与 `scripts/run_worker.sh` 当前**硬编码了真实 GitLab PAT 与 webhook secret**（明文）。应改为从 `.env` 读取（参考 `scripts/run_weekly_report.sh` 的 `source .env` 做法），并把仓库里的明文清掉、轮换密钥。
   - 另：`docs/DEPLOYMENT.md` 历史里记录的旧 secret（DeepSeek key、`glpat-...`、`414d0c...`）均已废弃，需轮换。
2. **opencode 接入 systemd**（86 服务器），避免重启丢失。
3. **监控告警**与上一步一并考虑。
4. **多项目扩展**（见路线图）。

---

## 相关文档

- [`docs/QUICKSTART.md`](docs/QUICKSTART.md) — 本地开发 / 86 运维 / 重建
- [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) — 86 服务器部署全记录（含踩坑）
- [`docs/V2_ENVIRONMENT.md`](docs/V2_ENVIRONMENT.md) — 本地 v2 环境（与 pr-agent v1 隔离）
