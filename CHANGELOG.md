# Changelog

格式参考 [Keep a Changelog](https://keepachangelog.com/)，版本号遵循语义化版本。
本文件记录**用户可见**的变更；内部重构细节见 `.superpowers/sdd/` 下的任务记录。

## Unreleased

### Changed

- **文档与代码对齐**：README / QUICKSTART / DEPLOYMENT / LLM_PROVIDER_ADAPTER 补充
  `qoder-sdk`、场景包 prompt 路径（`profiles/<profile>/prompts/`）、`deploy.sh`
  十阶段与各配置键的平面归属/必填范围；`telemetry` README 修正 `triggered_by` 取值、
  credits 来源与测试计数；`instance.env.example` 的 `LOG_ROTATION` 可选值改为
  `single|none`。
- **prompt 只保留场景库一份**：删除 `reviewagent/prompts/` 兼容门面（`loader.py`
  与 6 个与场景库逐字节重复的 prompt）及场景库中已失效的
  `prompts/_general_rules_block.md`。prompt 统一由 `reviewagent/scenario/prompts.py`
  按 `profile.prompts` 解析；通用规则正文的唯一来源是 RuleSet 的 `verbatim_body`
  （`_general_rules_text` 不再回退旧 loader，resolver 异常直接抛出）。
- **仓库规则改为运行时动态发现**：不再写死 `.agents/rules` 目录与 `SSD` 前缀。
  从入口文件（默认 `AGENTS.md`）跟随 Markdown 链接发现规则索引与规则文件，
  `index.md` 自动展开同目录兄弟文件，目录链接展开一层（深度 ≤2、≤60 个文件、
  单文件 ≤512KB）；运行时生成 `<rule_index>`（键/级别/标题），截断时模型仍能拿到
  完整键清单。`REPO_CONTEXT_RULES_DIR` 改为可选显式覆盖（默认空=自动发现），
  `RULE_KEY_PREFIX=auto` 识别任意 `<大写前缀>-RULE-<键>`。仓库规则新增/删除/改写
  无需改工具代码；规则来源随被检仓库默认分支实时生效。
- **场景配置两层收敛（删除 preset/extends 层）**：`profiles/_presets/` 与
  `profile.yaml` 的 `extends` 继承机制移除——其全部取值与 schema 默认值逐项相同，
  属纯重复定义。生效优先级收敛为 **schema 默认值 <
  `config/profiles/<name>/review.yaml` < env**；`profile.yaml` 只保留身份 + 内容键
  （含从 preset 提升的顶层 `test_paths`），调参组写入 `profile.yaml` 会启动报错并
  提示迁移；`_shared/` prompt 兜底目录一并删除。`config show --sources` 来源标签
  收敛为 `env / config / profile / default`。

### Added

- **`/readyz` 回显生效场景包指纹**：200 响应新增 `profile` / `profile_fingerprint`
  （服务进程实际加载的内容），可与磁盘当前指纹核对；全量档 E2E 预检据此新增
  `profile.freshness` 检查——改完 prompts/profile 忘记重启本地栈时立即失败并提示
  重启，而不是等队列 job 被 `job.profile_mismatch` 拒绝后超时。
- **`profile list` / `profile switch` CLI**：`reviewagent profile list` 列出可用
  场景包（`*` 标记当前生效）；`reviewagent profile switch <name>` 校验目标场景包
  后原位改写 `instance.env` 的 `REVIEW_PROFILE` / `REVIEWAGENT_DATA_DIR`（保留注释
  /顺序/权限，原子替换），目标数据目录已绑定其它场景包时拒绝执行，并按平台打印
  重启提示——换场景不再需要手改 env 文件。
- **`config/` 用户配置目录**：用户改配置只需看 `config/`。`config/instance.env`
  （实例平面，含密钥、不入库）承载 GitLab / LLM / Redis / 周报投递；
  `config/profiles/<name>/review.yaml`（场景平面，可入库）覆盖检视范围 / 打分 /
  限额 / 上下文；模板见 `config/examples/instance.env.example`，配置地图见
  `config/README.md`。只写非默认值，注释必须单独成行（systemd 不剥离行尾 `#`）。
- **场景包（profile packs）**：`profiles/` 下的 profile 包（`profile.yaml` + prompts +
  规则 + 动态规则）在启动时装配，由 `REVIEW_PROFILE` 选择；新增场景只需加数据，
  不改引擎代码。首期内置 `ssd-python-autotest`。
- **单一配置 schema 与三平面**：`reviewagent/settings/schema.py` 是默认值的唯一来源，
  `binder` 是唯一 env 解析入口。实例平面（`config/instance.env`）/ 场景平面（profile
  + `review.yaml`）/ 规则平面（规则包）分离；`reviewagent config check` / `config show`
  提供启动前校验与生效值核对（来源新增 `config`）。周报的 16 个 env 并入同一 schema
  （短名 `DINGTALK_WEBHOOK` 保留为别名）。
- **静态 / 动态规则拆分**：静态规则包支持 `verbatim` 与 `structured` 两种渲染、
  键命名空间校验、共享包语言声明；动态规则（`DYN-*`）按 provider 加载并 fail-fast 校验。
  `reviewagent rules check` 输出规则索引与 `ruleset_fingerprint` / `render_checksum`。
- **日志治理**：`LOG_FORMAT=text|json`（json 为单行机读，含脱敏 header dump）、
  `LOG_ROTATION=single|none`；启动日志固定输出 `config.resolved` / `bootstrap.ready`
  （含 `profile_fingerprint` / `config_fingerprint` / 规则指纹）。
- **运行时一致性**：RQ job manifest 携带 profile / 配置 / 规则 / 动态规则指纹，
  worker 接受 job 前校验；`/readyz` 汇总 profile / RuleSet / instance / SQLite / Redis。
- **运维文档与演练**：`docs/CONFIG.md`（生成）、`docs/RULES.md`（最小规则包模板，
  示例由 CI 实际解析）、`docs/BACKUP_RESTORE.md`（`VACUUM INTO` 备份 + drain + 恢复校验）；
  `tests/runtime/test_ops_drill.py` 覆盖备份恢复往返与孤儿 run 清扫。
- 实例配置模板迁到 `config/examples/instance.env.example`（原 `.env.example` 移除），
  其余字段指向 `docs/CONFIG.md`。
- **统一端到端验证**：`scripts/e2e/run_e2e.py` 提供两档验证——`--tier fast` 在进程内
  跑完整配置生效性审计（92 个 schema 字段逐条 witness + 21 组行为 delta，无需外部
  服务），`--tier full` 用临时 GitLab 项目/分支/MR 跑 describe、improve、自动链、
  adopt/dismiss、UI Apply 识别、Telemetry API、周报共 8 个功能，并用真实 suggestions
  断言限额/严重度/规则键等配置确实作用到产物。全量档新增真实 diff 规划断言
  （`scripts/e2e/real_config.py`）：用 MR `/changes` 与抽样分支的 compare 数据直接跑
  `planner.plan()`，逐条证明 `review_mode`、`priority_paths`、`full_files`、
  `test_sample_*` 在真实文件集合上生效，并从真实仓库拉取 `AGENTS.md` + rules_dir。
  报告落在 `logs/e2e/report-*.json`；`verify_e2e.py` 保留为全量档兼容入口。
- **遥测字段级审计**：全量档新增 `scripts/e2e/telemetry_audit.py`，在功能用例跑完后
  用真实 GitLab 产物 × DB 直读 × 读接口三方对账，输出
  `telemetry:mr_activity` / `review_runs` / `suggestions` / `actions` / `file_state` /
  `api_params` 6 项断言（字段逐条覆盖见 `reviewagent/telemetry/README.md`）。
  E2E 不再在 auto_chain 前清空 `review_runs`（默认上限 30 本就够用），保留
  "哪个 run 产出了哪些 suggestion" 的证据；空 `improved_code` 按删除整段的合法
  形态校验（`existing_code` 非空 + note 含 suggestion 块），非空时与 note 正文比对。
- **一键部署与部署体检**：`deploy.sh` 幂等完成 venv / Redis / 数据目录 / unit 安装 /
  `enable --now`（开机自启）/ 部署后验证，支持 `--check` 与 `--no-verify`；
  新增 `deploy/preflight.sh`（systemd、python、磁盘、端口、密钥、GitLab API、Redis、
  目录属主、config/rules check）与 `deploy/verify.sh`（is-enabled、is-active、
  `/health`、`/readyz`、telemetry、API 与 worker 的配置指纹一致性）。
- **worker 模板与常驻 reconciler**：`reviewagent-worker@.service` 按 `RQ_WORKER_COUNT`
  实例化（`@1..N`，调小后自动下线多余实例）；新增 `reviewagent-reconciler.service`
  + `.timer`（开机 2min 后每 60s 同步 ✓ 状态并清扫孤儿 running），Linux 生产不再依赖
  macOS launchd。

### Changed

- `improve` 拆分为 planner / assembly / context / runner / validation 分层，Publishing
  按 profile 渲染；prompt golden 逐字节保持，检视效果不变。
- 周报标题默认值改由 `profile.reporting.weekly_title` 提供（`REVIEWAGENT_WEEKLY_REPORT_TITLE`
  为空时生效），env 覆盖行为不变。
- 配置文件系统布局统一到 `REVIEWAGENT_DATA_DIR`（生产 `/var/lib/reviewagent`）；
  systemd unit 增加 `REVIEW_PROFILE` / `REVIEWAGENT_DATA_DIR` 与
  `ExecStartPre=... reviewagent config check`。
- **systemd worker 改为 env 驱动**：队列名与 Redis 地址在 `ExecStart` 里从
  `config/instance.env` 展开（`$${RQ_QUEUE_NAME:-review}` / `$${REDIS_URL:-...}`，
  周报队列缺省派生 `<主队列>-weekly`），unit 不再硬编码 `review` / `6379`；
  `TimeoutStopSec=1300` ≥ `RQ_WORKER_TIMEOUT`，重启不再 SIGKILL 执行中的 job。
- `deploy.sh` 首次引导改用 `config/examples/instance.env.example`（`.env.example`
  已删除），venv 与数据/工作树目录统一以服务账号创建，Redis 端口跟随 `REDIS_URL`。
- 日志与运维文档的事件名以代码为准：`chain.lock_timeout`、`improve.record_suggestion failed`。
- 非法配置值改为启动即失败（`config check` / binder 校验），不再静默回退默认值。
- `review.yaml` 支持嵌套键校验，并开放 `repo_context` 与 `reporting`（仅
  `weekly_title` / `repo_scan_*` 展示类键）两组；env 压过 `review.yaml` 时记录
  `config.env_shadowed` 告警。

### Fixed

- **仓库规则表接入运行期 RuleSet**：此前仓库规则只作为 prompt 文本注入，
  `parse_repo_overlay` / `RuleSet.with_repo` 仅测试覆盖。现在检视时把规则文件
  中的结构化表格解析为请求级规则（同键覆盖场景规则的 severity/title，新键按
  priority 1000 追加），`ruleset_fingerprint` 追加 `:repo:<摘要>`——仓库规则
  改写会使增量复用失效并触发整文件重检；自由文本仍只作为 prompt 上下文，
  场景规则正文（`verbatim_body`）与渲染字节不变。规则读取失败降级为基础
  规则集（`improve.repo_overlay_failed` WARNING），不阻塞检视。
- **数据采集补漏**：`review_runs.rule_keys_cited` / `suggestion_count`、
  `suggestions.score` / `severity_source`、`suggestion_actions.head_sha_posted`
  现在由真实 improve / adopt / dismiss 链路写入；webhook upsert 不再把事件操作者
  当成 MR 作者，`[no_more_review]` 标记不再污染 `mr_activity.title`，真实 webhook
  的 `created_at` / `updated_at` / `merged_at` 正确落库。
- **遥测读接口参数**：`/runs` 与 `/dismissals` 的 `total` 改为过滤条件整表计数
  （原为当前页大小）；`/mrs` 时间窗口改按 `created_at` 并支持 `until`
  （原按 `updated_at`，窗口外 MR 会泄漏）；`/dismissals` 的 `rule_key` 匹配
  `suggestions.rule_keys`、`until` 按 `dismissed_at`、`offset` 生效；
  `/metrics/rules` 改按真实规则键聚合（原按 severity 兜底）；
  `/metrics/authors` 合并 run 计数与 MR 计数。

- 三个"改了不生效"的配置接线：`REVIEWAGENT_WEEKLY_TIMEZONE` 现在真正参与周报周边界
  计算（非法 IANA 名启动失败）、`REVIEWAGENT_WEEKLY_CRON_SCHEDULE` 由
  `services_ops.sh` 渲染为 systemd timer drop-in、`review_config_dir` 的 Config 值
  （非仅 env）也参与用户配置目录解析。
- worktree 路径增加每次运行唯一的后缀，并在创建前清理超过 6 小时的孤儿目录；
  链锁丢失导致同一 MR/SHA 的并发链不再互相删除工作区（表现为 `worktree 不存在`
  与 `output not JSON` 的 describe 失败）。
- **部署脚本三处失败路径**：首次引导引用已删除的 `.env.example`；成功路径因未初始化
  的 `MISSING_DEPLOY_UNITS` 在 `set -u` 下退出 1；`/tmp/reviewagent-worktrees` 以 root
  创建导致 worker `permission denied`（现统一 `install -d -o workflow`）。
- **单 worker 与并发配置脱节**：旧 `reviewagent-worker.service` 只起 1 个进程而
  `RQ_WORKER_COUNT=3`；升级时自动 `disable --now` 旧 unit，改由模板实例承接。
- 清理 `scripts/` 下 5 个写死在已失效开发机路径上的一次性脚本（provider 验证脚本等），
  文档引用同步更新。

### Known limits

- `verify_job` **不比较** `dynamic_rules_fingerprint`：动态规则热改不会触发 job 校验，
  修改动态规则后需重启 API 与 worker 并重新入队。
- api / worker / weekly / reconciler 入口**尚未接线 `bind_context`**（spec §8.1 的
  entry binding 未完成）：部分入口日志缺少结构化上下文字段。
- **磁盘水位拒绝（`E_STORAGE`）、API drain 端点、secret 轮换后自动重投未实现**
  （本期明确不做）：备份/恢复的 drain 按 `docs/BACKUP_RESTORE.md` 手写步骤执行；
  磁盘需外部监控。
- **反向上线顺序风险**：升级必须「先新 worker、后新 API」。旧 worker 不识别新 manifest
  kwargs，若先上新 API，排队中的 job 会因 `unexpected keyword argument` 失败；
  回滚顺序相反（先回 API，再回 worker）。
- 文档中的事件名以代码为准：`chain.lock_timeout`、`improve.record_suggestion failed`
  （spec 早期草稿的 `review.lock_timeout` / `telemetry.write_fail` 为旧命名，
  日志与运维文档不再使用）。
