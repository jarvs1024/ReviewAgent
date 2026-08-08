# ReviewAgent Telemetry / ReviewAgent 遥测

> 数据采集 / 行为追踪 / 仪表盘 / 周报的事实源 (single source of truth)

`reviewagent.telemetry` 是 ReviewAgent 的全链路遥测后端：每个 MR 的检视过程、每条 improve suggestion 的生命周期、每个 reviewer 决策都被记录到本地 SQLite (`data/telemetry.db`, 来自 `config.sqlite_path`)，并通过 FastAPI 暴露为 `/api/v1/telemetry/*` REST 接口供前端 dashboard 与周报使用。

## 模块结构

| 文件 | 职责 |
|---|---|
| `models.py` | dataclass 数据契约: `MRRecord` / `ReviewRun` |
| `events.py` | 薄 emitter (`emit_*` 函数), 主流程调用, 失败仅警告不阻塞 |
| `store.py` | `Store` 类: SQLite DAO + DDL/迁移 + 查询/聚合 |
| `router.py` (在 `reviewagent/api/`) | FastAPI 路由, 挂载到 `/api/v1/telemetry` |
| `README.md` | 本文档 |

## 数据契约 (`models.py`)

主流程通过 dataclass 写入, 不直接碰 SQL。

### `MRRecord`

| 字段 | 类型 | 说明 |
|---|---|---|
| `project_id` | int | GitLab project id |
| `mr_iid` | int | MR iid |
| `title` | str | MR 标题 |
| `author_username` | str | GitLab API 原始 author, 格式 `name@username` (name 为空时回退到 username) |
| `source_branch` / `target_branch` | str | 源/目标分支 |
| `state` | str | GitLab MR state (`opened` / `closed` / `merged`) |
| `created_at` / `updated_at` / `merged_at` | datetime \| None | ISO 时间, `_parse_dt` 兼容 GitLab `2024-01-15 10:30:00 UTC` 与 `2024-01-15T10:30:00.000Z` 两种格式 |
| `description_generated` | bool | describe 命令是否已落库 (一次性标题守卫) |
| `last_review_at` | datetime \| None | 最近一次检视时间 |

`from_gitlab(mr: dict)` 工厂方法从 GitLab API 返回的 MR dict 构造; `author` 字段为空 username 回退为 `unknown`。

### `ReviewRun`

| 字段 | 类型 | 说明 |
|---|---|---|
| `project_id` / `mr_iid` | int | 归属 MR |
| `command` | str | `describe` / `review` / `improve` |
| `triggered_by` | str | `webhook` / `note` / `scheduled` |
| `actor_username` | str | 触发者 (note 命令为评论人, webhook 为空) |
| `started_at` | datetime | 默认 `_now()` (UTC) |
| `finished_at` | datetime \| None | finish_run 时由 store 写入 |
| `status` | str | `running` / `success` / `failed` / `timeout` / `skipped` |
| `error` | str \| None | 失败时错误摘要 |
| `model` | str \| None | LLM 模型名 |
| `prompt_tokens` / `completion_tokens` / `total_tokens` | int | LLM token 统计; `total_tokens` 在 finish_run 中由 `prompt + completion` 计算 |
| `duration_ms` | int | 实际执行时长 |

## Emit API (`events.py`)

所有 emitter 都包了 try/except, 仅 `logger.warning` 不抛 — 主流程不会因为 telemetry 故障而失败。

| 函数 | 用途 |
|---|---|
| `emit_mr_activity(mr: MRRecord)` | 写入或更新 `mr_activity` 行 (保留已有 `author_sticky`) |
| `emit_run_started(run: ReviewRun) -> int` | 插入 `review_runs` 行, 返回 `run_id` (失败返回 `-1`) |
| `emit_run_finished(run_id, *, status, error=None, model=None, prompt_tokens=0, completion_tokens=0, duration_ms=0)` | 更新 `review_runs` 状态, 自动写入 `finished_at` 与 `total_tokens` |
| `emit_description_generated(project_id, mr_iid)` | 标记 `description_generated=1` + 同步 touch `last_activity_at` |

调用点 (`reviewagent/commands/_common.py`):

- `emit_run_started` 在命令入口
- `emit_run_finished` 在 `_mark_finished` 统一收口 (避免 finally 与正常路径重复触发)
- `status="skipped"` 走 MR 状态守卫 / 子类 `_should_skip` / diff 为空 / diff 过大 4 个跳过分支

## Schema

### `mr_activity` — MR 元信息快照

