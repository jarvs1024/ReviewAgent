# Changelog

格式参考 [Keep a Changelog](https://keepachangelog.com/)，版本号遵循语义化版本。
本文件记录**用户可见**的变更；内部重构细节见 `.superpowers/sdd/` 下的任务记录。

## Unreleased

### Added

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

### Changed

- `improve` 拆分为 planner / assembly / context / runner / validation 分层，Publishing
  按 profile 渲染；prompt golden 逐字节保持，检视效果不变。
- 周报标题默认值改由 `profile.reporting.weekly_title` 提供（`REVIEWAGENT_WEEKLY_REPORT_TITLE`
  为空时生效），env 覆盖行为不变。
- 配置文件系统布局统一到 `REVIEWAGENT_DATA_DIR`（生产 `/var/lib/reviewagent`）；
  systemd unit 增加 `REVIEW_PROFILE` / `REVIEWAGENT_DATA_DIR` 与
  `ExecStartPre=... reviewagent config check`。
- 日志与运维文档的事件名以代码为准：`chain.lock_timeout`、`improve.record_suggestion failed`。
- 非法配置值改为启动即失败（`config check` / binder 校验），不再静默回退默认值。

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
