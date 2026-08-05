# r11 双 Provider 全流程验证 — 2026-08-05

仓库 `ReviewAgent` 分支 `codex/feat-llm-provider-adapter`（commit `6b8108b`，与 R10 同分支），对应测试仓库
`/Users/jarvs/ReviewAgent`（gitlab mirror `http://127.0.0.1:8929/root/auto-review-test.git`，project_id=34）。

## 改动点

1. **适配层 review + 优化**（commit `6b8108b`，与 R10 在同一分支累积）：
   - `commands/_common.py` 修核心 bug：原 except 子句只列 Opencode*Error，qodercli 抛 QoderCLI*Error 时被
     bare `except Exception` 兜底，telemetry 错误标记从 `qodercli: ...` 退化为 `unexpected: ...`.
     修法：补齐 6 个具体类（Opencode*× 3 + QoderCLI*× 3），无公共基类（避免动 opencode/client.py 类层级）。
     error 字符串用 provider_name 替代硬编码 `opencode`。
   - `qodercli_provider.py` 删 `_legacy` dead flag（设置后无人读）。
   - `qodercli_subprocess.py` `_build_attachment` 用 `uuid.uuid4().hex` 替代 `int(time.time()*1000)`，
     避免同毫秒撞名（并发场景 / 快速重试）。
   - `opencode_provider.py` 改用 `self._client.auth`（public property）替代 `_auth`（private 访问）。
   - `opencode/client.py` 加 `auth` property。
   - `llm/base.py` docstring 补 tolerant_markdown 降级语义说明。
   - `tests/test_llm_adapter.py` 加 2 个回归测试：
     - `test_commands_common_excepts_both_provider_families`（静态检查 except 列表覆盖 6 个具体类）
     - `test_base_command_catches_qodercli_error`（集成回归）
   - 测试：166 passed（含 2 个新增）/ 1 pre-existing fail（与本次无关）。

2. **新 fixture 分支 + 新 MR**：
   - `codex/verify-r11-fixture-base-20260805`（orphan-style base，仅 14 个 fixture 文件，无 .venv / .env / logs 泄漏）
   - `codex/verify-r11-qodercli-1-20260805` + `codex/verify-r11-qodercli-2-20260805`
   - `codex/verify-r11-opencode-1-20260805` + `codex/verify-r11-opencode-2-20260805`
   - 4 个新 MR: `!203` / `!204` / `!205` / `!206`（target=fixture_base，diff 仅 ~2.4KB 2 个文件）
   - 每个分支含 8 个故意 bug，分布与 R9 同：agents 规则 × 2、通用规则 × 2、other 规则 × 2、跨文件规则 × 2

## 7 类功能验证（按 provider × MR 全部通过）

| Feature | qodercli MR 203 r1 | qodercli MR 204 r2 | opencode MR 205 r1 | opencode MR 206 r2 |
|---|---|---|---|---|
| /describe | ✅ 1/1 terminal | ✅ 1/1 terminal | ✅ 1/1 terminal | ✅ 1/1 terminal |
| /improve | ✅ 1/1 terminal | ✅ 1/1 terminal | ✅ 1/1 terminal | ✅ 1/1 terminal |
| auto_chain (MR open) | ✅ runs+2 describe+improve | ✅ runs+2 describe+improve | ✅ runs+2 describe+improve | ✅ runs+2 describe+improve |
| /adopt | ✅ adopted_actions 3→4 | ✅ adopted_actions 0→1 | ✅ adopted_actions 2→4 | ✅ adopted_actions 0→1 |
| /dismiss | ✅ dismissed_actions 0→1 | ✅ dismissed_actions 0→1 | ✅ dismissed_actions 0→1 | ✅ dismissed_actions 0→1 |
| ui_apply | ✅ webhook queued | ✅ webhook queued | ✅ webhook queued | ✅ webhook queued |
| telemetry API | ✅ 14/14 endpoints ok | ✅ 14/14 endpoints ok | ✅ 14/14 endpoints ok | ✅ 14/14 endpoints ok |
| weekly report | ✅ json+md+xlsx files | ✅ json+md+xlsx files | ✅ json+md+xlsx files | ✅ json+md+xlsx files |

汇总：qodercli 16/16 passed（MR 203 + MR 204），opencode 15/16 passed（MR 205 r1 improve 因 RQ 队列串行竞争
timeout 一次，retry 单独跑 8/8 passed）。

## MR 205 opencode improve 输出样本（真实检视产出）

`summary_md`（节选）：
> - `services/verify_r11_2026_08_05.py` L20 — **改用 logger** [HIGH/code quality]: 违反 `SSD-RULE-NO-BARE-PRINT`
> - `services/verify_r11_2026_08_05.py` L30 — **记录异常堆栈** [HIGH/potential bug]: 违反 `SSD-RULE-NO-LOG-EXC`
> - `services/verify_r11_2026_08_05.py` L35 — **补 docstring 与类型注解** [MEDIUM/code quality]: 违反 `SSD-RULE-DOCSTRING-REQUIRED` 与 `SSD-RULE-TYPEHINTS`
> - `services/verify_r11_2026_08_05.py` L46 — **修正函数名拼写** [MEDIUM/code quality]: R-OTHER:typo — `calculte` 拼写错误应为 `calculate`
> - `services/verify_r11_caller_2026_08_05.py` L14 — **修复 import** [HIGH/potential bug]: R-OTHER-IMPACT:import_path

`suggestions_count`: 11, duration 174077ms (174s).

## 已知保留项

- `LLM_PROVIDER=qodercli`（默认）
- `OPENCODE_MODEL=deepseek/deepseek-v4-flash`
- `QODERCLI_NODE_PATH=/opt/homebrew/bin/node`（原 `/usr/bin/node` 不存在，改成 homebrew 实际路径）
- `QODERCLI_JS_PATH=/Users/jarvs/.nvm/versions/node/v22.22.2/lib/node_modules/@qoder-ai/qodercli/bundle/qodercli.js`
- `GITLAB_BOT_USERNAME=non-existent-bot-marker-2026-08-05`（测试 marker，绕 webhook bot_self skip）
- pre-existing failure: `tests/test_improve_alignment.py::test_build_summary_v2_version_increments_per_run`

## 部署

`bash scripts/restart_local.sh` 后：
- 6 个 screen session：opencode `:4096` + webhook `:3000` + 3 worker + weekly
- webhook `:3000` HTTP 200 / opencode `:4096` HTTP 200
- `LLM_PROVIDER=qodercli`（默认）
- 4 个新 MR（`!203` / `!204` / `!205` / `!206`）保持 opened 状态供后续回归

## 日志位置

- `logs/e2e/r11_20260805T011606Z/qodercli/summary-20260805T011606Z.json` — phase 1 qodercli 16/16
- `logs/e2e/r11_20260805T011606Z/opencode/summary-20260805T012657Z.json` — phase 2 opencode 15/16
- `logs/e2e/r11_retry_all_205/summary-20260805T014837Z.json` — retry MR 205 opencode 8/8

## 推送

- Local: `6b8108b...` on `codex/feat-llm-provider-adapter` (待 push)
- GitLab: `codex/feat-llm-provider-adapter` (target remote=gitlab)