主键: `(project_id, mr_iid)`。

| 字段 | 类型 | 说明 |
|---|---|---|
| `project_id` / `mr_iid` | int | PK |
| `title` | text | MR 标题 |
| `author_username` | text | 原始 author (`name@username` 格式) |
| `author_sticky` | text | 首次 upsert 时由 `COALESCE(author_sticky, author_username)` 固化, 后续不被覆盖 |
| `source_branch` / `target_branch` / `state` | text |  |
| `created_at` / `updated_at` / `merged_at` | timestamp |  |
| `description_generated` | int | 0/1, describe 落库一次性守卫 |
| `last_review_at` | timestamp | 最近一次检视完成时间 |
| `last_activity_at` | timestamp | MR 最后活动时间 (任意 review / suggestion / action 触发时 touch, MAX 语义不回退) |

索引: `idx_mr_state` on `state`

### `review_runs` — 一次检视任务执行记录

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | int | PK, autoincrement |
| `project_id` / `mr_iid` | int | 归属 MR |
| `command` | text | describe / review / improve |
| `triggered_by` | text | webhook / note / scheduled |
| `actor_username` | text | 触发者 username |
| `started_at` / `finished_at` | timestamp | 起止时间 (UTC) |
| `status` | text | running / success / failed / timeout / skipped |
| `error` | text | 失败摘要 |
| `model` | text | LLM 模型名 |
| `prompt_tokens` / `completion_tokens` / `total_tokens` | int | token 统计; total_tokens 在 `finish_run` 中由 `prompt + completion` 计算 |
| `duration_ms` | int | 实际执行时长 |
| `rule_keys_cited` | text | comma-joined rule_keys (improve 后回填) |
| `suggestion_count` | int | improve 命中 suggestion 数 |

索引: `idx_runs_project_mr` on `(project_id, mr_iid)`, `idx_runs_started` on `started_at`

注: `rule_keys_cited` / `suggestion_count` 是通过 ALTER TABLE 在线迁移加的列 (旧库自动加)。

### `suggestions` — improve 发布的 inline suggestion

记录每条 suggestion 的快照, 用于 `/adopt` 校验与 dashboard。

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | int | PK, autoincrement |
| `project_id` / `mr_iid` | int | 归属 MR |
| `note_id` | text | GitLab discussion/note id (字符串, 唯一) |
| `file_path` | text | 文件路径 |
| `target_line` / `target_line_end` | int | 目标起止行号 (多行替换) |
| `existing_code` / `improved_code` | text | 原文 / 修正 (供 /adopt 校验匹配) |
| `header` | text | suggestion 标题 (LLM 输出) |
| `severity` | text | improve prompt 给的严重等级 (critical / high / medium / low / ...) |
| `head_sha` | text | 发布时的 MR head_sha (supersede 判定用) |
| `state` | text | `open` / `applied` / `dismissed` / `resolved` / `superseded` (5 个值) |
| `applied_at` / `dismissed_at` / `resolved_at` | timestamp | 状态变更时间 |
| `adoption_source` | text | `ui_apply` / `manual_change` / `adopt_command` / `unknown` |
| `dismissed_by` / `resolved_by` | text | 操作者 |
| `dismissed_reason` | text | 用户提供的 dismiss 原因 (dashboard 聚合用) |
| `resolution_source` | text | resolved 时的来源 (`gitlab_resolve` 等) |
| `rule_keys` | text | comma-joined 规则键 (e.g. `SSD-RULE-NO-LOG-EXC`) |
| `one_sentence_summary` | text | 一句话摘要 |
| `importance` | int | LLM 给的重要性 (1-10) |
| `score` | int | score_filter 用的内部评分 (预留, 当前未写入) |
| `label` | text | improve prompt 给的标签 |
| `severity_source` | text | rule / pattern / llm 来源 (预留字段, 当前未写入) |
| `fingerprint` | text | 单条 suggestion 指纹 (跨次去重主键) |
| `cohort_key` | text | 同类 bug 聚合键 (跨次去重兜底) |
| `posted_at` / `created_at` / `updated_at` | timestamp | 发布时间 / DB 创建时间 / 最后更新 |

索引: `idx_sug_project_mr` on `(project_id, mr_iid)`, `idx_sug_note_id` on `note_id`, `idx_sug_state` on `state`, `idx_sug_cohort` on `(mr_iid, cohort_key)`

注: `dismissed_*` / `rule_keys` / `one_sentence_summary` / `importance` / `score` / `fingerprint` / `cohort_key` / `severity_source` / `label` / `posted_at` / `adoption_source` / `resolved_*` 都是在线 ALTER TABLE 迁移加列, 旧库自动补齐。

