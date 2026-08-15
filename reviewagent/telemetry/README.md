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

### 相关模块（跨目录协作）

| 文件 | 职责 |
|---|---|
| `reviewagent/commands/_common.py` | `publish_overview` 顶部 pre-reconcile：调 `_scan_and_mark_resolved_silent` catch-up "GitLab UI 已 resolve 但 DB 还 open" 的孤儿 |
| `reviewagent/commands/suggestion_actions.py` | `_scan_and_mark_resolved_silent` silent helper；`sync_resolved_from_gitlab` 复用之（不调 publish_overview，避免递归） |
| `reviewagent/reconciler/__init__.py` + `loop.py` | `reconcile_single_mr()` / `reconcile_open_mrs()` 周期 reconciler，CLI `python -m reviewagent.reconciler.loop [--project-id N]` |
| `scripts/run_reconciler.sh` | reconciler 启动脚本 |
| `scripts/com.jarvs.reviewagent.reconciler.plist` | launchd agent 配置 (StartInterval=60，需手动 `launchctl load`) |

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
| `last_review_at` | datetime \| None | 最近一次检视时间 (dataclass 字段, 由 `store.mark_description_generated` 回填, `from_gitlab` 不设) |

`from_gitlab(mr: dict)` 工厂方法从 GitLab API 返回的 MR dict 构造; `author` 字段为空 username 回退为 `unknown`。

### `ReviewRun`

| 字段 | 类型 | 说明 |
|---|---|---|
| `project_id` / `mr_iid` | int | 归属 MR |
| `command` | str | `describe` / `review` / `improve` |
| `triggered_by` | str | `webhook` (MR hook 入队) / `push` (push hook 入队) / `note` (note hook 入队) / `adopt` (/adopt 命令路径写 run 记录) |
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

## Store API (`store.py`)

`Store` 类 (单例, `get_store()`) 是 telemetry 的 DAO 层。所有 REST 端点 (`/api/v1/telemetry/*`) 都走这里。下面的方法是公开接口, 主流程 / 测试 / 维护脚本都直接调用。

写方法在出错时回滚 (`BEGIN/COMMIT/ROLLBACK`); 读方法只用 `_conn()` 自动提交上下文。`web_url` 字段由 `_enrich_web_url` 在 `get_mr` / `list_mrs` / `/mrs*` 端点上按需注入 (调 GitLab API, 项目级缓存)。

### MR 元信息 (`mr_activity`)

| 方法 | 用途 |
|---|---|
| `upsert_mr(mr: MRRecord)` | INSERT 或 UPDATE `mr_activity`; 已有 `author_sticky` 用 `COALESCE` 保留不被覆盖 |
| `mark_description_generated(project_id, mr_iid)` | `description_generated=1` + touch `last_review_at` / `last_activity_at` |
| `touch_mr_activity(project_id, mr_iid, *, at=None)` | 强制 touch `last_activity_at` (`at=None` = now, 也可传 ISO) |
| `backfill_last_activity_at() -> int` | 全表重算 `last_activity_at` (修复 / 维护 / 测试用) |
| `get_mr(project_id, mr_iid) -> dict \| None` | 读单条 MR, 顺手 enrich `web_url` |
| `list_mrs(*, project_id, since, until, state, limit=100) -> list[dict]` | MR 列表 (created_at DESC), enrich `web_url` |
| `mr_overview(*, project_id, since, until) -> dict` | `{total, opened, closed, merged, window_count}` |
| `rule_key_counts(*, project_id, since, until, top_n=5) -> list[(rule_key, count)]` | 拆分 `suggestions.rule_keys` CSV 后按 key 聚合, 取 top-N |

### Run (`review_runs`)

| 方法 | 用途 |
|---|---|
| `insert_run(run: ReviewRun) -> int` | INSERT, 返回 `run_id` |
| `finish_run(run_id, *, status, error=None, model=None, prompt_tokens=0, completion_tokens=0, duration_ms=0)` | UPDATE 收尾; 自动写 `finished_at` / `total_tokens = prompt + completion`; 同步 touch `mr_activity.last_activity_at` |
| `list_runs(*, project_id, mr_iid, since, until, command, status, limit=100, offset=0) -> list[dict]` | 多条件过滤 (started_at DESC), 给周报 / dashboard |

