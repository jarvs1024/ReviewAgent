# QoderCLI/Headless Provider Comparison (2026-08-04)

## Setup

- 仓库: `/Users/jarvs/ReviewAgent` (branch `codex/feat-llm-provider-adapter`)
- 测试 MR: `root/auto-review-test!176` (8 个故意 bug, 跨 4 类规则)
- worker: 3 个 rq worker (review-v2 queue), `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES`
- 模型: `DeepSeek-V4-Flash` (qodercli) / `deepseek/deepseek-v4-flash` (opencode)
- 比较 3 套配置, 每套跑 3 轮, reset MR 176 suggestions/actions 之间清空

## 三套配置

### Baseline (run 562, qodercli subprocess)
- `QODERCLI_DRIVER=subprocess`
- `QODERCLI_MAX_TURNS` 未设
- `QODERCLI_PERMISSION_MODE` 未设
- 命令: `node qodercli.js -p --model ... -o json -w ... --append-system-prompt ... --disallowed-tools write,edit,bash,webfetch,websearch`

### Plan A (runs 563/564/567-570, qodercli subprocess + headless docs flags)
- `QODERCLI_DRIVER=subprocess`
- `QODERCLI_MAX_TURNS=20` (兜底, 防 qodercli 无限循环)
- `QODERCLI_PERMISSION_MODE=accept_edits` (实验性, **最终决策: 默认关闭**)
- 命令比 baseline 多了 `--max-turns 20` (+ 可选 `--permission-mode accept_edits`)
- 改动文件:
  - `reviewagent/config.py` 新增 `qodercli_permission_mode: str = ""` + `qodercli_max_turns: int = 0`
  - `reviewagent/llm/qodercli_subprocess.py` 抽出 `_build_cmd()` 纯函数, 在 `--disallowed-tools` 后追加 headless flags (空 / 0 时跳过)
  - `.env` / `.env.example` 同步新字段
  - `tests/test_qodercli_plan_a_flags.py` 4 个测试覆盖 default / max-turns / accept_edits / bypass_permissions

### Plan B (runs 573-575, opencode HTTP API)
- `LLM_PROVIDER=opencode`
- 不需要 `QODERCLI_*` env vars, 走 `OPENCODE_URL=http://127.0.0.1:4096` HTTP API
- 命令模式: docs.qoder.com/zh/cli/parallel 推荐方式 — 每任务独立 ephemeral session (POST /session + POST /session/:id/message + DELETE session)
- 文件: `reviewagent/opencode/client.py` 已经按 docs 实现了 headless/parallel pattern (改 0 行)

## 数据 (单 MR, 8 个故意 bug)

| run | provider | dur | sugs | posted | notes |
|---|---|---:|---:|---:|---|
| 562 | baseline qodercli | 174s | 10 | mixed | reference |
| 564 | Plan A qodercli r1 | 192s | 10 | ? | (已 commit, instrumentation log 后移除) |
| 567 | Plan A qodercli solo | 215s | 12 | ? | |
| 568 | Plan A qodercli solo | 408s | 12 | ? | **outlier** — 当时 qodercli 排队慢, 模型长 thinking |
| 569 | Plan A qodercli r2 | 262s | 13 | 11 | |
| 570 | Plan A qodercli r3 | 207s | 12 | 10 | |
| 573 | Plan B opencode r1 | 197s | 10 | 10 | |
| 574 | Plan B opencode r2 | 232s | 11 | 11 | |
| 575 | Plan B opencode r3 | 239s | 11 | 9 | 2 skipped (head_sha drift) |

## 结论

| 维度 | Baseline qodercli | Plan A qodercli | Plan B opencode |
|---|---|---|---|
| 耗时 (median) | 174s | ~210s | ~232s |
| 建议数 (mean) | 10 | 12 | 11 |
| **inline posted 稳定性** | **中** (常 skipped) | **中** (0-11) | **高** (9-11, 几乎全) |
| 兜底 (防卡死) | 无 | `--max-turns 20` | `OPENCODE_TIMEOUT=900` |
| docs 合规 | 旧 CLI 模式 | 增量 headless flags | **完全合规** (parallel pattern) |
| 部署复杂度 | 单 worker 直接用 | 同 baseline | 需先启动 `opencode serve :4096` |

**保留 / 调整**:
- ✅ 保留 Plan A 的 `--max-turns 20` 兜底 (commit `123281e`)
- ❌ 移除默认 `QODERCLI_PERMISSION_MODE=accept_edits` (实验证明增加耗时 +25% 但无明显收益, 仍保留 env var 供需要时开启)
- ✅ Plan B (opencode) 已可作为 provider option 使用, 不需要改代码 (只是部署侧加 `opencode serve :4096`)

**最终决策**:
- 默认 provider: **qodercli subprocess** + `--max-turns 20` (改动已 commit)
- 可选 provider: **opencode HTTP API** (适合 inline posting 稳定性优先场景)

## 工程细节

### 改动文件
- `/Users/jarvs/ReviewAgent/reviewagent/config.py` (2 字段, 2 _env 读取)
- `/Users/jarvs/ReviewAgent/reviewagent/llm/qodercli_subprocess.py` (新 `_build_cmd` + `_build_cmd_for_test`, `run_subprocess` 改用)
- `/Users/jarvs/ReviewAgent/.env.example` (新 env var 注释)
- `/Users/jarvs/ReviewAgent/tests/test_qodercli_plan_a_flags.py` (新, 4 测试)

### 测试
- `pytest tests/`: 173/173 pass (172 → 173 增加 bypass_permissions 测试)

### 切换 provider
改 `.env` 的 `LLM_PROVIDER=qodercli|opencode`, 然后:
```
pkill -f 'rq worker'
/tmp/start_workers5.py
```

### 启动 opencode serve
```
screen -dmS revagent-opencode bash -c 'exec /Users/jarvs/.opencode/bin/opencode serve --port 4096 --hostname 127.0.0.1 2>&1 | tee -a /tmp/opencode-serve.log'
```