### `suggestion_actions` — /adopt /dismiss 事件流

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | int | PK, autoincrement |
| `project_id` / `mr_iid` | int | 归属 MR |
| `suggestion_note_id` | text | 关联 `suggestions.note_id` |
| `file_path` | text |  |
| `target_line` | int |  |
| `action` | text | `adopted` / `dismissed` |
| `actor_username` | text | 操作人 |
| `reason` | text | 原因 (dismiss 时用户填, adopt 走 `validation_status`) |
| `validation_status` | text | `/adopt`: `ok` / `target-unchanged` / `content-unavailable` / `gitlab-ui-apply` / `ui-apply` |
| `adoption_source` | text | `ui_apply` / `manual_change` / `adopt_command` / `unknown` (按 `validation_status` 反推) |
| `head_sha_posted` | text | `/adopt` 校验: suggestion 发布时的 head_sha |
| `head_sha_current` | text | `/adopt` 校验: 当前 head_sha |
| `created_at` | timestamp | 事件时间 |

索引: `idx_actions_project_mr` on `(project_id, mr_iid)`, `idx_actions_suggestion` on `suggestion_note_id`

注: `adoption_source` 列也是迁移加的, 迁移时按 `validation_status` 反推回填:

- `gitlab-ui-apply` / `ui-apply` → `ui_apply`
- `ok` → `adopt_command`
- 其他 adopted → `unknown`

### `agent_failures` — 调试用临时表

`save_agent_output_fail(text, agent)` 在 agent 失败时把输出前 500 字符写入此表, 仅调试用, **无 API 端点暴露**。

| 字段 | 类型 |
|---|---|
| `id` | int PK |
| `ts` | timestamp DEFAULT CURRENT_TIMESTAMP |
| `agent` | text |
| `text_preview` | text |

## REST API (`/api/v1/telemetry`)

| Method | Path | 说明 |
|---|---|---|
| GET | `/health` | DB 连接 + `mr_activity` / `review_runs` 行数 |
| GET | `/runs` | run 列表 (`project_id` / `mr_iid` / `command` / `status` / `since` / `until` / `limit` / `offset`) |
| GET | `/runs/{run_id}` | 单 run 详情 (404 if not found) |
| GET | `/summary` | 聚合: `total_runs` / `by_command{cmd:{count,success,failed,timeout,running,avg_duration_ms,total_tokens}}` / `by_status` / `by_day` / `top_mrs` (前 10) |
| GET | `/mr/{project_id}/{mr_iid}` | MR 元信息 + `recent_runs` (最近 50 条); 顺手 enrich `web_url` |
| GET | `/mr/{project_id}/{mr_iid}/runs` | MR 的 run 列表 (`limit` 默认 100) |
| GET | `/mr/{project_id}/{mr_iid}/suggestions` | MR 的 suggestion 列表 (`state` 过滤 + 分页; 响应里加 `state_label` / `adoption_source_label`) |
| GET | `/mr/{project_id}/{mr_iid}/stats` | state / action / severity 计数 + `adoption_rate` (applied / processed 百分比) |
| GET | `/mr/{project_id}/{mr_iid}/timeline` | run + suggestion_posted + suggestion_action 三方归并时间线 (`event_type`, `detail`, `state` / `validation_status`) |
| GET | `/mrs` | MR 列表 (`project_id` / `state` / `since` / `limit`); enrich `web_url` (调 GitLab API, 项目级缓存) |
| GET | `/mrs/{project_id}/{mr_iid}` | alias of `/mr/{project_id}/{mr_iid}` (兼容 pr-agent 风格) |
| GET | `/mrs/{project_id}/{mr_iid}/dismissals` | MR 的 dismiss 详情 (含 `dismissed_reason`, 按 `dismissed_at DESC`) |
| GET | `/dismissals` | dismiss 列表 (`project_id` / `mr_iid` / `since` / `limit`); `rule_key` 参数语义: 匹配 `file_path == rule_key` OR `reason == rule_key` (兼容前端传 file 名当 key 的用法, **不匹配** `suggestions.rule_keys` 字段, 注意) |
| GET | `/dismissals/by-rule` | dismiss 按 `suggestions.rule_keys` 聚合 (含 `(no_rule_key)` 兜底 + 每条 rule 的 `reasons[]` 分布) |
| GET | `/metrics/overview` | `summary` 合并 `suggestion_metrics` (state_counts / severity_counts / action_counts / adopted / dismissed / resolved / adoption_rate) |
| GET | `/metrics/severity` | severity 维度计数 |
| GET | `/metrics/rules` | **当前按 severity 兼容分组返回** (前端 dashboard 直接消费), 不是真正按 `rule_keys` 聚合 — `suggestion_metrics` 里没有 rule_key 聚合路径 |
| GET | `/metrics/authors` | 按 `author_sticky` 聚合的 MR 活跃度 (没有 author 维度的 suggestion / run 计数) |
| GET | `/weekly-reports` | 列出 `data/weekly_reports/weekly-*.json` (`project_id` 过滤按 JSON 内容里的 project_id; `limit` 默认 20) |
| GET | `/weekly-reports/{name}` | 读取单个周报 JSON (防 `../` 路径穿越) |

