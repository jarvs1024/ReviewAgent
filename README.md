# ReviewAgent

GitLab 代码检视平台，基于 [opencode](https://opencode.ai) + webhook 集成。

参考实现：`D:\Code\my-pr-agent` — 本项目独立仓库，**opencode 优先 + 薄 Python 壳**。

## 设计哲学

| 工作 | 实现方 |
|---|---|
| webhook 鉴权、路由、入队 | Python（薄） |
| GitLab API 调用（拉 diff / 发评论） | Python（搬运） |
| 任务队列（RQ / Redis） | Python |
| SQLite 落库 | Python（薄 emitter） |
| **diff 切片 / severity 分类 / 行号抽取 / JSON 输出** | **opencode agent（厚）** |
| **周报文字生成** | **opencode agent** |

Python 端不做任何代码理解工作；agent prompt 是核心交付物。

## 目录结构

```
reviewagent/
├── config.py            # 业务配置（dataclass + 环境变量）
├── logging_setup.py     # loguru
├── main.py              # FastAPI app
├── git/workspace.py     # bare repo + worktree + 代码污染防护
├── gitlab/client.py     # python-gitlab 封装
├── opencode/client.py   # subprocess 调 opencode run --format json
├── prompts/             # agent prompt（Markdown frontmatter）
│   ├── loader.py
│   └── describe.md      # /describe agent
├── commands/describe.py # /describe 工作流
├── webhook/             # GitLab webhook 接入
│   ├── auth.py
│   ├── parsers.py
│   ├── locks.py
│   └── router.py
├── workers/tasks.py     # RQ 任务
└── telemetry/           # 数据采集
    ├── models.py
    ├── store.py
    └── events.py
```

## 启动

### 1. 准备环境

```bash
cp .env.example .env
# 编辑 .env，填 GitLab URL/PAT/webhook secret
```

必需环境变量：
- `GITLAB_URL` — GitLab 实例 URL
- `GITLAB_PERSONAL_ACCESS_TOKEN` — bot 账号 PAT（最低 api scope）
- `GITLAB_WEBHOOK_SECRET` — webhook X-Gitlab-Token 比对值

### 2. 安装依赖

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate    # Linux/macOS
pip install -e ".[dev]"
```

### 3. 启动 Redis

```bash
# 选项 A：本地装
redis-server --daemonize yes

# 选项 B：临时 docker 容器
docker run -d --name review-redis -p 6379:6379 redis:7-alpine
```

### 4. 启动 webhook（端口 3000）

```bash
uvicorn reviewagent.main:app --host 0.0.0.0 --port 3000 --reload
```

### 5. 启动 RQ worker（另起终端）

```bash
rq worker review --url redis://localhost:6379/0
```

### 6. 配置 GitLab webhook

在 GitLab 项目 Settings → Webhooks：

- URL：`http://<server>:3000/webhook`
- Secret token：与 `GITLAB_WEBHOOK_SECRET` 一致
- Trigger：☑ Merge request events  ☑ Comments

## 当前已实现（PoC）

| 功能 | 触发方式 | 状态 |
|---|---|---|
| `/describe` | MR open / update / Note `/describe` | ✅ |
| `/improve` | Note `/improve` | ✅ |
| Telemetry API (`/api/v1/telemetry/*`) | — | ✅ |
| 周报 | — | ⛔ 不实现 |

## API

- `POST /webhook` — GitLab webhook 入口
- `GET /health` — 健康检查
- `GET /docs` — OpenAPI Swagger UI

## 数据

- SQLite：`./data/telemetry.db`（WAL 模式）
- Bare repos：`./data/repos/{project_id}.git/`
- Worktrees：`/tmp/reviewagent-worktrees/`（应挂 tmpfs）
- 周报：`./data/weekly_reports/`（Phase 4）

## 代码污染防护

三层防护：

| 层 | 措施 |
|---|---|
| 容器 / 系统 | `/tmp/reviewagent-worktrees` 用 tmpfs；容器 `read_only` + `cap_drop` + `no-new-privileges` |
| Agent prompt | frontmatter 禁用 `write / edit / bash / webfetch` 工具 |
| 任务流程 | `prepare_workspace()` 后 `cleanup_workspace()` 通过 `try/finally` 保证；bare repo 仅含 git 对象 |

## 配置

- **业务配置**：`reviewagent/config.py`（dataclass，环境变量驱动）
- **Agent 提示词**：`reviewagent/prompts/*.md`（Markdown frontmatter）

单一 `.env` 文件提供环境变量，不使用 Dynaconf / TOML 等重型配置库。

## 下一步

详见 `C:\Users\reviewer\.claude\plans\glistening-gathering-perlis.md`，Phase 1 完成后：

- **Phase 2** — `/improve` agent prompt + 命令实现
- **Phase 3** — Telemetry API（`/api/v1/telemetry/*`）
- **Phase 4** — 周报生成（agent 主导）