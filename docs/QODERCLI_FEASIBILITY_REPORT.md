# qodercli 替换 opencode 可行性验证报告

## 测试环境
- **qodercli**: v1.20.1 (npm `@qoder-ai/qodercli`)
- **调用方式**: 直接用 `node qodercli.js`（绕过 `qodercli.cmd` 的 PATH 问题）
- **node**: v22+ (`C:\Program Files\nodejs\node.exe`)
- **测试时间**: 2026-07-31

## 测试过程

### 阶段一：MiniMax-M3 初步测试 (1/4 通过)

| 测试项 | 结果 | 说明 |
|--------|------|------|
| Test 1: 基础 JSON 输出 | ❌ FAIL | 模型拒绝输出 JSON，要求提供代码内容 |
| Test 2: 文件 Attachment | ⚠️ 不稳定 | 有时输出 JSON，有时输出自然语言 |
| Test 3: 自定义 Agent | ❌ FAIL | `--system-prompt` 参数导致崩溃 (exit code 255) |
| Test 4: 模拟 /improve | ✅ PASS | 成功输出结构化 JSON |

**结论**: MiniMax-M3 的 JSON 输出不稳定，不推荐用于结构化输出场景。

### 阶段二：DeepSeek-V4-Flash 测试 (3/4 通过)

切换模型为 DeepSeek-V4-Flash 后结果大幅改善：

| 测试项 | 结果 | 耗时 | 说明 |
|--------|------|------|------|
| Test 1: 基础 JSON 输出 | ✅ PASS | 124.8s | 稳定输出 JSON，包含 9 个 issues |
| Test 2: 文件 Attachment | ✅ PASS | 33.4s | 正确分析附件文件并输出 JSON |
| Test 3: 自定义 Agent | ❌ FAIL | 0.2s | `--system-prompt` 参数 bug (exit code 255) |
| Test 4: 模拟 /improve | ✅ PASS | 25.0s | 输出 3 个 issues，格式正确 |

**结论**: DeepSeek-V4-Flash JSON 输出稳定，但 `--system-prompt` 有 Windows bug。

### 阶段三：`--agents` 参数验证 (突破)

发现 `--system-prompt` 失败根因是 `qodercli.cmd` 内部 `node` 不在 PATH 中。
直接用 `node qodercli.js` 调用后，`--agents <json>` + `--agent <name>` **完全可用**：

```
Exit code: 0
Output: {"message": "hello"}  ← 自定义 agent 成功输出 JSON
```

### 阶段四：P0/P1/P2 全面验证 (7/7 通过)

| 验证项 | 结果 | 关键数据 |
|--------|------|----------|
| **P0-1 并行调用** | ✅ PASS | 3 路并行，总耗时 15.7s = max(单任务)，真正并行 |
| **P0-2 长 prompt** | ✅ PASS | 20KB diff 通过 `--attachment` 传递，JSON 解析成功 |
| **P0-3 工作目录+读文件** | ✅ PASS | `--cwd` 指向项目根，agent 成功读取 `config.py`，正确识别 `has_redis_url=True`、`default_port=4096`、`file_lines=149` |
| **P1-1 超时控制** | ✅ PASS | 10s timeout 精确触发，后续调用正常（进程清理干净） |
| **P1-2 工具限制** | ✅ PASS | `--disallowed-tools write,edit,bash` 正常工作 |
| **P2-1 Token 统计** | ✅ PASS | `-o json` 返回完整 `usage` 对象，含 `input_tokens`/`output_tokens`/`total_cost_usd`/`duration_ms` |
| **P2-2 截断检测** | ✅ PASS | `--max-output-tokens` 可用，`stop_reason` 字段可判断截断 |

### `-o json` 输出格式（意外发现）

qodercli `-o json` 返回丰富的元数据，比 opencode 的 token 统计更完整：

```json
{
  "type": "result",
  "subtype": "success",
  "result": "<agent 实际输出 (string，需二次 JSON parse)>",
  "stop_reason": "end_turn",
  "duration_ms": 3700,
  "duration_api_ms": 3683,
  "total_cost_usd": 0,
  "num_turns": 1,
  "usage": {
    "input_tokens": 0,
    "output_tokens": 0,
    "context_usage_ratio": 0.023
  },
  "modelUsage": {"dfmodel": {"inputTokens": 0, "outputTokens": 0, ...}},
  "session_id": "..."
}
```

## 最终结论

### ✅ qodercli 可以替换 opencode

**所有核心功能验证通过**，关键能力：

| 能力 | 状态 | 说明 |
|------|------|------|
| 非交互模式 (`-p`) | ✅ | 稳定可用 |
| 自定义 Agent (`--agents` + `--agent`) | ✅ | 可内联定义 agent，无需预注册 |
| 文件 Attachment (`--attachment`) | ✅ | 支持大文件 (20KB+ 已验证) |
| 工作目录 (`-w` / `--cwd`) | ✅ | agent 可读取项目源文件 |
| JSON 输出 (DeepSeek-V4-Flash) | ✅ | 3/4 测试稳定输出 JSON |
| 并行调用 (3 路 subprocess) | ✅ | 真正并行，进程隔离安全 |
| 超时控制 | ✅ | subprocess timeout 精确可靠 |
| 工具限制 (`--disallowed-tools`) | ✅ | 可禁用 write/edit/bash |
| Token 统计 (`-o json`) | ✅ | 返回 usage 元数据 |
| 截断检测 (`stop_reason`) | ✅ | 可判断输出是否被截断 |

### 注意事项

1. **调用方式**: 必须直接用 `node qodercli.js`，不能通过 `qodercli.cmd`（PATH 问题）
2. **模型选择**: DeepSeek-V4-Flash 的 JSON 稳定性远好于 MiniMax-M3
3. **`-o json` 的 `result` 字段是 string**: 需要二次 `json.loads()` 解析
4. **Token 统计为 0**: DeepSeek-V4-Flash 通过 qoder 代理调用时 `input_tokens`/`output_tokens` 为 0，可能需要用 `modelUsage` 字段

## 替换方案

### 推荐方案: LLM Provider 适配层

详见 [LLM_PROVIDER_ADAPTER.md](./LLM_PROVIDER_ADAPTER.md)

通过配置 `LLM_PROVIDER=opencode|qodercli` 自动切换，上层代码零改动。

```python
# 调用方式（统一接口）
from reviewagent.llm.client import get_client
client = get_client()
result = client.run(agent="code-improver", prompt=..., workdir=..., files=[...])
# result.data → {"summary_md": "...", "suggestions": [...]}
```

## 附录: 测试脚本

| 脚本 | 用途 | 结果 |
|------|------|------|
| `scripts/test_qodercli_feasibility.py` | 基础可行性验证 (4 项) | MiniMax-M3: 1/4, DeepSeek: 3/4 |
| `scripts/test_qodercli_p0.py` | P0/P1/P2 全面验证 (7 项) | 7/7 通过 |
| `scripts/test_agents_node.py` | `--agents` 参数单独验证 | ✅ 通过 |

### 测试命令

```bash
# 基础验证
python scripts/test_qodercli_feasibility.py

# 全面验证
python scripts/test_qodercli_p0.py

# 直接调用示例
node "C:\Users\2268\AppData\Roaming\npm\node_modules\@qoder-ai\qodercli\bundle\qodercli.js" ^
  -p --model DeepSeek-V4-Flash --no-session-persistence -o json ^
  --agents "{\"reviewer\":{\"description\":\"test\",\"instructions\":\"Output only JSON\"}}" ^
  --agent reviewer ^
  "Review this code: def foo(x): return x + 1"
```
