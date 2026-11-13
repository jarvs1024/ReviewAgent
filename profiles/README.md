# 场景包（profiles/）

本目录是**场景包作者领域**：一个子目录 = 一个完整检视场景（提示词 + 规则 + 能力开关）。
日常调参不要改这里 —— 用户配置的唯一入口是 `config/`（见 `config/README.md`）。

```
profiles/
└── ssd-python-autotest/            # SSD 自动化测试 Python 检视
    ├── profile.yaml                # 身份 + 内容清单（见下）
    ├── prompts/                    # 提示词模板
    │   ├── improve.md / describe.md      # 主检视 / MR 描述（profile.prompts 登记）
    │   ├── weekly_*.md                   # 周报三件套（profile.prompts 登记）
    │   └── blocks/                       # 内部装配片段（固定路径读取）
    │       ├── review.md                 # chunk / batch / rules_general / rules_repo
    │       ├── source.md                 # full / partial / patch 等源码上下文变体
    │       └── dynamic.md                # cross_file_refs 空 / 有引用
    ├── rules/                      # 规则相关全部在此
    │   ├── noise_boundary.md       # 「不报什么」降噪边界（priority 900，先于正向规则注入）
    │   ├── general_rules.md        # 静态规则条文（正文逐条解析为规则键）
    │   ├── dynamic_rules.yaml      # 动态规则声明（grep/AST 探针）
    │   └── rule_actions.yaml       # 规则键 → 周报跟进动作（默认路径，可选）
```

`profile.yaml` 的 `prompts` 只登记 `improve` / `describe` / 周报三件套。
`prompts/blocks/*.md` 是**内部装配片段**：不写进 `prompts` 映射、不会被
`sync_qoder_agents` 物化成 `.qoder/agents/*.md`，由 `review/assembly.py` 与
`review/context.py` 按固定路径读取。

### 内部片段的分节约定

一个 blocks 文件可以承载多个片段，按 `<!-- @section:<name> -->` 分节（标记必须
独占一行）。代码用 `路径#section` 引用，例如：

```yaml
# rules/dynamic_rules.yaml
empty_template: prompts/blocks/dynamic.md#cross_file_refs_empty
```

- 段正文从标记行换行后开始，到下一个标记行行首（或 EOF）结束；
- 段间空行属于**上一段正文**（`rules_general` / `cross_file_refs_*` 依赖这个尾空行）；
- 占位符校验、路径逃逸校验与普通模板一致（`scenario.prompts.load_section`）。

## profile.yaml 只放"身份 + 内容"

允许的键（写了别的键启动即失败）：

| 键 | 作用 |
| --- | --- |
| `schema_version` / `name` / `description` / `language` | 身份 |
| `test_paths` | 测试文件判定（GenericPolicy 命中即按测试代码策略检视） |
| `prompts` | 提示词文件清单（`improve` / `describe` 必填） |
| `rules` / `dynamic_rules` / `rule_keys` / `rule_key_promotions` / `rule_overrides` / `disable` | 规则装配 |
| `rule_actions` | 规则键 → 周报跟进动作的 YAML 路径（缺省 `rules/rule_actions.yaml`） |
| `capabilities` / `security` | 能力与安全开关 |

规则动作映射（规则键 → 周报跟进动作）由 `profile.yaml` 的 `rule_actions` 指向，
缺省读取 `rules/rule_actions.yaml`；不同场景包可指向不同文件。它是唯一来源——
周报渲染与 LLM user prompt 均从此文件注入；文件存在即加载并计入 profile 指纹，
显式声明但文件缺失会启动失败，未声明且默认文件缺失时周报回退到规则名兜底。

**调参组（`file_filter` / `scoring` / `limits` / `context` / `repo_context`，以及
`rule_keys.repo_prefix` 和 `reporting` 的展示/扫描类键 `weekly_title` /
`repo_scan_extensions` / `repo_scan_entry_files`）不允许出现在
这里** —— 它们属于 `config/profiles/<name>/review.yaml`。`rule_keys` 的其余键
（`builtin_pattern` / `free_prefixes`）仍是场景包内容。写错位置会在启动时报错并提示迁移。
`preset`/`extends` 继承层已删除：场景包之间不共享基类，公共默认值统一在
`reviewagent/settings/schema.py`。

## 新增一个场景包

1. 复制 `ssd-python-autotest/` 为新目录，改 `name` / `description` / `language`，
   替换 prompts 与 rules 内容（`improve` / `describe` 两个 prompt 必须存在；
   内部装配片段在 `prompts/blocks/`，按 `<name>` 分节替换文案）。
2. 需要差异化调参时，建 `config/profiles/<新场景名>/review.yaml` 只写差异值。
3. 自检 + 入口登记：

```bash
.venv/bin/python -m reviewagent rules check --profile <新场景名>
.venv/bin/python -m reviewagent profile list
```

4. 切换到新场景做验证：`.venv/bin/python -m reviewagent profile switch <新场景名>`，
   再按提示重启服务。
