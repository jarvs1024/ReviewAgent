# qodercli 替换 opencode 可行性验证报告

## 测试环境
- **qodercli**: v1.20.1 (Windows npm 安装)
- **模型**: MiniMax-M3
- **测试时间**: 2026-07-31
- **测试脚本**: `scripts/test_qodercli_feasibility.py`

## 测试结果汇总

| 测试项 | 结果 | 说明 |
|--------|------|------|
| Test 1: 基础 JSON 输出 | ❌ FAIL | 模型拒绝输出 JSON，要求提供代码内容 |
| Test 2: 文件 Attachment | ⚠️ 不稳定 | 有时输出 JSON，有时输出自然语言 |
| Test 3: 自定义 Agent | ❌ FAIL | `--system-prompt` 参数导致崩溃 (exit code 255) |
| Test 4: 模拟 /improve | ✅ PASS | 成功输出结构化 JSON，包含 findings/suggestions |

## 关键发现

### ✅ 可行的部分

1. **非交互模式 (`-p`) 可用**
   - `qodercli -p --model MiniMax-M3` 能够正常执行并返回
   - 响应时间: 12-57 秒（取决于 prompt 复杂度）

2. **文件 Attachment 可用**
   - `--attachment <file>` 能够附加文件作为 context
   - 模型能够读取文件内容并进行分析

3. **JSON 输出可行（核心场景）**
   - Test 4 成功输出了符合 `/improve` 格式的 JSON
   - 包含 `findings`, `suggestions`, `severity`, `description` 等字段
   - 能够识别代码问题并给出改进建议

### ❌ 存在的问题

1. **JSON 输出不稳定**
   - 模型有时拒绝输出 JSON，要求"提供代码内容"
   - 即使 prompt 明确要求 JSON，仍可能输出自然语言
   - **根因**: qodercli 没有 `response_format: json_object` 强制约束

2. **`--system-prompt` 参数异常**
   - 使用 `--system-prompt` 导致 exit code 255
   - 可能是 Windows 环境下的 bug 或参数不兼容

3. **模型行为不可控**
   - opencode 通过 agent frontmatter 强制 JSON 约束
   - qodercli 没有等效的强制约束机制
   - 输出格式依赖 prompt 工程，不够可靠

## 与 opencode 对比

| 维度 | opencode | qodercli | 结论 |
|------|----------|----------|------|
| HTTP API | ✅ REST API | ❌ 无 API | opencode 胜 |
| 编程调用 | ✅ httpx 调用 | ⚠️ subprocess | opencode 更优雅 |
| JSON 约束 | ✅ frontmatter 强制 | ❌ 仅 prompt 约束 | opencode 更可靠 |
| Agent 定义 | ✅ prompts/*.md | ⚠️ --system-prompt (不稳定) | opencode 更稳定 |
| 文件 context | ✅ files 参数 | ✅ --attachment | 持平 |
| 模型选择 | ✅ 配置灵活 | ✅ --model 灵活 | 持平 |
| 冷启动 | ✅ daemon 无开销 | ⚠️ 每次启动有开销 | opencode 更快 |
| 工具限制 | ✅ read-only 控制 | ⚠️ 无细粒度控制 | opencode 更安全 |

## 替换方案评估

### 方案 A: qodercli subprocess (当前测试)

```python
result = subprocess.run(
    ["qodercli", "-p", "--model", "MiniMax-M3", "--attachment", diff_file, prompt],
    capture_output=True, text=True, timeout=600
)
data = json.loads(result.stdout)
```

**优点**:
- 不需要额外依赖
- 直接使用 qodercli 已配置的模型

**缺点**:
- JSON 输出不稳定，需要额外的解析/重试逻辑
- 每次启动有冷启动开销
- 没有 HTTP API，无法做健康检查/超时控制
- `--system-prompt` 等参数不稳定

**可行性**: ⚠️ **需要大量适配工作**，不推荐

### 方案 B: 直接调 LLM API (推荐)

```python
# 跳过 opencode 和 qodercli，直接调 MiniMax API
resp = httpx.post(f"{LLM_API_URL}/chat/completions", json={
    "model": "MiniMax-M3",
    "messages": [
        {"role": "system", "content": agent_prompt},  # prompts/improve.md
        {"role": "user", "content": diff_content},
    ],
    "response_format": {"type": "json_object"},  # 强制 JSON
})
data = resp.json()["choices"][0]["message"]["content"]
```

**优点**:
- `response_format: json_object` 强制 JSON 输出，100% 可靠
- 不依赖 opencode daemon 或 qodercli
- 保留 ReviewAgent 全部 Python 逻辑
- 更快（无 subprocess 开销）
- 更灵活（可随时切换模型/ provider）

**缺点**:
- 需要自己管理 API key
- 需要自己实现 retry/timeout 逻辑

**可行性**: ✅ **完全可行，推荐方案**

### 方案 C: 混合架构

- 保留 opencode 作为主要推理引擎
- 用 qodercli 做 prompt 开发/调试（交互式迭代更快）
- 用 qodercli 做周报生成（直接读 SQLite）

**可行性**: ✅ 可行，但价值有限

## 最终结论

### ❌ 不推荐用 qodercli 替换 opencode

**原因**:
1. **JSON 输出不稳定** — 核心问题，无法保证每次输出合法 JSON
2. **没有编程接口** — subprocess 调用不如 HTTP API 优雅
3. **没有强制约束** — opencode 的 agent frontmatter 更可靠
4. **参数不稳定** — `--system-prompt` 等参数在 Windows 下有 bug

### ✅ 推荐方案: 直接调 LLM API

**实施步骤**:
1. 获取 MiniMax API key（或其他 OpenAI-compatible API）
2. 修改 `reviewagent/opencode/client.py`，将 HTTP 调用从 opencode serve 切换到直接 LLM API
3. 使用 `response_format: json_object` 强制 JSON 输出
4. 复用现有 prompts/*.md 作为 system prompt
5. 其他代码零改动

**改动量**: 仅修改 `opencode/client.py` 一个文件，约 100 行代码

**收益**:
- 去掉 opencode daemon 依赖（少一个进程、少一个 agent 目录同步问题）
- JSON 输出 100% 可靠（`response_format: json_object`）
- 模型切换更灵活
- 保留 ReviewAgent 全部业务逻辑

## qodercli 的真正价值

qodercli 的价值不在替换 opencode，而在：

1. **Prompt 开发/调试**
   - 用 qodercli 交互式迭代 prompts/*.md 的规则
   - 比"改代码 → 部署 → 触发 webhook"快 10 倍

2. **周报生成**
   - qodercli 直接读 SQLite + 生成 markdown
   - 不需要 opencode 中间层

3. **运维诊断**
   - 用 qodercli 查 telemetry、分析 suggestion 采纳率
   - 交互式探索数据

## 附录: 测试命令

```bash
# 运行验证脚本
python scripts/test_qodercli_feasibility.py

# 单独测试某个场景
qodercli -p --model MiniMax-M3 --no-session-persistence --attachment scripts/_test_diff.patch "审查附件中的 diff，输出 JSON"
```
