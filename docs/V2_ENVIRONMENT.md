# ReviewAgent v2 环境说明（与 pr-agent v1 完全隔离）

> 更新日期：2026-08-02

v2 是跑在**本机 macOS** 上、与旧 `pr-agent v1`（Docker 容器 `my-pr-agent:v49`）完全隔离的一套环境。本文档只描述 v2 特有的差异，共用部分见 [`README.md`](../README.md) 与 [`docs/DEPLOYMENT.md`](DEPLOYMENT.md)。

## 与 pr-agent v1 的区别

| 维度 | pr-agent v1 | ReviewAgent v2 |
|---|---|---|
| 形态 | Docker 容器 `my-pr-agent:v49` | 宿主机进程（uvicorn + rq worker） |
| Webhook 端口 | `5050`（容器内映射） | **`5051`**（socat bridge）→ `5052`（uvicorn on host） |
| Webhook secret | `414d0c...`（已废弃） | 新生成 `42abb7d...` |
| GitLab 用户 | `review-bot`（id=35） | **`review-bot-v2`**（id=40） |
| GitLab PAT | `glpat-jsgVs...`（已废弃） | 新生成 `glpat-rv2-...` |
| Redis | `redis://localhost:6379/0` | `redis://127.0.0.1:63790/2`（testmate-redis 容器，db=2） |
| RQ 队列 | `default` | **`review-v2`** |
| opencode 端口 | 4096（复用） | 4096（同一份 serve，agent 文件独立） |
| 模型 | deepseek-v4-flash | **`minimax/MiniMax-M2.7`** |

---

## 启动流程

仓库已提供脚本，无需手敲环境变量：

```bash
# 1. opencode serve（headless 后台）
nohup /Users/jarvs/.opencode/bin/opencode serve --port 4096 --hostname 127.0.0.1 \
  > /tmp/opencode.log 2>&1 &

# 2. 同步 agent prompt（改了 reviewagent/prompts/*.md 后必跑）
python scripts/sync_agents.py

# 3. socat bridge（让 GitLab 容器经 :5051 可达宿主 :5052）
docker run -d --name reviewagent-bridge \
  --network gitlab-stack_net \
  -p 5051:5051 \
  alpine/socat \
  TCP-LISTEN:5051,fork,reuseaddr TCP:host.docker.internal:5052

# 4. webhook + worker
bash scripts/run_webhook.sh     # 后台 uvicorn :5052
bash scripts/run_worker.sh      # 后台 rq worker review-v2（含 OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES）
```

> `scripts/run_*.sh` 内置了 v2 全部环境变量（GITLAB_URL / PAT / secret / REDIS `:63790/2` / 队列 `review-v2` / 模型 minimax）。这些脚本当前**硬编码了真实凭证**，仅限本地；生产请用 `.env`（见 DEPLOYMENT.md 安全提示）。

---

## 关键文件

- `reviewagent/prompts/describe.md` / `improve.md` — agent prompt 源（仓库内，单一事实来源）
- `~/.config/opencode/agent/{describe,improve}.md` — opencode agent 副本（**须与仓库通过 `scripts/sync_agents.py` 保持同步**）
- `~/.config/opencode/opencode.json` / `opencode.jsonc` — provider 配置（minimax provider）

---

## 已验证端到端

- 项目：`root/auto-review-test`（id=34）
- 触发：推送新 commit / MR 评论 `/describe`
- 输出：改写中文 MR 标题 + Description（Markdown）

---

## 给后续开发的注意点

1. 新增 bot 时务必同步创建专属 webhook + 专属 secret + 独立 RQ 队列（避免和 v1 串味）。
2. **改了 `reviewagent/prompts/*.md` 必须跑 `python scripts/sync_agents.py`** 把副本同步到 `~/.config/opencode/agent/`，否则 opencode serve 用的是旧 prompt。
3. `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES` 是 macOS rq worker 必需环境变量（不设会 segfault）。
4. macOS Docker Desktop 无法直接 `host.docker.internal:port` 访问宿主进程，必须起 socat bridge 才走得通。
