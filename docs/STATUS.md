# ReviewAgent 项目状态

> 更新日期：2026-07-28 · PoC 端到端已跑通（MR `<id>` / deepseek-v4-flash）

## 一句话

ReviewAgent = GitLab webhook → RQ 异步任务 → opencode HTTP API（deepseek-v4-flash） → 中文 MR description。

---

## 当前状态

| Phase | 状态 | 验证 |
|---|---|---|
| Phase 1.1 — 项目骨架 | ✅ 完成 | pyproject + 30 个源文件 |
| Phase 1.2 — 代码污染防护 | ✅ 完成 | tmpfs worktree + 任务结束 rm -rf |
| Phase 1.3 — opencode 客户端 | ✅ 完成 | HTTP API（替代 subprocess） |
| Phase 1.4 — Agent prompt | ✅ 完成 | `prompts/describe.md` (pr-describer) |
| Phase 1.5 — Webhook 接入 | ✅ 完成 | MR Hook + Note Hook + 鉴权 |
| Phase 1.6 — RQ 任务 | ✅ 完成 | Redis db=1 + worker systemd |
| Phase 1.7 — GitLab 客户端 | ✅ 完成 | python-gitlab 薄封装 |
| Phase 1.8 — /describe 端到端 | ✅ 完成 | MR `<id>` description 自动改写 |
| Phase 1.9 — SQLite telemetry | ✅ 完成 | mr_activity / review_runs 表 |
| Phase 2 — /review + /improve | ⏳ 待做 | agent prompt + command 实现 |
| Phase 3 — Telemetry API | ⏳ 待做 | `/api/v1/telemetry/*` 给前端 |
| Phase 4 — 周报 | ⏳ 待做 | weekly-report-agent 主导生成 |

---

## 已验证的端到端链路

```
GitLab MR `<id>` (gitlab.internal)
   ↓ comment "/describe"
Note Hook → 86:3000/webhook (POST)
   ↓ webhook 鉴权 + 路由
enqueue_describe → Redis db=1
   ↓ RQ worker (reviewagent-worker.service)
DescribeCommand.run()
   ↓
GitLab API: get_mr_diff (11.7 KB)
   ↓
prepare_workspace: git clone --bare + worktree + diff.patch
   ↓
OpencodeClient.run (HTTP API)
   POST /session         → ses_xxx
   POST /session/.../message
     parts: [text prompt, file data URL]
     model: deepseek/deepseek-v4-flash
     agent: pr-describer
   ↓
deepseek API 返回 parts: [{step-start}, {reasoning}, {text: "{...}"}, {step-finish}]
   ↓
_extract_assistant_dict: 找最后一个 type=text → JSON 解析 → dict
   ↓
update_mr_title("优化 CI 配置...")
update_mr_description(1801 bytes 中文 Markdown)
   ↓
cleanup_workspace: rm -rf worktree
   ↓
SQLite review_runs: status=success, duration_ms=25467
```

总耗时：~25 秒（含 deepseek API 推理）

---

## 后续路线图

### Phase 2 — `/review` + `/improve`（预计 1.5 周）

**新增文件**：
- `prompts/review.md` — code-reviewer agent
- `prompts/improve.md` — code-improver agent
- `commands/review.py` — /review 工作流
- `commands/improve.py` — /improve 工作流（含行内 suggestion）
- `workers/tasks.py` 加 `run_review` / `run_improve`
- `telemetry/store.py` 加 `suggestions` / `action_events` 表

**agent prompt 设计原则**（沿用 describe 经验）：
- 强制 JSON 输出（含 `output_schema` 字段约束）
- agent 自己拆 hunk / 评 severity / 抽行号（**Python 端不做代码理解**）
- 行内 suggestion 用 `commitable_code_suggestions=true` 让 GitLab UI 可 Apply

**Suggestion Apply 匹配**：
- `fingerprint = sha256(normalized_code)`
- `cohort_key = sha256(file + line + rule_keys)`
- 这段仍由 Python 做（commit SHA 比对），不让 agent 做

### Phase 3 — Telemetry API（预计 1 周）

**新增文件**：
- `telemetry/api.py` 加 FastAPI 路由
- 暴露 `/api/v1/telemetry/*`（11 个端点，参考 my-pr-agent README）

**路由清单**：
```
GET /health
GET /api/v1/telemetry/metrics/overview
GET /api/v1/telemetry/metrics/rules
GET /api/v1/telemetry/metrics/authors
GET /api/v1/telemetry/metrics/severity
GET /api/v1/telemetry/mrs
GET /api/v1/telemetry/mrs/{project_id}/{mr_iid}
GET /api/v1/telemetry/mrs/{project_id}/{mr_iid}/suggestions
GET /api/v1/telemetry/mrs/{project_id}/{mr_iid}/runs
GET /api/v1/telemetry/mrs/{project_id}/{mr_iid}/timeline
GET /api/v1/telemetry/mrs/{project_id}/{mr_iid}/stats
GET /api/v1/telemetry/dismissals
GET /api/v1/telemetry/dismissals/by-rule
GET /api/v1/telemetry/weekly_reports/latest
GET /api/v1/telemetry/weekly_reports/list
GET /api/v1/telemetry/weekly_reports/{project_id}/{week}
```

**鉴权**：`Authorization: Bearer ${REVIEW_TELEMETRY_HTTP_TOKEN}`，空 token 放行（本地）

### Phase 4 — 周报（预计 1 周）

**Agent 主导**（薄 Python + 厚 agent）：
- `prompts/weekly_report.md` — weekly-report-agent
- `reporting/aggregator.py` — 从 SQLite 抽本周数据成 JSON
- `reporting/report_builder.py` — 调 agent 生成 Markdown
- `reporting/scheduler.py` — APScheduler 周一 09:07

**输出双格式**：
- `weekly_reports/{project_id}/{YYYY-WW}.json`
- `weekly_reports/{YYYY-WW}.md`（人类阅读 / 可同步 Obsidian）

---

## 安全 / 维护待办

1. **轮换密钥**（生产前必做）：
   - DeepSeek API key（`sk-ed49b...`）：已落 `/root/.config/opencode/opencode.jsonc`
   - GitLab PAT（`glpat-rFaZ3...`）：已落 `/home/workflow/ReviewAgent/.env`
   - Webhook secret（`414d0c...`）：同上
   - 之前 pr-agent 镜像 `/home/workflow/pr-agent-images/` 也有旧 secret 残留

2. **生产化增强**：
   - opencode 加 systemd unit（目前是 setsid 后台，机器重启丢）
   - 加监控告警（webhook 5xx / token 超限 / 服务下线）
   - 加 Grafana dashboard（前端需要时再做）

3. **多项目扩展**：
   - 当前只接 `<group>/<project>`（MR `<id>` 项目）
   - Phase 5：批量接入 + per-project 配置

---

## 相关文档

- `docs/DEPLOYMENT.md` — 86 服务器部署全记录（含所有踩坑）
- `docs/QUICKSTART.md` — 快速启动 / 本地开发指南
- `plans/glistening-gathering-perlis.md` — 初始设计方案（已部分实现）
- `README.md` — 项目入口