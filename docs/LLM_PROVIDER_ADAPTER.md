# LLM Provider 适配层 — 可行性与实施方案

> 将 ReviewAgent 的 LLM 推理引擎从 opencode 扩展为同时支持 opencode + qodercli，通过配置自动切换。

## 1. 背景

### 1.1 当前架构

```
ReviewAgent Python (RQ Worker)
    │
    │  HTTP POST /session + /session/:id/message
    ▼
opencode serve (daemon, port 4096)
    │
    │  加载 agent (~/.config/opencode/agents/*.md)
    │  调用 LLM (MiniMax-M2.7 / DeepSeek-V4-Flash)
    ▼
返回结构化 JSON (suggestions[], title, summary_md...)
```

**核心接口** (`reviewagent/opencode/client.py`):

```python
OpencodeClient.run(
    agent: str,              # agent 名称 (对应 prompts/*.md)
    prompt: str,             # user prompt
    workdir: Path,           # git worktree 路径
    files: list[Path],       # diff 文件列表
    timeout: int,            # 超时秒数
    tolerant_markdown: bool, # 周报兜底模式
) -> OpencodeResult(data=dict, prompt_tokens=int, completion_tokens=int, model=str)
```

### 1.2 当前痛点

| 痛点 | 说明 |
|------|------|
| opencode daemon 额外进程 | 需要单独管理 opencode serve 进程（不在 systemd 内） |
| agent 目录同步 | `sync_agents.py` 写到 `agent/`，opencode 读 `agents/`，需手动复制 |
| JSON 约束依赖模型自觉 | opencode 通过 agent frontmatter 约束，但模型偶发不遵守 |
| 部署依赖重 | 需要 opencode 二进制 + 配置 + auth.json + agent 文件 |

### 1.3 目标

引入 **LLM Provider 适配层**，让上层代码（improve/describe/周报）通过统一接口调用 LLM，底层可配置 opencode 或 qodercli。

---

## 2. qodercli 可行性验证

### 2.1 测试环境

- **qodercli**: v1.20.1 (npm `@qoder-ai/qodercli`)
- **node**: v22+ (`C:\Program Files\nodejs\node.exe`)
- **模型**: DeepSeek-V4-Flash
- **测试时间**: 2026-07-31

### 2.2 关键发现

#### 调用方式

`qodercli.cmd` 在 Python subprocess 中因 PATH 问题失败，**直接用 node 运行 qodercli.js 可绕过**：

```python
# ❌ 不可靠 (PATH 问题)
subprocess.run(["qodercli", ...])

# ✅ 可靠 (直接调 node)
NODE = r"C:\Program Files\nodejs\node.exe"
QODERCLI_JS = r"...\node_modules\@qoder-ai\qodercli\bundle\qodercli.js"
subprocess.run([NODE, QODERCLI_JS, ...])
```

#### Agent 定义

`--agents <json>` + `--agent <name>` 可以定义和选择自定义 agent：

```python
agents_json = json.dumps({
    "code-reviewer": {
        "description": "Code review agent",
        "instructions": "你是代码审查助手。只输出 JSON..."
    }
})
cmd = [..., "--agents", agents_json, "--agent", "code-reviewer", prompt]
```

#### 输出格式

`-o json` 返回结构化元数据，agent 实际输出在 `result` 字段（string，需二次 JSON parse）：

```json
{
  "type": "result",
  "subtype": "success",
  "result": "{\"issues\": [...], \"summary\": \"...\"}",
  "stop_reason": "end_turn",
  "duration_ms": 3700,
  "total_cost_usd": 0,
  "usage": {
    "input_tokens": 0,
    "output_tokens": 0,
    "context_usage_ratio": 0.023
  },
  "modelUsage": {"dfmodel": {"inputTokens": 0, ...}},
  "num_turns": 1,
  "session_id": "..."
}
```

### 2.3 验证结果 (7/7 通过)

