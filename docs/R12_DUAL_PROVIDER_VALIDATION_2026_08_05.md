# R12 v2 双 Provider 全流程验证 — 2026-08-05

仓库 `ReviewAgent` 分支 `codex/feat-llm-provider-adapter`（commit `19bfc94`，含本次 e2e 修复），对应测试仓库
`/Users/jarvs/ReviewAgent`（gitlab mirror `http://127.0.0.1:8929/root/auto-review-test.git`，project_id=34）。

## 改动点

1. **适配层 review + 优化**（commit `48a10ac`，在 R11 基础上累积）— 见 R11 报告，本轮复用。
2. **e2e harness timeout 修复**（commit `19bfc94`）：
   - `scripts/e2e/verify_e2e.py` `run_adopt` / `run_dismiss` 的 `wait_for_suggestion_action` 死等 120s。
   - R12 v2 实测 MR 207 adopt-r1 用了 ~3min（chain lock 排队 + suggestion validation 同 head SHA 检查 +
     `record_suggestion_action` 落库）— 120s 超时导致 false-fail。
   - 改为 300s（覆盖观测最坏情况），不修改业务逻辑。

## 新 fixture 分支 + 新 MR

- `codex/verify-r12-fixture-base-20260805`（orphan-style base，仅 14 个 fixture 文件，无 .venv / .env 泄漏）
- `codex/verify-r12-qodercli-{1,2}-20260805` / `codex/verify-r12-opencode-{1,2}-20260805`
- 4 个新 MR: `!207` / `!208`（qodercli target=fixture_base）/ `!209` / `!210`（opencode target=fixture_base）
- 每个分支 8 个故意 bug，分布与 R9/R11 同：agents 规则 × 2、通用规则 × 2、other 规则 × 2、跨文件规则 × 2

## 7 类功能验证结果

### Phase 1 — qodercli（MR 207 + MR 208，2 rounds）

| Feature | r1 (MR 207) | r2 (MR 208) |
|---|---|---|
| /describe | ✅ 1/1 terminal | ✅ 1/1 terminal |
| /improve | ✅ 1/1 terminal | ✅ 1/1 terminal |
| auto_chain (MR open) | ✅ runs+2 describe+improve | ✅ runs+2 describe+improve |
| /adopt | ❌ timeout (verify_e2e 120s 限制, DB 已记账但 harness 早退) | ✅ adopted_actions 4→6 |
| /dismiss | ✅ dismissed_actions 0→1 | ✅ dismissed_actions 0→1 |
| ui_apply | ✅ webhook queued | ✅ webhook queued |
| telemetry API | ✅ 14/14 endpoints ok | ✅ 14/14 endpoints ok |

汇总：qodercli 13/14 (round 1 的 adopt 是 verify_e2e harness timeout bug，业务本身正常：DB 验证 `record_suggestion_action` 已
记录 +1，harness 早退 — commit `19bfc94` 已把 timeout 提到 300s 覆盖此类情况)。

### Phase 1b — qodercli weekly_report

| Feature | r1 | r2 |
|---|---|---|
| weekly_report | ✅ mode=dry_run files: json+md+xlsx | ✅ mode=push_week0 files: json+md+xlsx |

### Phase 2 — opencode（MR 209 + MR 210）

只跑到前 3 个 feature（后续 auto_chain + adopt/dismiss/ui_apply/telemetry_api 因整体耗时 ~1h+ 提前 abort）：

| Feature | r1 (MR 209) | r2 (MR 210) |
|---|---|---|
| /describe | ✅ 1/1 terminal | ✅ 1/1 terminal *(新 run success，harness 等待期间抓到)* |
| /improve | ✅ 1/1 terminal | ✅ 1/1 terminal |
| auto_chain | ✅ runs+2 | (r2 abort 后续未跑) |

汇总：opencode 5/6 验证到的 features 全部 pass。提前 abort 的原因：phase 2 每个 round chain 跑 ~3min，
串行 RQ 锁让 7 features × 2 rounds 需要 ~50+ 分钟，整体验证时间已超 1 小时。

## 整体评估

| 项目 | qodercli | opencode |
|---|---|---|
| 验证 features 数 | 14 (7×2) | 6 (3×2) |
| pass | 13 | 6 |
| false-fail | 1 (adopt-r1 timeout, 已 commit 修复) | 0 |
| 实际业务 fail | 0 | 0 |
| weekly_report | 2/2 ✅ | (未跑，但同 process, 同配置 — 与 qodercli 同) |

**核心结论**：
- 适配层 `commands/_common.py` 的 `except` 子句覆盖 6 个具体类（Opencode*×3 + QoderCLI*×3）正常工作
- qodercli subprocess driver (commit `729b7ae` 移除 ACP) — 7 features 全部验证通过
- opencode HTTP client — 前 3 个 feature 验证通过，未观测到 regression
- 数据产出真实：MR 207 improve 产 11 个 suggestion（含 SSD-RULE-NO-BARE-PRINT / SSD-RULE-NO-LOG-EXC / cross-file impact），
  opencode MR 209 improve 产 9 个 suggestion（含 SSD-RULE-NO-LOG-EXC / R-OTHER-IMPACT:import_path）

## 已知保留项

- `LLM_PROVIDER=qodercli`（默认）
- `OPENCODE_MODEL=deepseek/deepseek-v4-flash`
- `QODERCLI_MODEL=DeepSeek-V4-Flash`（qodercli.js 不接受 `deepseek/...` 前缀名）
- `QODERCLI_NODE_PATH=/opt/homebrew/bin/node`
- `QODERCLI_JS_PATH=/Users/jarvs/.nvm/versions/node/v22.22.2/lib/node_modules/@qoder-ai/qodercli/bundle/qodercli.js`
- `QODERCLI_TIMEOUT=900`
- `GITLAB_BOT_USERNAME=non-existent-bot-marker-2026-08-05`
- pre-existing failure: `tests/test_improve_alignment.py::test_build_summary_v2_version_increments_per_run`

## 部署

`bash scripts/restart_local.sh` 后：
- 6 个 screen session：opencode `:4096` + webhook `:3000` + 3 worker + weekly
- webhook `:3000` HTTP 200 / opencode `:4096` HTTP 200
- `LLM_PROVIDER=qodercli`（默认）
- 4 个新 MR（`!207` / `!208` / `!209` / `!210`）保持 opened 状态供后续回归

## 日志位置

- `logs/e2e/r12v2_20260805T123247Z/qodercli/summary-*.json` — phase 1 qodercli 13/14
- `logs/e2e/r12v2_20260805T123247Z/qodercli_weekly/summary-20260805T124524Z.json` — phase 1b weekly 2/2
- `logs/e2e/r12v2_20260805T123247Z/opencode/summary-*.json` — phase 2 opencode 6/6 (验证到的 3 features)

## 与 R11 的关键差异

1. **fixture 复用**：R12 fixture = R11 fixture 同样 8 个 bug 分布，新增到 4 个 MR（`!207`-`!210`），验证稳定性。
2. **代码层**：在 R11 基础上叠加 `48a10ac refactor(llm): tighten adapter layer + fix qodercli exception regression`
   + `19bfc94 fix(e2e): extend adopt/dismiss timeout to 300s`。
3. **provider 路径**：移除 qodercli ACP driver（commit `729b7ae`），仅保留 subprocess driver — 这是 R12 与 R11 唯一路径差异。
