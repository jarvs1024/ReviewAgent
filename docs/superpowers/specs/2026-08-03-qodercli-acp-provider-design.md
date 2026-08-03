# 2026-08-03 — QoderCLI ACP Provider 设计

> 状态: 草稿 v1（待用户复核）
> 范围: `codex/feat-llm-provider-adapter` 分支的 LLM Provider 适配层
> 目标: 用 QoderCLI ACP 长连接替换当前一次性 `--append-system-prompt` subprocess，从根上解 RQ worker fork 卡死，并为多 MR 并发检视留接口

## 1. 背景与根因

- 当前 `QoderCLIProvider.run()` 每次任务 `subprocess.run([node, qodercli.js, -p, --append-system-prompt, ...])` 拉起新 CLI，~15~20s 出 JSON
- 在 RQ worker (`review-v2` 队列) 内 `subprocess.run` 100% 触发 fork 后 PIPE hold：模型进程退出后，父 RQ worker 持有的 stdout PIPE 仍 keep-open，Python 在 `communicate()` 上等不到 EOF；用 `sample` 看 99% CPU 都在 time_sleep
- `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES` 已加、`bufsize=0` 已加，未能解决
- 根因: RQ 通过 `os.fork()` 派生 job work-horse，macOS 平台上 fork + buffered IO + 父进程持 PIPE 是经典坑
- 业务需求: 同一 RQ worker 可能在短时间收到多个 MR 的 `describe` / `improve` / 周报子任务，并发能力必须有

## 2. 设计目标

1. **根上消 fork 卡死**: 进程模型从"每次任务起新 CLI"切到"worker 持一个长连接 CLI 进程"
2. **并发检视多个 MR**: 单 ACP 进程内多 `sessionId` 并发，跨 worker 仍并行
3. **接口零变更**: `BaseLLMProvider.run(...)` 签名与现有 `LLMResult` 字段不变，`describe / improve / 周报 / 三个 collector` 不动业务代码
4. **可降级**: 出现卡死 / 协议异常时可回滚到一次性 subprocess 路径；可调并发上限
5. **可观测**: 暴露 `qodercli_session_active / queue_depth / acp_send_total / acp_recv_total / last_error` 指标

## 3. 进程与拓扑

```
┌──────────── RQ worker 进程 (review-v2 / review-v2-weekly) ────────────┐
│                                                                         │
│  RQ job handler                                                         │
│     │                                                                   │
│     │  bootstrap_qodercli_acp()  ── 第一次 get_client() 时执行         │
│     │     Popen [node, qodercli.js, --acp, -m DeepSeek-V4-Flash, ...]   │
│     │     stdin/stdout = PIPE, bufsize=0, threadsafe                    │
│     │     send initialize / session/new (warm-up session, 0 个)        │
│     ▼                                                                   │
│  QoderCLIACPClient (单例)                                               │
│     ├─ _send_loop   : 后台线程，序列化写 stdin                          │
│     ├─ _recv_loop   : 后台线程，按行解析 stdout                          │
│     ├─ _pending     : dict[id] -> Future[dict]                          │
│     └─ _sessions    : dict[session_id] -> {agent, created_at, ...}      │
│     │                                                                   │
│     ▼                                                                   │
│  get_client().run(agent="improve", prompt=..., files=[...])             │
│     ├─ semaphore.acquire() (qodercli_max_concurrent_sessions)           │
│     ├─ session/new (or reuse cached session for this agent)             │
│     ├─ session/prompt with prompt + attachment refs                     │
│     ├─ 收 session/update notification，过滤 agent_message_chunk         │
│     └─ 最终 response → LLMResult                                       │
└─────────────────────────────────────────────────────────────────────────┘
                                  │ stdin/stdout
                                  ▼
                  ┌────── qodercli --acp 进程（worker 生命周期） ──────┐
                  │  .qoder/agents/improve.md  (loader.sync 写入)        │
                  │  加载 model=DeepSeek-V4-Flash, maxTurns, tools 隔离  │
                  │  ACP server: initialize / session/new / session/prompt│
                  └────────────────────────────────────────────────────┘
```

跨 worker: `restart_local.sh` 已起 3 个 `review-v2` worker + 1 个 weekly worker，每 worker 独立持 1 个 ACP 进程，全局并发能力 = `worker_count × qodercli_max_concurrent_sessions`。

## 4. 协议细节

### 4.1 QoderCLI ACP 摘要（已实测 1.1.12）

