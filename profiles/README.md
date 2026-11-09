# 场景包（profiles/）

本目录是**场景包作者领域**：一个子目录 = 一个完整检视场景（提示词 + 规则 + 能力开关）。
日常调参不要改这里 —— 用户配置的唯一入口是 `config/`（见 `config/README.md`）。

```
profiles/
└── ssd-python-autotest/            # SSD 自动化测试 Python 检视
    ├── profile.yaml                # 身份 + 内容清单（见下）
    ├── prompts/                    # 提示词模板（frontmatter + 正文）
    │   ├── improve.md / describe.md      # 主检视 / MR 描述（profile.prompts 登记）
    │   ├── weekly_*.md                   # 周报三件套（profile.prompts 登记）
    │   ├── chunk.md / batch.md           # 单文件 / 多文件批次整体框架（固定路径读取）
    │   ├── rules_repo.md / rules_general.md  # 规则块（有 / 无仓库规则，固定路径读取）
    │   ├── source/                       # 源码上下文块（patch/full/partial…）
    │   └── dynamic/                      # 动态规则上下文块（cross_file_refs…）
    ├── rules/general_rules.md      # 规则条文（正文逐条解析为规则键）
    ├── dynamic_rules.yaml          # 动态规则声明（grep/AST 探针）
    └── reporting/rule_actions.yaml # 规则键 → 处置动作映射
```

`profile.yaml` 的 `prompts` 只登记 `improve` / `describe` / 周报三件套；`chunk.md` /
`batch.md` / `rules_*.md` 与 `source/`、`dynamic/` 由 `review/assembly.py` 和
`review/context.py` 按固定路径读取，不要写进 `prompts` 映射。

## profile.yaml 只放"身份 + 内容"

允许的键（写了别的键启动即失败）：

| 键 | 作用 |
| --- | --- |
| `schema_version` / `name` / `description` / `language` | 身份 |
| `test_paths` | 测试文件判定（GenericPolicy 命中即按测试代码策略检视） |
| `prompts` | 提示词文件清单（`improve` / `describe` 必填） |
| `rules` / `dynamic_rules` / `rule_keys` / `rule_key_promotions` / `rule_overrides` / `disable` | 规则装配 |
| `capabilities` / `security` | 能力与安全开关 |
| `reporting` | 仅 `actions_file`（包内代码路径引用，不开放用户覆写） |

**调参组（`file_filter` / `scoring` / `limits` / `context` / `repo_context`，以及 `reporting`
的展示/扫描类键 `weekly_title` / `repo_scan_extensions` / `repo_scan_entry_files`）不允许出现在
这里** —— 它们属于 `config/profiles/<name>/review.yaml`。写错位置会在启动时报错并提示迁移。
`preset`/`extends` 继承层已删除：场景包之间不共享基类，公共默认值统一在
`reviewagent/settings/schema.py`。

## 新增一个场景包

1. 复制 `ssd-python-autotest/` 为新目录，改 `name` / `description` / `language`，
   替换 prompts 与 rules 内容（`improve` / `describe` 两个 prompt 必须存在）。
2. 需要差异化调参时，建 `config/profiles/<新场景名>/review.yaml` 只写差异值。
3. 自检 + 入口登记：

```bash
.venv/bin/python -m reviewagent rules check --profile <新场景名>
.venv/bin/python -m reviewagent profile list
```

4. 切换到新场景做验证：`.venv/bin/python -m reviewagent profile switch <新场景名>`，
   再按提示重启服务。