## 关键行为

### `last_activity_at` MAX 语义

`mr_activity.last_activity_at` = MAX(当前值, `last_review_at`, MAX(suggestions.created_at), MAX(suggestion_actions.created_at))。

写入触发点: `finish_run` / `record_suggestion` / `record_suggestion_action` / `mark_description_generated` / `touch_mr_activity`。

Why: dashboard "MR 最后活动时间" 区别于 `last_review_at` (后者只在检视完成时变化, 不会因 /adopt /dismiss 刷新); MAX 保证单调递增, 写入时间早于已存在的值不会回退。

迁移: `_init_schema` 检测列缺失时调用 `_backfill_last_activity` 一次性回填历史数据; `backfill_last_activity_at()` 公开入口可手动重跑。

### Suggestion dedup (`suggestion_exists_at_line`)

跨次 improve 去重的判定逻辑:

1. 基础过滤: `(project_id, mr_iid, file_path, target_line BETWEEN line±tolerance, state='open')`
2. `rule_keys` 维度 (任一命中):
   - 已有建议 `rule_keys` 为空 / None (旧数据) → 视为命中 (兼容旧行为)
   - 已有建议 `rule_keys` 与新建议 `rule_keys` 任一重叠 (LIKE `%,rk,%` 包裹避免前缀误匹配, 如 `SSD-RULE-NO-LOG` 不能误命中 `SSD-RULE-NO-LOG-EXC`) → 视为命中
3. `state='open'` 限定: 已 `applied` / `dismissed` / `superseded` 视为"已处理", 允许重新检视 (用户 push 改了内容让 auto_detect 标 applied, 然后又撤回原始内容 → 系统应能重新检视出新 issue)
4. `line_tolerance` 默认 2 行 (LLM 跨次 ±1~3 漂移容差), 设为 0 = 严格相等
5. **不限定 `head_sha`**: 跨 V1 / V2 / V3 同一 file:line 仍 dedup, 避免 GitLab 重复评论

`fingerprint` 是单条 suggestion 的精确指纹 (主键 dedup); `cohort_key` 是同类 bug 的聚合键 (兜底 dedup)。

### Head SHA 变化 → 标 superseded

`supersede_stale_open_suggestions(project_id, mr_iid, current_head_sha)`:

- 找到该 MR 所有 `state='open'` 且 `head_sha != current_head_sha` 的 suggestion
- 批量更新为 `state='superseded'` (按 note_id 逐条 executemany)
- 返回被 supersede 的 `note_id` 列表 (供发一次性合并通知)

Why: 用户 UI Apply suggestion 或新 push 后老 suggestion 的行号 / 上下文可能已无效, 留 `state='open'` 会让:

- `/adopt` 误判还待应用 → 触发 reconcile 失败
- 前端 V{N} 列表看到一堆"仍 open"的过时提示
- 下次 improve 的 dedup 误把它们当作"已发过"漏掉新 bug

边界: `head_sha` 为空时直接返回 `[]`, 不操作。

### `adoption_source` 自动回填 (schema 迁移)

迁移时反推回填两条 SQL:

```sql
-- suggestion_actions: 按 validation_status 反推
UPDATE suggestion_actions
SET adoption_source = CASE
    WHEN action != 'adopted' THEN NULL
    WHEN validation_status IN ('gitlab-ui-apply', 'ui-apply') THEN 'ui_apply'
    WHEN validation_status = 'ok' THEN 'adopt_command'
    ELSE 'unknown'
END
WHERE adoption_source IS NULL;

-- suggestions: 从最近一条 adopted action 拉
UPDATE suggestions
SET adoption_source = COALESCE(
    (SELECT sa.adoption_source FROM suggestion_actions sa
     WHERE sa.suggestion_note_id = suggestions.note_id
       AND sa.action = 'adopted'
     ORDER BY sa.id DESC LIMIT 1),
    'unknown'
)
WHERE state = 'applied' AND adoption_source IS NULL;
```