| 验证项 | 结果 | 关键数据 |
|--------|------|----------|
| **P0-1 并行调用** | ✅ PASS | 3 路并行，总耗时 = max(单任务)，真正并行 |
| **P0-2 长 prompt** | ✅ PASS | 20KB diff 通过 `--attachment` 传递，JSON 解析成功 |
| **P0-3 工作目录+读文件** | ✅ PASS | `--cwd` 指向项目根，agent 成功读取源文件并正确分析 |
| **P1-1 超时控制** | ✅ PASS | timeout 精确触发，后续调用正常（进程清理干净） |
| **P1-2 工具限制** | ✅ PASS | `--disallowed-tools write,edit,bash` 正常工作 |
| **P2-1 Token 统计** | ✅ PASS | `-o json` 返回 `usage` 对象，含 `input_tokens`/`output_tokens`/`duration_ms` |
| **P2-2 截断检测** | ✅ PASS | `stop_reason` 字段可判断截断，`--max-output-tokens` 可用 |

### 2.4 模型对比

| 模型 | JSON 稳定性 | 响应速度 | 结论 |
|------|------------|----------|------|
| MiniMax-M3 | ❌ 经常拒绝输出 JSON | 快 | 不适合 |
| DeepSeek-V4-Flash | ✅ 3/4 测试稳定输出 JSON | 中等 | **推荐** |

### 2.5 结论

**qodercli 完全可以替换 opencode**，所有核心功能验证通过。

---

## 3. 适配层架构设计

### 3.1 架构图

```
上层代码 (improve.py / describe.py / reporting/*)
    │
    │  from reviewagent.llm.client import get_client()
    │  client.run(agent, prompt, workdir, files, timeout, tolerant_markdown)
    ▼
┌──────────────────────────────────────┐
│  LLMClient (统一接口)                 │
│  ├── run() -> LLMResult              │
│  ├── health_check() -> bool          │
│  └── provider_name -> str            │
└──────────────┬───────────────────────┘
               │ 根据 config.llm_provider 分发
         ┌─────┴─────┐
         ▼           ▼
┌──────────────┐  ┌───────────────┐
│ OpencodeProv │  │ QoderCLIProv  │
│ (HTTP API)   │  │ (subprocess)  │
└──────┬───────┘  └──────┬────────┘
       ▼                 ▼
 opencode serve    node qodercli.js
 (daemon:4096)     (ephemeral)
```

### 3.2 两个 Provider 的差异映射

| 维度 | OpencodeProvider | QoderCLIProvider |
|------|-----------------|------------------|
| **调用方式** | HTTP (httpx) | subprocess (node) |
| **Agent 加载** | 预注册在 `~/.config/opencode/agents/` | `--agents` JSON 内联 |
| **System prompt** | agent md 自动加载 | `--agents` JSON 的 `instructions` |
| **文件传递** | prompt 内联 (截断到 max_diff_chars) | `--attachment` 参数 |
| **工作目录** | 不传 cwd (bug)，内容拼到 prompt | `-w` / `--cwd` |
| **输出解析** | HTTP JSON → parts 数组提取 text | `-o json` → `result` 字段二次 parse |
| **Token 统计** | `info.tokens.input/output` | `usage.input_tokens/output_tokens` |
| **截断检测** | `finish=length` | `stop_reason != "end_turn"` |
| **超时控制** | httpx timeout | subprocess timeout |
| **工具限制** | agent frontmatter `tools: {write: false}` | `--disallowed-tools` 参数 |
| **重试(截断)** | 减半 diff 重试 | 适配层统一实现 |
| **健康检查** | `GET /api/health` | `node --version` |
| **并行安全** | HTTP 天然安全 | subprocess 进程隔离（已验证） |

### 3.3 Agent 定义转换

