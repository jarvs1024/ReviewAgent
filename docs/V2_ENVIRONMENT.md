# ReviewAgent v2 环境说明（与 pr-agent 完全隔离）

> 启用日期：2026-07-28

## 与 pr-agent v1 的区别

| 维度 | pr-agent v1 | ReviewAgent v2 |
|---|---|---|
| Docker 容器 | `my-pr-agent:v49` | 无容器，宿主机进程 |
| Webhook 端口 | `5050` (pr-agent 容器外映射) | **`5051`** (socat bridge) → `5052` (uvicorn on host) |
| Webhook secret | `414d0c...` | 新生成的 `42abb7d...` |
| GitLab 用户 | `review-bot` (id=35) | **`review-bot-v2`** (id=40) |
| GitLab PAT | `glpat-jsgVs...` (pr-agent) | 新生成的 `glpat-rv2-kAf7n...` |
| Redis | `redis://localhost:6379/0` | `redis://127.0.0.1:63790/2` (test-mate-redis 容器，db=2) |
| RQ 队列 | 默认 `default` | **`review-v2`** |
| opencode 端口 | 4096 (复用) | 4096 (同一份，agent 文件独立) |
| Model | deepseek-v4-flash | **`minimax/MiniMax-M2.7`** |

## 启动流程

### 1. 启动 opencode serve（headless 后台）

```bash
nohup /Users/jarvs/.opencode/bin/opencode serve --port 4096 --hostname 127.0.0.1 \
  > /tmp/opencode.log 2>&1 &
```

### 2. 启动 socat bridge（让 GitLab 容器可达 5051）

```bash
docker run -d --name reviewagent-bridge \
  --network gitlab-stack_net \
  -p 5051:5051 \
  alpine/socat \
  TCP-LISTEN:5051,fork,reuseaddr TCP:host.docker.internal:5052
```

### 3. 启动 ReviewAgent webhook + worker

```bash
bash scripts/run_webhook.sh   # 后台 uvicorn 5052
bash scripts/run_worker.sh    # 后台 rq worker review-v2
```

启动脚本会自动 `source .env`。

## 关键文件

- `reviewagent/prompts/describe.md` — `/describe` 的 prompt 模板（仓库内）
- `~/.config/opencode/agent/pr-describer.md` — opencode agent 系统 prompt 副本（**两文件须保持字面同步**）
- `~/.config/opencode/opencode.json` — provider 配置（minimax provider）

## 已验证端到端

- 项目：`root/auto-review-test` (id=34)
- MR：#126
- URL：http://127.0.0.1:8929/root/auto-review-test/-/merge_requests/126
- 触发：推送新 commit + webhook
- 输出：

```
title: 新增 compliance 报告与导出模块
description_md:
## Description

- 新增 `ComplianceReporter` 类含 `emit_daily` 评分输出

- 新增 `explain` 格式化 ops 摘要

- 新增 `exporter.py` 含 `emit_to_stdout` 等两导出函数
```

## 给后续 Phase 2 的注意点

1. 新增 bot 时务必同步创建专属 webhook + 专属 secret + 独立 RQ 队列
2. ReviewAgent prompt 文件改了，opencode agent 文件须同步更新（或写个 install hook 自动同步）
3. `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES` 是 macOS rq worker 的必需环境变量（不设会 segfault）
4. macOS Docker Desktop 无法直接 `host.docker.internal:port` 访问宿主进程，必须起 socat bridge 才走得通