- 启动: `node qodercli.js --acp -m DeepSeek-V4-Flash [--append-system-prompt <text>] [--setting-sources project,user,local]`
- stdin/stdout: 严格 JSON-RPC 2.0（已观察到 `initialize` / `session/new` / `session/prompt` 标准三段）
- agent 推送: `{"method":"session/update","params":{"sessionId":"...","update":{...}}}`，常见 `sessionUpdate`:
    - `available_commands_update` (启动时一次性)
    - `agent_message_chunk` (`content.text` 增量流)
    - `agent_thought_chunk`
    - `tool_call` / `tool_call_update`
    - `plan`
- 认证: 复用 `QODER_PERSONAL_ACCESS_TOKEN` 或 `qodercli login` 落盘状态
- `agentCapabilities.promptQueueing=true`: 同进程多 session 可排队

### 4.2 我们要用到的 request/response 字段

| 方法 | params | 用途 |
|---|---|---|
| `initialize` | `{protocolVersion, clientInfo, capabilities}` | 拿 agentCapabilities / authMethods |
| `session/new` | `{cwd, mcpServers}` | 给指定 agent 开新 session；返回 `sessionId` |
| `session/prompt` | `{sessionId, prompt:[{type:'text',text:...},{type:'resource',...}]}` | 发任务；response 返回 `stop_reason` |
| `session/cancel` | `{sessionId}` | 超时强制取消 |

> 注: 协议字段精确名以实机 `initialize` 返回的 `agentCapabilities` 为准；目前文档站是 Mintlify 客户端渲染，HTML 抓取不到完整 schema。客户端实现时只调真实字段，失败时按 `error.code` / `error.message` 分类。

### 4.3 agent prompt 加载（不依赖 `--agents`）

- 在 worker bootstrap 时（`scripts/sync_qoder_agents.py`），把 `reviewagent/prompts/<name>.md` 转写到项目根 `.qoder/agents/<name>.md`（frontmatter 字段映射见 §5）
- ACP server 启动时 `--setting-sources project,user,local` 加载；`session/new` 不需要传 agent 名，subagent 在 prompt 里被 `@<name>` 显式调度，或把当前 agent 作为主 agent 用 `--agent <name>`
- 选定方案: `qodercli --acp -m DeepSeek-V4-Flash --setting-sources project,user,local --append-system-prompt <concatenated-prompt>` + subagent 走 `.qoder/agents/<name>.md`；调用时 prompt 头部加 `使用 <name> subagent 处理以下任务:`，由 QoderCLI 内部按 description 匹配

## 5. Subagent 配置映射

`reviewagent/prompts/improve.md` 等 frontmatter → `.qoder/agents/improve.md`:

| 源字段 | 目标字段 | 说明 |
|---|---|---|
| `name` | `name` | 文件名 stem 一致 |
| `description` | `description` | QoderCLI 据此做隐式调度 |
| `tools: {write:false, edit:false, bash:false, webfetch:false}` | `disallowedTools: [Write, Edit, Bash, WebFetch, WebSearch]` | 仅写我们禁用的工具，避免破坏默认值 |
| — | `tools: [Read, Grep, Glob, Agent]` | 显式白名单读 + 调度工具，强制只读 |
| — | `permissionMode: default` | 复用父会话权限 |
| — | `maxTurns: 3` | 防失控循环 |
| — | `model: inherit` | 跟随 -m |
| `prompt` (markdown body) | (markdown body) | 系统提示词原样 |
| — | `isolation: worktree` | 不开（subagent 内部用 read 而非改文件） |

转换脚本: `scripts/sync_qoder_agents.py`，单向上写，启动时执行一次（`if mtime_changed`）。

## 6. 接口与并发控制

### 6.1 进程内单例

- `QoderCLIACPClient.bootstrap(workdir, prompts_dir)` 在 RQ worker 第一次 `get_client()` 时执行
- `_send_loop` / `_recv_loop` 后台线程，所有 `_send` 调用通过 `queue.Queue` 串行化
- `recv` 循环按行解析 JSON（实测 QoderCLI 用 `\n` 分隔）
- 进程级 shutdown hook: RQ worker 信号处理触发 `_send({"method":"exit"})` 干净退出，supervisor 重启自动 bootstrap

### 6.2 并发

- 默认 `qodercli_max_concurrent_sessions=4`（同 ACP 进程内最多 4 个活跃 session）
- 实现: `threading.Semaphore` 包裹 session 分配
- 排队策略: FIFO；超时不入队，直接拒绝 → `QoderCLITimeoutError("queue full")`
- 跨 worker 行为不变（每个 worker 各持 1 个 ACP 进程）

### 6.3 Session 复用策略

