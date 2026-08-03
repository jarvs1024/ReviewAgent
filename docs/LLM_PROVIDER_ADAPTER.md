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
