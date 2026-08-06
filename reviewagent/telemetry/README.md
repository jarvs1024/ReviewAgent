# ReviewAgent Telemetry

> 数据采集 / 行为追踪 / 仪表盘 / 周报的事实源

`reviewagent.telemetry` 是 ReviewAgent 的全链路遥测后端：每个 MR 的检视过程、每条 improve suggestion 的生命周期、每个 reviewer 决策都被记录到本地 SQLite (`data/telemetry.db`)，并通过 FastAPI 暴露为 `/api/v1/telemetry/*` REST 接口供前端 dashboard 与周报使用。

## 表结构

### `mr_activity` — MR 元信息
| 字段 | 类型 | 说明 |
|---|---|---|
| `project_id` | int | GitLab project id |
| `mr_iid` | int | MR iid |
| `title`, `author_username`, `author_sticky` | text | 标题/作者; sticky 在第一次 upsert 时固化 |
| `source_branch`, `target_branch`, `state` | text |  |
| `description_generated` | int | 1 = describe 已落库 (用于一次性标题守卫) |
| `last_review_at` | timestamp | 最近一次检视时间 |

### `review_runs` — 一次检视任务
| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | PK | autoincrement |
| `command` | text | describe / improve / review |
| `status` | text | running / success / failed / timeout / skipped |
| `triggered_by` | text | webhook / note / scheduled |
| `rule_keys_cited` | text | comma-joined rule_keys (改善后) |
| `suggestion_count` | int | improve 命中的 suggestion 数 |

### `suggestions` — improve 发布的 inline suggestion
| 字段 | 类型 | 说明 |
|---|---|---|
| `note_id` | text | GitLab discussion id (40-char) |
| `file_path`, `target_line`, `target_line_end` | text / int | 目标位置 |
| `existing_code`, `improved_code` | text | 原文 / 修正 (供 /adopt 校验) |
| `head_sha` | text | 发布时的 MR head_sha |
| `state` | text | `open` / `applied` / `dismissed` / `superseded` |
| `adoption_source` | text | `ui_apply` / `manual_change` / `adopt_command` / `unknown` |
| `applied_at` / `dismissed_at` | timestamp | 状态变更时间 |
| `dismissed_by` | text | dismiss 操作者 |
| `dismissed_reason` | text | 用户提供的 dismiss 原因 (供 dashboard 聚合) |
| `rule_keys` | text | comma-joined 规则键 (e.g. `ZLG-RULE-NO-LOG-EXC`) |
| `importance` | int | LLM 给的重要性 (1-10) |
| `one_sentence_summary` | text | 一句话摘要 |
| `label`, `severity` | text | improve prompt 里给出的标签/严重等级 |
| `severity_source` | text | rule/pattern/llm 来源 (预留字段) |
| `fingerprint`, `cohort_key` | text | 内部去重键 |

### `suggestion_actions` — /adopt /dismiss 事件流
| 字段 | 说明 |
|---|---|
| `action` | `adopted` / `dismissed` |
| `validation_status` | `ok` / `target-unchanged` / `content-unavailable` |
| `head_sha_posted`, `head_sha_current` | `/adopt` 校验使用 |
| `actor_username`, `reason` | 操作人 + 原因 |

## REST API (`/api/v1/telemetry`)

| 端点 | 说明 |
|---|---|
| `GET /health` | DB 连接 + 行数 |
| `GET /runs` | run 列表 (分页 + 多条件过滤) |
| `GET /runs/{run_id}` | run 详情 |
| `GET /mr/{project_id}/{mr_iid}` | MR 元信息 + recent_runs |
| `GET /mr/{project_id}/{mr_iid}/suggestions` | MR 的 suggestion 列表 |
| `GET /mr/{project_id}/{mr_iid}/runs` | MR 的 run 列表 |
| `GET /mr/{project_id}/{mr_iid}/stats` | state/action/severity 计数 + adoption_rate |
| `GET /mr/{project_id}/{mr_iid}/timeline` | run + suggestion + action 三方归并时间线 |
| `GET /mrs` | MR 列表 (按 project/state 过滤) |
| `GET /mrs/{project_id}/{mr_iid}/dismissals` | MR 的 dismiss 详情 (含 reason) |
| `GET /mrs` (alias of `/mr/{...}`) | 兼容 pr-agent 风格 |
| `GET /summary` | 跨 MR 聚合 (by_command/status/day) |
| `GET /metrics/overview` | overview (run + suggestion 合并) |
| `GET /metrics/severity` | severity 维度计数 |
| `GET /metrics/rules` | rule_key 维度计数 (前端 dashboard 直接消费) |
| `GET /metrics/authors` | MR 作者维度活跃度 |
| `GET /dismissals` | dismiss 列表 (since/project/rule_key/mr_id 过滤) |
| `GET /dismissals/by-rule` | dismiss 按 rule_key 聚合 (前端"调优规则"看板) |
| `GET /weekly-reports` | 列出 `data/weekly_reports/weekly-*.json` |
| `GET /weekly-reports/{name}` | 读取单个周报 JSON |

## 端到端验证 (本仓库已落)

完整测试 (`pytest tests/`)：
- `tests/test_improve_alignment.py` — 21 个对齐/缩进/多行替换测试
- `tests/test_suggestion_actions.py` — 12 个 /adopt /dismiss 测试
- `tests/test_telemetry_endpoints.py` — 3 个新端点集成测试 (record_suggestion 扩展字段、dismissed_reason、by-rule 聚合)

e2e 流程（基于 `codex/telemetry-e2e-20260730-224254` MR !134）：

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
| 端点 `/dismissals/by-rule` | ✅ | ✅ | reason 分布 |
| 端点 `/mrs/{...}/stats` | ✅ | ✅ | adoption_rate, severity_counts |
| 端点 `/mrs/{...}/timeline` | ✅ | ✅ | 合并 run + suggestion + action |
| 端点 `/weekly-reports` | ✅ | ✅ | 列表 + 单条读取 |
| 字段 `cohort_key` | ✅ | ✅ | 内部去重 |
| 字段 `severity_source` | ✅ | ✅ | 预留写入 |
| Severity 规则文件 (.agents/rules) | ✅ | ✅ | 走 `repo_context` 自动加载 |

## 周报 (`reviewagent/reporting/`)

- `telemetry` collector 拉 `mr_activity` / `review_runs` / `suggestions` / `suggestion_actions` 聚合
- `run_weekly_job` 写 `data/weekly_reports/weekly-{week}.json` + 渲染 `.md` + 钉钉推送
- 列表/读取通过 `/api/v1/telemetry/weekly-reports` 对接前端