### WAL + autocommit

`_conn()` 启用 `journal_mode=WAL` + `busy_timeout=5000` + `foreign_keys=ON`, `isolation_level=None` (autocommit), 写路径用显式 `BEGIN/COMMIT/ROLLBACK`。`check_same_thread=False` 允许 webhook worker 并发调用。

## 端到端验证

完整测试 (`pytest tests/`):

- `tests/test_improve_alignment.py` — 21 个对齐 / 缩进 / 多行替换测试
- `tests/test_suggestion_actions.py` — 12 个 /adopt /dismiss 测试
- `tests/test_telemetry_endpoints.py` — 3 个新端点集成测试 (record_suggestion 扩展字段、dismissed_reason、by-rule 聚合)
- `tests/test_last_activity_at.py` — `last_activity_at` MAX 语义测试
- `tests/test_supersede_stale_suggestions.py` — head_sha 变化 supersede 测试

e2e 流程 (基于 `codex/telemetry-e2e-20260730-224254` MR !134):

```bash
# 1) 触发 open webhook
curl -X POST http://127.0.0.1:5052/webhook \
  -H 'Content-Type: application/json' -H 'X-Gitlab-Token: ...' \
  -d '{"object_kind":"merge_request","event_type":"merge_request","project":{"id":34},
       "object_attributes":{"iid":134,"action":"open","state":"opened",
                           "source_branch":"codex/telemetry-e2e-20260730-224254",
                           "target_branch":"main","last_commit":{"id":"508c68aa"}},
       "user":{"username":"e2e-telemetry"}}'

# 2) /dismiss 触发 (note webhook)
curl -X POST http://127.0.0.1:5052/webhook \
  -H 'Content-Type: application/json' -H 'X-Gitlab-Token: ...' \
  -d '{"object_kind":"note","event_type":"note","project":{"id":34},
       "merge_request":{"iid":134},
       "object_attributes":{"id":..,"note":"/dismiss 误报",
                           "noteable_type":"MergeRequest","type":"DiffNote",
                           "discussion_id":"<note_id>"},
       "user":{"username":"e2e-runner"}}'

# 3) 查询
curl http://127.0.0.1:5052/api/v1/telemetry/mr/34/134/stats
curl http://127.0.0.1:5052/api/v1/telemetry/dismissals/by-rule?project_id=34
curl http://127.0.0.1:5052/api/v1/telemetry/mrs/34/134/dismissals
curl http://127.0.0.1:5052/api/v1/telemetry/weekly-reports
```

## 与 pr-agent 的差异与补全

| 维度 | pr-agent `telemetry` | ReviewAgent | 备注 |
|---|---|---|---|
| 后端 | sqlite + jsonl | sqlite (单一) | ReviewAgent 暂不需要 jsonl |
| Schema migration | 在线 ALTER + index | 在线 ALTER + `idx_sug_cohort` | 等价 |
| 端点 `/mrs` 列表 | ✅ | ✅ | path 用 `/mrs` |
| 端点 `/mrs/{...}/dismissals` | ✅ | ✅ | 含 `dismissed_reason` |
| 端点 `/dismissals/by-rule` | ✅ | ✅ | reason 分布 (按 `suggestions.rule_keys` 聚合, 含 `(no_rule_key)` 兜底) |
| 端点 `/mrs/{...}/stats` | ✅ | ✅ | adoption_rate, severity_counts |
| 端点 `/mrs/{...}/timeline` | ✅ | ✅ | 合并 run + suggestion + action |
| 端点 `/weekly-reports` | ✅ | ✅ | 列表 + 单条读取 |
| 字段 `cohort_key` | ✅ | ✅ | 内部去重 |
| 字段 `severity_source` | ✅ | ✅ | 预留写入 |
| Severity 规则文件 (.agents/rules) | ✅ | ✅ | 走 `repo_context` 自动加载 |

## 周报 (`reviewagent/reporting/`)

- `telemetry` collector 拉 `mr_activity` / `review_runs` / `suggestions` / `suggestion_actions` 聚合
- `run_weekly_job` 写 `data/weekly_reports/weekly-{week}.json` + 渲染 `.md` + 钉钉推送
- 列表 / 读取通过 `/api/v1/telemetry/weekly-reports` 对接前端