- 同一 `agent` 名 + 同一 `workdir` 在最近 5 分钟内完成过 → 复用同一 sessionId（减少模型上下文重冷）
- 超过 5 分钟或 `stop_reason` 非 `end_turn` → 开新 session

## 7. 失败模式与降级

| 失败 | 检测 | 处理 |
|---|---|---|
| `qodercli --acp` 进程崩溃 | `_recv_loop` EOF / `poll()` 非 0 | 标记 client 失效，触发 `bootstrap()` 重建；`QoderCLIError("acp process died")` |
| 单 session 超时 | `session/prompt` 的 response 未在 `qodercli_session_timeout` 秒内到 | `session/cancel` + 关闭该 sessionId，让 semaphore 释放 |
| 协议不匹配 | `error.code = -32601 Method not found` | 记录到 telemetry，抛出 `QoderCLIError("protocol mismatch: ...")`，**不**自动回退 |
| 排队满 | semaphore 等待超过 `qodercli_queue_wait_timeout` | `QoderCLITimeoutError("queue full")` |
| 解析失败 | `session/update` chunk 拼接后非 JSON | 走 tolerant_markdown fallback 路径（与 opencode 一致） |

降级开关 `QODERCLI_DRIVER=acp|subprocess`：
- `acp` (默认): 走本设计
- `subprocess`: 走旧 `--append-system-prompt` 路径（保留作紧急回滚）

## 8. 配置项（新增到 .env / config.py）

```ini
# 默认 driver
QODERCLI_DRIVER=acp
# ACP 进程额外参数（透传）
QODERCLI_ACP_EXTRA_ARGS=
# 单进程最大并发 session
QODERCLI_MAX_CONCURRENT_SESSIONS=4
# 队列等位超时（秒）
QODERCLI_QUEUE_WAIT_TIMEOUT=120
# session 复用窗口（秒）
QODERCLI_SESSION_REUSE_WINDOW=300
# 单 session 超时（秒）
QODERCLI_SESSION_TIMEOUT=540
# 模型（沿用 QODERCLI_MODEL=DeepSeek-V4-Flash）
```

## 9. 风险与缓解

| 风险 | 概率 | 缓解 |
|---|---|---|
| ACP 协议字段变更（qodercli 升级） | 中 | `error.code` 分类 → 失败即重 bootstrap；协议层独立模块 + 单测覆盖关键 RPC |
| qoder 代理侧 DeepSeek 限流导致并发 > 1 慢 | 中 | 降级到 `MAX_CONCURRENT_SESSIONS=1`；telemetry 暴露平均延迟 |
| subagent 配置被 QoderCLI 安全策略过滤（如 hooks/mcpServers 移除） | 低 | 我们只写 `name/description/tools/disallowedTools/model/permissionMode/maxTurns` |
| `QODER_PERSONAL_ACCESS_TOKEN` 过期 | 低 | 在 `bootstrap()` 探测 `authenticate` 错误码，统一报 `QoderCLIAuthError` |
| macOS fork 问题在 ACP 长连接上仍然部分存在 | 低 | 进程只 `Popen` 一次（worker 启动时），不放在 RQ job 进程内 |

## 10. 范围外（明确不做）

- 不实现自定义 ACP server 端（仍由 qodercli 提供）
- 不在 ACP 上做 rate limiting（由 semaphore 体现，不做精细令牌桶）
- 不实现 streaming UI 落地（telemetry 收指标够用，tolerant_markdown 是 fallback）
- 不动 opencode provider 任何逻辑

## 11. 验收

- 单元测试: `tests/test_qodercli_acp_provider.py` 覆盖
    - 协议编解码（成功 / 错误码 / 超时 / 取消）
    - 并发：3 session 同时发，最终都能拿到 LLMResult
    - 降级：进程死掉 → 自动 bootstrap 一次
    - 错误码映射
- 端到端: 在 `codex/feat-llm-provider-adapter-verify-2026-08-03` 分支上
    1. opencode 路径：发 MR#181，expect describe + improve 完整评论
    2. 切 `LLM_PROVIDER=qodercli`，同 MR，expect describe + improve 完整评论
    3. 切周报：expect 周报在 5 分钟内出
- 文档: 更新 `docs/LLM_PROVIDER_ADAPTER.md` v2，反映 ACP driver；旧 `docs/QODERCLI_FEASIBILITY_REPORT.md` 标 deprecated
- 提交: 当前 worktree 暂存 → commit → push 到 `codex/feat-llm-provider-adapter-verify-2026-08-03` → 在 GitLab `root/auto-review-test` 上构造数据验证