### Suggestion (`suggestions`)

| 方法 | 用途 |
|---|---|
| `record_suggestion(*, project_id, mr_iid, note_id, file_path, target_line, target_line_end=None, existing_code=None, improved_code=None, header=None, severity=None, head_sha, rule_keys=None, one_sentence_summary=None, importance=None, label=None, fingerprint=None, content_fingerprint=None, cohort_key=None, severity_source=None) -> int` | INSERT 一条 inline suggestion, 同步 touch `last_activity_at`。`posted_at` 与 `created_at` 同时写 `_utcnow()` (当前实现等价)。`content_fingerprint` 是 Batch5 的行号变化场景 dedup (existing_code normalize 后 hash) |
| `suggestion_exists_by_fingerprint(project_id, mr_iid, fingerprint, head_sha="") -> bool` | 跨次精确指纹 dedup (主键命中即返回 True)。Z3+ 行为: 已处理状态 (applied/dismissed/resolved) 永远命中; `state="open"` 必须 `head_sha` 一致 (force-push 后残留 → 放行重新识别), 防止 fingerprint 维度因 head_sha 变化误命中 |
| `suggestion_exists_at_line(project_id, mr_iid, file_path, target_line, severity="", head_sha="", line_tolerance=0, rule_keys=None, existing_code="") -> bool` | 跨次 heuristic dedup (`file, line±tolerance, state=open[, rule_keys 重叠 | content_fingerprint 命中]` + `head_sha` 兜底), 详见下方"关键行为"。`existing_code` 用于 content_fingerprint dedup (行号变化如 Apply docstring 后 +1 仍能命中, Batch5; MR301/aa87d4c 补充: 无 rule_keys 时退化为 content_fingerprint 维度拦住重复发布); `severity` 保留参数仅为向后兼容, 当前实现不再按 severity 过滤 |
| `list_suggestion_headers(project_id, mr_iid) -> list[dict]` | 轻量列表 (file / line / header / severity / fp_short 前 8 位), 给 agent prompt 注入避免重复提 |
| `get_suggestion_by_note_id(note_id) -> dict \| None` | 按 GitLab note_id 查 (id DESC LIMIT 1) |
| `find_open_suggestion_by_line(*, project_id, mr_iid, file_path, target_line, window=3) -> dict \| None` | UI Apply 兜底匹配 (state=open, line±window, 按行号距离 + id 排序) |
| `list_open_suggestions(*, project_id, mr_iid) -> list[dict]` | 全量 open suggestion (note_id / file_path / target_line / existing_code / improved_code / head_sha), 给 `auto_detect_applied` 跑 reconcile |
| `list_resolved_suggestions(*, project_id, mr_iid) -> list[dict]` | 全量 `state='resolved'` 且 `resolution_source IN ('gitlab_resolve', 'publish_overview_reconcile')` 的 suggestion (字段同上), 给 `auto_detect_applied` 的 late_detect 跑 reconcile。**白名单只覆盖 bot 误分类的两条路径**——/adopt 走 `adoption_source='adopt_command'` 不进 / /dismiss 状态是 dismissed 也不进, 避免覆盖 (`publish_overview_reconcile` 是 MR289 根因 #2, commit c9760b2 加) |
| `update_suggestion_state(note_id, state, *, actor_username=None, dismissed_reason=None, adoption_source=None, adoption_evidence=None, applied_commit_sha=None, expected_states=None) -> bool` | 状态机: `applied` / `dismissed` / `resolved` / `superseded`; 自动写 `applied_at` / `dismissed_at` / `dismissed_by` / `dismissed_reason` / `resolved_at` / `resolved_by` / `resolution_source` / `adoption_evidence` / `applied_commit_sha` / `updated_at`。传 `expected_states` 时 WHERE 带 `state IN (...)` 原子化 guard (Batch7/MR264), 防并发 race 覆盖; 返回 bool: True=实际更新, False=state 不在 expected 内 (并发修改跳过) |
| `supersede_stale_open_suggestions(*, project_id, mr_iid, current_head_sha) -> list[str]` | head_sha 不一致的全标 superseded, 返回被 supersede 的 `note_id` 列表; `current_head_sha=""` (空字符串) 时返回 `[]` 不操作 |
| `update_suggestion_note_id(suggestion_id, new_note_id)` | webhook /adopt 兜底命中后回写真实 GitLab note_id |
| `list_suggestions(*, project_id, mr_iid, state, since, until, limit=100, offset=0) -> list[dict]` | 多条件分页 (created_at DESC, id DESC) |
| `get_reviewed_file_shas(project_id, mr_iid) -> dict[str, str]` | V8 增量检视: 返回 `{file_path: head_sha}` ——每个文件最近一次被检视时的 head_sha (不区分 state, dismissed/applied 的文件复用)。无 suggestion 的文件不在结果中 → 调用方视为首次检视 (commit 7708877) |
| `suggestion_stats(project_id, mr_iid) -> dict` | `{total, state_counts, action_counts, severity_counts, adopted, dismissed, resolved, processed, open, adoption_rate}` (`adoption_rate = applied / processed`, 百分比) |
| `suggestion_metrics(*, project_id, since, until) -> dict` | 周报 / 仪表盘维度: `state_counts` / `severity_counts` / `action_counts` / `adoption_rate` (0~1 小数) / `adoption_pct` |
| `supersede_stale_in_cohort(*, project_id, mr_iid, cohort_key, keep_note_id) -> list[str]` | 同 cohort 内除 keep_note_id 外所有 open/resolved/dismissed 标 superseded（cohort 归并兜底；process_adopt / mark_suggestion_applied_by_diff 末尾调一次） |
| `supersede_suggestion(old_note_id, new_note_id, generation) -> None` | 单条 supersede（被新 suggestion 取代；Batch3 写 `supersedes_note_id` + `superseded_at` + 新 suggestion 的 `cohort_generation`）|
| `get_latest_in_cohort_excluding(*, project_id, mr_iid, cohort_key, exclude_note_id) -> dict \| None` | cohort 内排除某 note 后取最新一条（Batch3 improve 去重用，跳过 state='superseded'） |
| `list_latest_by_cohort(*, project_id, mr_iid) -> list[dict]` | 每个 cohort_key (fallback 到 note_id) 取最新一条 + 任何 terminal state 全保留 (Batch6/MR263 + MR299 二次扩展)。排除 superseded。规则: `row_number=1` (最新一条) 永远保留;任何 terminal state (applied/dismissed/resolved) 全部保留——不论 row_number。MR299 修复 "terminal + open 共存" 场景: 用户对老版本做了 applied/resolved/dismissed, 新一轮又发了同位置 open suggestion 时, 老版本 terminal 与新 open 都参与统计 (旧版本用户动作不能被新一代 open 覆盖) |
| `count_superseded_in_mr(*, project_id, mr_iid) -> int` | 显式 superseded 数（`supersede_suggestion` 触发） |
| `count_hidden_by_cohort(*, project_id, mr_iid) -> int` | cohort 归并隐藏的旧记录数（list_latest_by_cohort 中 row_number > 1） |

### Action (`suggestion_actions`)

| 方法 | 用途 |
|---|---|
| `record_suggestion_action(*, project_id, mr_iid, suggestion_note_id, file_path=None, target_line=None, action, actor_username=None, reason=None, validation_status=None, adoption_source=None, head_sha_posted=None, head_sha_current=None) -> int` | INSERT /adopt /dismiss 事件; 同步 touch `last_activity_at` |
| `list_suggestion_actions(*, project_id, mr_iid, action, since, until, limit=100, offset=0) -> list[dict]` | 多条件过滤 (id DESC) |
| `list_dismissals(*, project_id, mr_iid, since, until, rule_key=None, limit=200) -> list[dict]` | 关联 `suggestions` 取 `dismissed_reason`; `rule_key` 过滤走 `file_path == rule_key OR reason == rule_key` (兼容前端传 file 名当 key) |
| `dismissals_by_rule(*, project_id, since) -> list[dict]` | 按 `suggestions.rule_keys` 聚合, 含 `(no_rule_key)` 兜底 + 每条 rule 的 `reasons[]` 分布 |
| `distinct_rule_keys(*, project_id, mr_iid) -> list[str]` | 全量去重 rule key 列表 (按 created_at DESC 扫, 去重后排序) |

### 调试 / 聚合

| 方法 | 用途 |
|---|---|
| `save_agent_output_fail(text, agent)` | 写 `agent_failures` 表前 500 字符 (调试用, 无 API 端点) |
| `summary(*, since, until) -> dict` | 周报 / 仪表盘聚合: `total_runs` / `by_command{cmd:{count,success,failed,timeout,running,avg_duration_ms,total_tokens}}` / `by_status` / `by_day` / `top_mrs` (前 10) |

## 状态 / 来源标签映射 (`router.py`)

后端在 `/mr/.../suggestions` 响应里额外加 `state_label` 和 `adoption_source_label` 字段，前端直接展示中文标签：

| 内部值 | state_label | adoption_source_label |
|---|---|---|
| `open` | 待处理 | — |
| `applied` | 已采纳 | — |
| `dismissed` | 已忽略 | — |
| `resolved` | 已关闭（未分类） | — |
| `superseded` | 已过期 | — |
| `ui_apply` | — | 应用建议 |
| `manual_change` | — | 手动修改 |
| `adopt_command` | — | /adopt |
| `unknown` | — | 历史数据 |
| `gitlab_resolve` | — | GitLab 直接解决主题 |

未在表内的 `adoption_source` (如 `late_detect` / `periodic_reconcile` / `publish_overview_reconcile`) 返回 `None`，前端走兜底展示。

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

注: `triggered_by` (防御性补老库) / `rule_keys_cited` / `suggestion_count` 是通过 ALTER TABLE 在线迁移加的列 (旧库自动加)。

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
| `severity` | text | improve prompt 给的严重等级 (high / medium / low) |
| `head_sha` | text | 发布时的 MR head_sha (supersede 判定用) |
| `state` | text | `open` / `applied` / `dismissed` / `resolved` / `superseded` (5 个值) |
| `applied_at` / `dismissed_at` / `resolved_at` | timestamp | 状态变更时间 |
| `adoption_source` | text | `ui_apply` / `manual_change` / `adopt_command` / `unknown` / `gitlab_resolve` / `late_detect` / `publish_overview_reconcile` (4 → 7, MR289 + c9760b2 扩展) |
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
| `content_fingerprint` | text | existing_code normalize 后 hash，行号变化 (如 Apply docstring 后 +1) 仍能命中 dedup (Batch5) |
| `cohort_key` | text | 同类 bug 聚合键 (跨次去重兜底) |
| `cohort_generation` | int | cohort 代际号（默认 1）；跨多轮重复发布区分代际 (Batch2/3) |
| `supersedes_note_id` | text | 本条 suggestion 取代的旧 note_id (Batch2/3) |
| `superseded_at` | timestamp | 何时被取代 (Batch2/3) |
| `adoption_evidence` | text | 采纳证据等级 (Batch2：`exact_match` / `strict_token` / `region_changed` / `late_detect` 等) |
| `applied_commit_sha` | text | 关联到的 commit sha (Batch4: Apply suggestion 时记录，便于审计) |
| `posted_at` / `created_at` / `updated_at` / `applied_at` / `dismissed_at` / `resolved_at` | timestamp | 发布时间 / DB 创建时间 / 最后更新 / 采纳时间 / 忽略时间 / 解决时间 |

索引: `idx_sug_project_mr` on `(project_id, mr_iid)`, `idx_sug_note_id` on `note_id`, `idx_sug_state` on `state`, `idx_sug_cohort` on `(mr_iid, cohort_key)`

注: 以下列都是在线 ALTER TABLE 迁移加的, 旧库自动补齐:

- `dismissed_*` / `rule_keys` / `one_sentence_summary` / `importance` / `score` / `label` / `severity_source` / `posted_at` / `adoption_source` / `resolved_*`
- `fingerprint` / `cohort_key` — Z1+ 跨次去重维度
- `content_fingerprint` — Batch5, 行号变化场景
- `cohort_generation` / `supersedes_note_id` / `superseded_at` — Batch2/3 cohort 代际
- `adoption_evidence` — Batch2 采纳证据等级
- `applied_commit_sha` — Batch4 Apply suggestion 关联 commit

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
| `validation_status` | text | `/adopt` 路径：`ok` / `target-unchanged` / `content-unavailable` / `gitlab-ui-apply` / `ui-apply`；其他：`gitlab-resolve` (GitLab 直接解决主题) / `late-detect-apply` (late_detect 兜底翻 applied) / `same-head` (head_sha 一致但行号偏移) / `already-{state}` (重复 /adopt /dismiss 已记录) / `publish_overview_reconcile` (顶部 pre-reconcile) / `periodic_reconcile` (周期 reconciler) |
| `adoption_source` | text | `ui_apply` / `manual_change` / `adopt_command` / `unknown` / `gitlab_resolve` / `late_detect` / `periodic_reconcile` / `publish_overview_reconcile` (按 `validation_status` 反推 + c9760b2 扩展) |
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
| GET | `/summary` | 聚合: `since` / `until` / `total_runs` / `by_command{cmd:{count,success,failed,timeout,running,avg_duration_ms,total_tokens}}` / `by_status` / `by_day` / `top_mrs` (前 10) |
| GET | `/mr/{project_id}/{mr_iid}` | MR 元信息 + `recent_runs` (最近 50 条); 顺手 enrich `web_url` |
| GET | `/mr/{project_id}/{mr_iid}/runs` | MR 的 run 列表 (`limit` 默认 100) |
| GET | `/mr/{project_id}/{mr_iid}/suggestions` | MR 的 suggestion 列表 (`state` 过滤 + 分页; 响应里加 `state_label` / `adoption_source_label`) |
| GET | `/mr/{project_id}/{mr_iid}/stats` | state / action / severity 计数 + `adoption_rate` (applied / processed 百分比) |
| GET | `/mr/{project_id}/{mr_iid}/timeline` | run + suggestion_posted + suggestion_action 三方归并时间线 (at DESC), 响应 `{"events": [{at, event_type, event_id, detail, state}]}`。event_type=`run` 时 state=status; event_type=`suggestion_action` 时 state=validation_status; event_type=`suggestion_posted` 时 state=suggestion.state |
| GET | `/mrs` | MR 列表 (`project_id` / `state` / `since` / `limit`); enrich `web_url` (调 GitLab API, 项目级缓存) |
| GET | `/mrs/{project_id}/{mr_iid}` | alias of `/mr/{project_id}/{mr_iid}` (兼容 pr-agent 风格) |
| GET | `/mrs/{project_id}/{mr_iid}/dismissals` | MR 的 dismiss 详情 (含 `dismissed_reason`, 按 `dismissed_at DESC`) |
| GET | `/dismissals` | dismiss 列表 (`project_id` / `mr_iid` / `since` / `limit`); `rule_key` 参数语义: 匹配 `file_path == rule_key` OR `reason == rule_key` (兼容前端传 file 名当 key 的用法, **不匹配** `suggestions.rule_keys` 字段, 注意) |
| GET | `/dismissals/by-rule` | dismiss 按 `suggestions.rule_keys` 聚合 (含 `(no_rule_key)` 兜底 + 每条 rule 的 `reasons[]` 分布) |
| GET | `/metrics/overview` | `summary` 合并 `suggestion_metrics` (state_counts / severity_counts / action_counts / adopted / dismissed / resolved / adoption_rate) |
| GET | `/metrics/severity` | severity 维度计数 |
| GET | `/metrics/rules` | **当前按 severity 兼容分组返回** (前端 dashboard 直接消费), 不是真正按 `rule_keys` 聚合。要按 rule_key 维度用 `Store.distinct_rule_keys(*, project_id, mr_iid)` 拉去重列表, 或 `Store.rule_key_counts(*, project_id, since, until, top_n=5)` 直接拿计数 (这俩方法无 REST 端点暴露) |
| GET | `/metrics/authors` | 按 `author_sticky` 聚合的 MR 活跃度 (没有 author 维度的 suggestion / run 计数) |
| GET | `/weekly-reports` | 列出 `data/weekly_reports/weekly-*.json`；`project_id` 过滤按 JSON 内容里的 `project_id` 字段；`limit` 默认 20 |
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
2b. `content_fingerprint` 维度 (MR301 修复, commit aa87d4c 新增): 当新建议无 `rule_keys` 时, 第 2 点整段被 `if rule_keys:` 守卫跳过, 此时退化为仅靠 `fingerprint` 主键 (而旧数据 `fingerprint` 落入空串 hash 形同虚设). 新增对 `state='open' AND content_fingerprint=? AND target_line BETWEEN line±tolerance` 的已有建议命中 → 拦住"无 rule_keys + head_sha 已变 + 同 existing_code"的重复发布 (MR301: V2 重复 V1 同位置建议). `content_fingerprint` = `existing_code` normalize 后 sha256 前 16 位; 修复前从 `normalised.get("existing_code")` 取值恒为 None → 全部落入空串 hash; 修复后从 raw `existing_code` 取值, 并在 `_init_schema` 加回填迁移补齐历史记录.
3. `state='open'` 限定: 已 `applied` / `dismissed` / `superseded` 视为"已处理", 允许重新检视 (用户 push 改了内容让 auto_detect 标 applied, 然后又撤回原始内容 → 系统应能重新检视出新 issue)
4. `line_tolerance` 默认 2 行 (LLM 跨次 ±1~3 漂移容差), 设为 0 = 严格相等
5. **不限定 `head_sha`**: 跨 V1 / V2 / V3 同一 file:line 仍 dedup, 避免 GitLab 重复评论

`fingerprint` 是单条 suggestion 的精确指纹 (主键 dedup); `cohort_key` 是同类 bug 的聚合键 (兜底 dedup)。

### Head SHA 变化 → late_detect 翻 applied

`auto_detect_applied` 在 open 扫完后, 还会扫一遍 `state='resolved' + resolution_source='gitlab_resolve'` 的 suggestions, 跑同一套 exact_match / region_changed / token_fallback. 命中就翻 `applied` + 记一条 action=adopted, `adoption_source='late_detect'`, `validation_status='late-detect-apply'`. 原 resolved action 保留作历史.

Why (race condition):
    用户先在 GitLab UI 点「解决主题」关掉 discussion, 然后才 push commit 让代码落地. 当时 auto_detect 跑那一遍 exact_match 没命中 (head_sha 还停在 push 前) → 标 resolved. 后续 push 触发再跑时, 这条 suggestion 已不在 `list_open_suggestions()` 里, 永远不会被重检, 数据就一直停在"已关闭 (未分类)"但实际代码已采纳的状态.

Why 只扫 `gitlab_resolve`:
    - `/adopt` 流程虽然也会 resolve discussion, 但走的是 `adoption_source='adopt_command'`, 不应被覆盖回 applied (会丢失 /adopt 的语义).
    - `/dismiss` 状态是 dismissed 也不会进 resolved 集合.
    - 真正需要 late_detect 重扫的是 bot 因"代码没匹配上 + 讨论已关"误分类的那批, `resolution_source` 全是 `'gitlab_resolve'`.

历史数据修复:
    `python3 scripts/reconcile_late_detect.py --project-id <pid> --mr-iid <iid>`  
    `python3 scripts/reconcile_late_detect.py --all` (扫所有 MR)

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

完整测试 (`pytest tests/`，当前 452 passed / 0 failed — 不含与 telemetry 无关的预存在 fakeredis lock 测试 `test_lock_ttl_and_fence.py`):

**核心 telemetry / dedup / 状态机**

- `tests/test_improve_alignment.py` — 42 个对齐 / 缩进 / 多行替换测试
- `tests/test_suggestion_actions.py` — 16 个 /adopt /dismiss 测试
- `tests/test_telemetry_endpoints.py` — 5 个端点集成测试 (record_suggestion 扩展字段、dismissed_reason、by-rule 聚合、adoption_source 写入与 label)
- `tests/test_metrics_endpoint.py` — `/metrics/*` 端点
- `tests/test_last_activity_at.py` — 6 个 `last_activity_at` MAX 语义 / 回填测试
- `tests/test_supersede_stale_suggestions.py` — 6 个 head_sha 变化 supersede 测试
- `tests/test_supersede_stale_in_cohort.py` — cohort 维度 supersede
- `tests/test_auto_detect_applied.py` — auto_detect_applied 主流程
- `tests/test_auto_detect_race.py` — mid-scan dismiss race
- `tests/test_auto_detect_late_apply.py` — 21 个 late_detect 行为测试: `state='resolved' + resolution_source IN ('gitlab_resolve', 'publish_overview_reconcile')` 翻回 applied (含 race 修复、region_changed、token_fallback、cohort 归并、MR289 publish_overview_reconcile 路径)

**Batch / 增量修复回归**

- `tests/test_publish_overview_reconcile.py` — pre-reconcile + silent helper 行为 (10)
- `tests/test_sync_resolved_regression.py` — silent helper 重构后 sync_resolved 行为不变 (3)
- `tests/test_webhook_handler_fallback.py` — webhook handler 异常 fallback (4, commit 491a16f 回归)
- `tests/test_reconciler_loop.py` — reconciler 行为 (silent scan / 错误隔离 / 幂等, 7)
- `tests/test_reconciler_plist.py` — launchd plist 解析 + 配置正确 (8)
- `tests/test_process_adopt.py` — /adopt 完整流程 (7)
- `tests/test_process_dismiss.py` — /dismiss 完整流程 (5)
- `tests/test_build_overview_body.py` — 检视汇总 markdown 生成 (10)
- `tests/test_dedup_store.py` — Store dedup (fingerprint + line, 16)
- `tests/test_cohort_dedup.py` — cohort 聚合 (list_latest_by_cohort, 13)
- `tests/test_dedup_general.py` — 通用 dedup 行为
- `tests/test_adopt_race_recovery.py` — /adopt 并发竞态恢复
- `tests/test_apply_risk_check.py` — Apply 风险校验 (27)
- `tests/test_apply_risk_target_syntax_error.py` — 目标文件 SyntaxError 时不误报 '目标文件未定义 X' (11, MR299 + a5c6b72)
- `tests/test_list_latest_by_cohort.py` — cohort 归并 list_latest_by_cohort 行为回归 (9, MR299 + c070b06)
- `tests/test_command_chain_order.py` — 命令链顺序 (describe → improve)
- `tests/test_bot_loop_detection.py` — bot 循环检视防护
- `tests/test_parse_dt_formats.py` — GitLab 时间格式兼容
- `tests/test_resolve_section_title.py` — 检视汇总标题解析
- `tests/test_telemetry_section_render.py` — 周报 section 渲染
- `tests/test_rule_translate_xxx.py` — rule_keys 转换
- `tests/test_extract_action_compat.py` — /adopt /dismiss 提取兼容
- `tests/test_sync_qoder_agents.py` — qodercli agent 同步
- `tests/test_verify_e2e_smoke.py` — 端到端 smoke
- `tests/test_webhook_diff_head_lock.py` — webhook diff head 锁
- `tests/test_worker_runtime.py` — RQ worker 运行时
- `tests/test_llm_adapter.py` + `test_llm_markdown_unwrap.py` — LLM 调用兼容
- `tests/test_qodercli_plan_a_flags.py` + `test_qodercli_subprocess_fallback.py` — qodercli 路径
- `tests/test_lock_ttl_and_fence.py` — 分布式锁
- `tests/test_config_provider_defaults.py` — 配置默认值

e2e 流程 (基于 `codex/telemetry-e2e-20260730-224254` MR !134):

```bash
# 1) 触发 open webhook
curl -X POST http://127.0.0.1:3000/webhook \
  -H 'Content-Type: application/json' -H 'X-Gitlab-Token: ...' \
  -d '{"object_kind":"merge_request","event_type":"merge_request","project":{"id":34},
       "object_attributes":{"iid":134,"action":"open","state":"opened",
                           "source_branch":"codex/telemetry-e2e-20260730-224254",
                           "target_branch":"main","last_commit":{"id":"508c68aa"}},
       "user":{"username":"e2e-telemetry"}}'

# 2) /dismiss 触发 (note webhook)
curl -X POST http://127.0.0.1:3000/webhook \
  -H 'Content-Type: application/json' -H 'X-Gitlab-Token: ...' \
  -d '{"object_kind":"note","event_type":"note","project":{"id":34},
       "merge_request":{"iid":134},
       "object_attributes":{"id":..,"note":"/dismiss 误报",
                           "noteable_type":"MergeRequest","type":"DiffNote",
                           "discussion_id":"<note_id>"},
       "user":{"username":"e2e-runner"}}'

# 3) 查询
curl http://127.0.0.1:3000/api/v1/telemetry/mr/34/134/stats
curl http://127.0.0.1:3000/api/v1/telemetry/dismissals/by-rule?project_id=34
curl http://127.0.0.1:3000/api/v1/telemetry/mrs/34/134/dismissals
curl http://127.0.0.1:3000/api/v1/telemetry/weekly-reports
```

## Reconciler（周期性安全网）

GitLab 17.5 偶尔不发 "marked this discussion as resolved" webhook 给 note_events hook。`publish_overview` 顶部 pre-reconcile 已覆盖 "click 后还有 push /adopt /dismiss 等其他事件" 的场景；但 "纯 click-only 无任何后续事件" 的极端场景下没有事件触发 publish_overview，DB 会永远停在 `state='open'`。

两层防御：

1. **publish_overview pre-reconcile**（`commands/_common.py:520` 左右）—— 任何 `improve/adopt/dismiss/ui_apply/system_resolve` 路径调 publish_overview 前，先扫一遍 GitLab 把已 resolve 但 DB 还 open 的孤儿 catch-up
2. **周期 reconciler**（`reconciler/loop.py`）—— launchd StartInterval=60 秒扫一次全部 bot 跟踪的 open MR

### API

```python
from reviewagent.reconciler import reconcile_single_mr, reconcile_open_mrs

# 单 MR 扫描 (测试 / 手动跑)
result = reconcile_single_mr(project_id=34, mr_iid=247)
# {
#   "scanned": int,            # 扫了几条 open suggestions
#   "updated": int,            # 翻了几条 → resolved
#   "note_ids": list[str],     # 被翻的 note_id
#   "overview_refreshed": bool # 顶部汇总是否已刷新
# }

# 全量扫描
result = reconcile_open_mrs(project_id=None)  # None = 所有 bot 跟踪的 project
# {
#   "total_mrs": int,
#   "total_updated": int,
#   "mrs_updated": [{"project_id", "mr_iid", "scanned", "updated", "note_ids"}, ...],
#   "duration_s": float
# }
```

### CLI

```bash
# 手动跑一次
python -m reviewagent.reconciler.loop --project-id 34

# 全量
python -m reviewagent.reconciler.loop
```

### launchd 注册

```bash
cp scripts/com.jarvs.reviewagent.reconciler.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.jarvs.reviewagent.reconciler.plist
```

启动后日志写到 `/Users/jarvs/ReviewAgent/logs/reconciler.log`；幂等，连跑两次第二次 `updated=0`。

### 故障隔离

单个 MR reconcile 失败不影响其他 MR（每个 MR 独立 try/except）；`publish_overview` 失败仅 warning，不阻塞 DB 更新。

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