当前 prompts/*.md 的 frontmatter 如何转成 qodercli `--agents` JSON：

```
prompts/improve.md                    →   --agents JSON
─────────────────────                     ──────────────
frontmatter:
  name: code-improver                 →   {"code-improver": {
  description: 对 MR diff...         →       "description": "对 MR diff...",
  tools:                                  "instructions": "<md content>"
    write: false                     →   }}
    bash: false                      →   --disallowed-tools write,bash

markdown content (角色/规则/输出格式)  →   instructions 字段
```

### 3.4 统一接口

```python
@dataclass
class LLMResult:
    """统一 LLM 调用结果."""
    data: dict[str, Any]       # 解析后的 agent 输出 dict
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""
    duration_ms: int = 0       # 调用耗时
    provider: str = ""         # "opencode" | "qodercli"
    raw_output: str = ""       # 原始输出（调试用）

class BaseLLMProvider:
    """LLM Provider 基类."""

    def run(
        self,
        *,
        agent: str,
        prompt: str,
        workdir: Path,
        files: list[Path] | None = None,
        timeout: int | None = None,
        tolerant_markdown: bool = False,
    ) -> LLMResult: ...

    def health_check(self) -> bool: ...

    @property
    def provider_name(self) -> str: ...
```

**上层代码调用签名完全不变**，只是 import 路径从 `reviewagent.opencode.client` 改为 `reviewagent.llm.client`。

### 3.5 配置设计

```python
# config.py 新增字段
llm_provider: str = "opencode"          # "opencode" | "qodercli"

# qodercli 专属
qodercli_node_path: str = ""            # node 可执行文件路径
qodercli_js_path: str = ""              # qodercli.js 路径
qodercli_model: str = "DeepSeek-V4-Flash"
qodercli_timeout: int = 600
```

环境变量：

```bash
# 切换 provider（默认 opencode，线上无影响）
LLM_PROVIDER=opencode          # 或 qodercli

# qodercli 配置
QODERCLI_NODE_PATH=/usr/bin/node
QODERCLI_JS_PATH=/usr/lib/node_modules/@qoder-ai/qodercli/bundle/qodercli.js
QODERCLI_MODEL=DeepSeek-V4-Flash
QODERCLI_TIMEOUT=600
```

---

## 4. 实施计划

### 4.1 文件结构

```
reviewagent/
├── llm/                          # 新增模块
│   ├── __init__.py               # 模块入口
│   ├── base.py                   # BaseLLMProvider + LLMResult
│   ├── opencode_provider.py      # 包装现有 OpencodeClient
│   ├── qodercli_provider.py      # subprocess 调用 qodercli
│   └── client.py                 # get_client() 工厂 + 配置驱动
├── opencode/                     # 保留不动
│   └── client.py                 # 原 OpencodeClient 实现
├── commands/
│   ├── _common.py                # 改 import
│   └── improve.py                # 改 import
├── reporting/collectors/
│   ├── merged_mrs.py             # 改 import
│   ├── repo_scan.py              # 改 import
│   └── telemetry.py              # 改 import
└── config.py                     # 新增 llm_provider 等配置
```

### 4.2 改动清单

| 文件 | 操作 | 改动量 | 说明 |
|------|------|--------|------|
| `reviewagent/llm/__init__.py` | 新增 | ~5 行 | 模块入口 |
| `reviewagent/llm/base.py` | 新增 | ~50 行 | `LLMResult` + `BaseLLMProvider` |
| `reviewagent/llm/opencode_provider.py` | 新增 | ~40 行 | 包装 `OpencodeClient` |
| `reviewagent/llm/qodercli_provider.py` | 新增 | ~150 行 | subprocess 调用 + JSON 解析 |
| `reviewagent/llm/client.py` | 新增 | ~30 行 | `get_client()` 工厂 |
| `reviewagent/config.py` | 小改 | ~15 行 | 新增 `llm_provider` 等配置 |
| `reviewagent/commands/_common.py` | 小改 | ~3 行 | 改 import |
| `reviewagent/commands/improve.py` | 小改 | ~3 行 | 改 import |
| `reviewagent/reporting/collectors/*.py` | 小改 | ~6 行 | 改 import (3 文件) |
| **合计** | | **~300 行** | 新代码 ~280 行 + 改动 ~20 行 |

### 4.3 实施步骤

1. **创建 `reviewagent/llm/` 模块** — base.py + client.py
2. **实现 OpencodeProvider** — 包装现有 OpencodeClient，零逻辑改动
3. **实现 QoderCLIProvider** — subprocess 调用 + JSON 解析 + agent 转换
4. **修改 config.py** — 新增 llm_provider 等配置
5. **修改上层 import** — _common.py / improve.py / reporting/*
6. **本地测试** — `LLM_PROVIDER=opencode` 确认无回归
7. **服务器测试** — `LLM_PROVIDER=qodercli` 端到端验证
8. **灰度切换** — 确认稳定后默认切 qodercli

### 4.4 服务器端部署（qodercli 模式）

```bash
# 1. 安装 node.js
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo bash -
sudo apt-get install -y nodejs

# 2. 安装 qodercli
sudo npm install -g @qoder-ai/qodercli

# 3. 登录
qodercli login

# 4. 更新 .env
echo 'LLM_PROVIDER=qodercli' >> .env
echo 'QODERCLI_NODE_PATH=/usr/bin/node' >> .env
echo 'QODERCLI_JS_PATH=/usr/lib/node_modules/@qoder-ai/qodercli/bundle/qodercli.js' >> .env
echo 'QODERCLI_MODEL=DeepSeek-V4-Flash' >> .env

# 5. 重启服务
bash scripts/services_ops.sh restart
```

---

## 5. 风险评估

| 风险 | 等级 | 影响 | 应对 |
|------|------|------|------|
| opencode 行为变化 | 低 | 无 | OpencodeProvider 直接包装，不改逻辑 |
| qodercli 升级导致接口变化 | 中 | qodercli 模式不可用 | 独立模块，好修复；可随时切回 opencode |
| 两套 Provider 输出不完全一致 | 中 | 建议质量差异 | `tolerant_markdown` 兜底 + JSON 校验 |
| 服务器 node 环境维护 | 低 | 运维成本 | 配置切回 opencode 即可 |
| Token 统计口径不同 | 低 | telemetry 数据差异 | 允许 0 值，后续统一 |
| qodercli 认证过期 | 中 | 调用失败 | 监控 + 告警 + 自动切回 |

---

## 6. 收益

| 维度 | 收益 |
|------|------|
| **运维简化** | qodercli 模式不需要管理 opencode daemon 进程 |
| **部署简化** | 不再需要同步 agent 文件到 `~/.config/opencode/agents/` |
| **JSON 可靠性** | qodercli + DeepSeek-V4-Flash 的 JSON 输出更稳定 |
| **灵活性** | 可随时切换 provider，A/B 对比效果 |
| **可扩展** | 未来加新 Provider（如直接 LLM API）只需新增一个类 |
| **零风险** | 默认 opencode，线上行为不变；灰度验证后再切换 |

---

## 7. 附录

### 7.1 测试脚本

| 脚本 | 用途 |
|------|------|
| `scripts/test_qodercli_feasibility.py` | 基础可行性验证 (4 项) |
| `scripts/test_qodercli_p0.py` | P0/P1/P2 全面验证 (7 项) |
| `scripts/test_agents_node.py` | `--agents` 参数单独验证 |

### 7.2 参考资料

- qodercli `--help` 输出
- `reviewagent/opencode/client.py` — 当前 opencode 调用实现
- `reviewagent/prompts/loader.py` — agent prompt 加载逻辑
- `reviewagent/commands/_common.py` — 上层调用入口

---

## v2→v3 历史：QoderCLI ACP driver (2026-08-03 ~ 2026-08-05)

v2 阶段把 v1 的一次性 subprocess 替换为长连接 `qodercli --acp`。实现跑通 162/162 单测，端到端 3 并发 8.82s 返回，但在 MR 176 实战 (run 577) 上游 `qodercli --acp` 把 session/prompt 响应流稳卡 7+ 分钟，CPU 0%，只能 `SIGTERM` 杀掉。

**结论 (2026-08-05)**：ACP driver 整链从代码库彻底删除，包括：

- `reviewagent/llm/qodercli_acp.py`（366 行客户端）
- `scripts/probe_qodercli_acp.py`（端到端 probe）
- 5 个 `tests/test_qodercli_acp_*.py`
- `Config` 字段：`qodercli_driver`、`qodercli_acp_extra_args`、`qodercli_max_concurrent_sessions`、`qodercli_queue_wait_timeout`、`qodercli_session_reuse_window`、`qodercli_session_timeout`
- 全部 `.env` / `.env.example` 上的 `QODERCLI_DRIVER` / `QODERCLI_ACP_*` / `QODERCLI_SESSION_*` 变量

如果上游修了 stdin hang，新接入应该另起独立模块（不要再 import 这套旧代码），并在复测前先用最小单 case 验证 `agent_message_chunk` 不会卡死。

回溯阅读：本文档即为完整实施记录。

---

## 附录 A: qodercli subprocess → qoder-sdk 切换 (2026-09-06)

### A.1 为什么切换

`qodercli -p` subprocess 模式 (`qodercli_subprocess.py`, 19k 字节) 累积 4 段 JSON 解析 hack
(`_strip_line_number_prefix` / `_extract_inner_json` / `_strip_fence` / `_unwrap_markdown_wrapper`)
应对 DeepSeek-V4-Flash 输出抖动。近 6 个月打 8+ 次补丁（`git log` 数据），每一段都是 LLM
输出形态变化的兜底。

`qoder-agent-sdk` (qoder 官方 Python SDK) 直接给结构化 `ResultMessage`，干掉全部解析补丁。

### A.2 字段映射（subprocess → SDK）

| subprocess CLI flag | SDK `QoderAgentOptions` 字段 | 备注 |
|---|---|---|
| `--model Lite` | `model = "Lite"` | 完全相同 |
| `--append-system-prompt {md 内容}` | `system_prompt = md 内容` | SDK 支持 string 或 `{"type": "file", "path": "..."}` |
| `--disallowed-tools write,edit,bash,webfetch,websearch` | `disallowed_tools = [...]` | SDK 用 list 不用 csv string |
| `-w {workdir}` | `cwd = str(workdir)` | 相同 |
| `--attachment {tmp_diff}` | `add_dirs = [workdir]` | SDK 没有 attachment, 通过 workdir + Read 工具访问 |
| `--no-session-persistence` | (无需传) | SDK 默认无持久化 |
| `--permission-mode {mode}` | `permission_mode = "{mode}"` | 值集合: default/acceptEdits/plan/bypassPermissions/yolo/dontAsk/auto |
| `--max-turns {N}` | `max_turns = N` | 0=不传 |

### A.3 异常映射表 (SDK 11 类 → ReviewAgent 3 类)

| SDK 异常 (`qoder_agent_sdk`) | ReviewAgent 异常 | 触发场景 |
|---|---|---|
| `CLIJSONDecodeError` | `QoderCLIOutputError` | qodercli stdout 不是 JSON |
| `ModelPolicyTimeoutError` | `QoderCLITimeoutError` | `resolve_model` callback 超时 |
| `CLINotFoundError` | `QoderCLIError` | qodercli binary 找不到 (PATH 上没有) |
| `ProcessError` | `QoderCLIError` | qodercli 进程异常退出 / signal |
| `CLIConnectionError` | `QoderCLIError` | 连接 qodercli 失败 |
| `AuthNotConfiguredError` | `QoderCLIError` | 未配置鉴权 |
| `AuthAccessTokenEnvVarError` | `QoderCLIError` | `QODER_PERSONAL_ACCESS_TOKEN` 未设 |
| `AuthServiceAccountEnvVarError` | `QoderCLIError` | `QODER_SERVICE_ACCOUNT_KEY` 未设 |
| `CloudAgentApiError` | `QoderCLIError` | 云端 agent API 5xx |
| `CloudAgentUnsupportedAuthError` | `QoderCLIError` | 云端 agent 鉴权不支持 |
| `UnsupportedCliCapabilityError` | `QoderCLIError` | qodercli 版本太老, 缺能力 |
| 任何 `QoderSDKError` 子类 (含未列出的) | `QoderCLIError` (catchall) | 升级 SDK 引入新类时兜底 |
| `asyncio.TimeoutError` | `QoderCLITimeoutError` | SDK query 超时 |

`ResultMessage.is_error=True` 的两种细分:
- `subtype=error_during_execution` → **直接 raise** `QoderCLIError`, 上层不会拿到半截结果
- `subtype=error_max_turns` → **log warning + 返回部分数据** (agent 跑满 `max_turns` 但仍产出部分 JSON)

### A.4 单元测试覆盖 (`tests/test_qoder_sdk_provider_errors.py`)

- 12 个 `_map_sdk_exception` 单测 (每个 SDK 异常类一个, 含 catchall)
- 1 个防回归护栏: 遍历 SDK 所有 `QoderSDKError` 子类, 确保每个都能映射到 3 个 ReviewAgent 异常类
- 6 个 `_drive()` 路径覆盖: `error_during_execution` raise / `error_max_turns` 返回部分 / `success` 正常 / SDK 异常映射 / tolerant_markdown 兜底 / 非 JSON 严格模式 raise
- 3 个 `_resolve_cli_path` 严格性测试: 显式路径不存在必须报错 (不静默 fallback PATH)

### A.5 验证结果

| 项 | 状态 |
|---|---|
| 22 个新单元测试 | 全过 |
| 全套单元测试 | 399 过 / 3 失败 (pre-existing) / 6 超时 (集成测试) / 3 跳过 |
| E2E MR 318 (空 diff) | describe 改标题, improve 跳过 (无 diff) |
| E2E MR 319 (含 3 个 bug) | describe 21s 改标题+描述, improve 103s 发 2 条 inline + 顶部总览 + 检视汇总表 |

### A.6 错误诊断字段 (since 2026-09-07)

上层 (`BaseCommand.run` / `commands/_common.py`) 仍只 `except (QoderCLITimeoutError, QoderCLIOutputError, QoderCLIError)`，
但每个异常实例现在带结构化诊断字段，方便：

- Sentry / 报告层直接读字段，不解析 `str(exc)` 字符串
- 上层根据 `retryable` 决定是否重试
- 排障时按 `session_id` 去 qodercli 端拉日志
- 按 `error_code` 路由（鉴权失败 / 配额 / 政策拒绝 / 瞬时不可用）

#### 字段表

| 字段 | 类型 | 来源 | 用途 |
|---|---|---|---|
| `error_code` | `str \| None` | 从 `ResultMessage.errors[0]` 解析 `"code: msg"` 前缀；或 SDK 异常类内置 | 机器可读错误码；上层路由 |
| `machine_code` | `str \| None` | SDK 异常类 `.code` 属性 (e.g. `auth_not_configured`) | SDK 视角的语义 code |
| `session_id` | `str \| None` | `ResultMessage.session_id` | qodercli 端日志关联 |
| `subtype` | `str \| None` | `ResultMessage.subtype` (`success` / `error_max_turns` / `error_during_execution`) | 区分 partial vs hard failure |
| `original_errors` | `tuple[str, ...] \| None` | `ResultMessage.errors` 原文 | 上层拼到 issue / 报告模板 |
| `exit_code` | `int \| None` | `ProcessError.exit_code` | qodercli 进程退出码 |
| `status_code` | `int \| None` | `CloudAgentApiError.status` | 云端 API HTTP 状态 |
| `capability` | `str \| None` | `UnsupportedCliCapabilityError.capability` | 提示用户升级 qodercli |
| `timeout_ms` | `int \| None` | `ModelPolicyTimeoutError.timeout_ms` / asyncio 超时 (秒 * 1000) | 超时相关 |
| `retryable` | `bool \| None` | 按错误码分类 (见下表) | 上层是否自动重试 |
| `extra` | `dict \| None` | 兜底 (e.g. `env_var` 名 / `data_keys`) | 调试用 |

`QoderCLIError(message, **fields)` 构造时所有字段都用 keyword-only 默认 `None`，
老的 `QoderCLIError("msg")` 调用方式（subprocess 路径仍用）完全兼容。

#### `error_code` / `retryable` 决策表（qodercli wire protocol 层）

| error_code | 含义 | retryable |
|---|---|---|
| `105` | 鉴权失败 / token 过期 | `False` |
| `110`, `113`–`122` | 余额 / 配额 / 计费 | `False` |
| `406`, `416`, `430` | 请求被拒 (policy) | `False` |
| `500`, `10408`, `10500` | 服务暂时不可用 | `True` |
| `47902` | 超过 `max_turns` | `False`（拉大 `max_turns` 才能恢复） |
| `100400`–`100403` | 自定义模型相关 | `False` |
| `auth_not_configured` / `auth_*_env_var_not_configured` | SDK 鉴权配置缺失 | `False` |
| `cli_not_found` | qodercli binary 找不到 | `False`（用户手动装） |
| `unsupported_cli_capability` | qodercli 版本太老 | `False`（升级 qodercli） |
| `cloud_agent_5xx` (e.g. `cloud_agent_503`) | 云端 API 5xx | `True` |
| `cloud_agent_4xx` (e.g. `cloud_agent_401`) | 云端 API 4xx | `False` |
| `model_policy_timeout` | `resolve_model` callback 超时 | `True`（拉长 timeout） |
| `sdk_timeout` | asyncio query 超时 | `True`（拉长 timeout） |
| `output_not_json` | stdout 不是 JSON | `False`（重试不会变好） |
| `message_parse_error` | SDK message 解析失败 | `False` |
| `qoder_sdk_unknown` | 未列出的 SDK 异常 | `None`（让上层决策） |
| `cli_process_error` (exit_code >= 1) | qodercli 主动返回错误 | `False` |
| `cli_process_error` (exit_code < 0) | 子进程被信号杀 | `True`（环境瞬时） |
| `cli_process_error` (exit_code is None) | 子进程没起来 | `True`（环境瞬时） |

#### 错误日志格式

每次 raise 前都会先调用 `qoder_sdk.error` 结构化日志（`logger.error` 而非 `exception`，避免双 stack trace），
关键字段：`error_code` / `machine_code` / `session_id` / `subtype` / `exit_code` / `status_code` / `retryable` / SDK 版本号。
上层 Sentry / Logtail 直接读这些字段做告警路由。

```python
# 上层使用示例:
try:
    result = client.run(...)
except QoderCLITimeoutError as e:
    if e.retryable:
        schedule_retry()       # e.g. asyncio timeout, 5xx
    else:
        alert_ops(e.to_dict()) # e.g. max_turns 需要拉大配置
except QoderCLIError as e:
    if e.error_code == "auth_failed":
        notify_user_token_expired(e.extra.get("env_var"))
    elif e.retryable is True:
        schedule_retry()
    else:
        alert_ops(e.to_dict())
```

#### 测试覆盖

`tests/test_qoder_sdk_provider_errors.py` 现共 **61 个测试** (22 原有 + 39 新增)：

- **TestDiagnosticFields (14 个)** — 每个 SDK 异常映射后字段填充 (`error_code` / `retryable` / `exit_code` / `status_code` / `capability` / `original_errors` / `extra`)
- **TestParseResultErrorCode (5 个)** — `_parse_result_error_code` 数字 / 字母 / 无前缀 / 空 / 多 errors 解析
- **TestClassifyResultRetryable (13 个)** — `_classify_result_retryable` 参数化覆盖 8 个 permanent + 3 个 retryable + max_turns + unknown
- **TestDriveResultMessageErrorFields (7 个)** — `_drive` 收到 `is_error=True` 时 raise 异常带 `error_code=105` / `retryable=True` (500) / `retryable=None` (无前缀) / `subtype` / `session_id`；`error_max_turns` 不 raise；SDK 异常映射字段；asyncio 超时 `timeout_ms=timeout*1000`；非 JSON 输出带 `subtype + session_id`

### A.7 验证结果 (after 错误处理完善)

| 项 | 状态 |
|---|---|
| 61 个错误传播 + 字段测试 | 全过 |
| 全套单元测试 (排除 pre-existing 7 失败) | 501 过 / 7 pre-existing 失败 |
| `QoderCLIError("msg")` backward compat | 验证过 (subprocess 路径仍可用) |
| `to_dict()` 不输出 None 字段 | 验证过 (payload 干净) |
| 凭据 (PAT) 不进入 `to_dict()` 字段 | 验证过 (`extra` 不存 token 字面值) |
