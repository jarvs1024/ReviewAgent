# 通用检视场景包架构（Profile-based Review）设计文档

- 日期：2026-11-07
- 分支：new_review_v2（基于 main 新建）
- 状态：已与需求方分段确认；已按完整性核查与分层评审补齐；首期仅 Python 场景（C/C++ 仅预留接口），待审阅

## 1. 背景与问题

ReviewAgent 当前是「SSD 自动化测试（Python）MR 检视」专用系统：

- 20 条 R-* 通用规则硬编码在 `prompts/_general_rules_block.md`，带大量 SSD 硬件 / pytest 场景假设；
- prompt（`prompts/improve.md`、`describe.md`）及三个 weekly prompt 中角色、示例、缩进约定均写死 Python 测试语境；
- 语言假设散落在核心代码：`commands/improve.py` 中的 ast 符号校验（:1975-2300）、`rg -t py` 跨文件检索（:718,795）、pytest 关键词密度打分（`config.py:140-149`）、4 空格缩进约定（prompt :323）、规则键正则写死 `SSD-RULE-`（:86-96）；
- `commands/improve.py` 为 3354 行巨石模块，prompt 装配、调度、校验、发布、去重全部耦合；
- 周报链路除规则解析外仍写死：标题（`reporting/config.py:50,79`、`artifact.py:49,80,103`）、规则→ruff 动作映射（`renderer.py:392-399`）、代码文件仅 `.py`（`repo_scan.py:298,324`）、三个 weekly prompt 的 SSD 语境；
- 规则知识三处散落：prompt Markdown、`repo_context.py`（仓库规则）、`rule_translate.py`（反向正则解析规则名），换场景必然漏改；
- 配置纯环境变量，无场景切换概念。

目标：框架不变、场景可变——**切换场景包即可适配不同检视场景与检视质量**，规则可独立配置，架构可扩展。

## 2. 需求决策（已确认）

| 决策点 | 结论 |
|---|---|
| 场景切换作用域 | **部署实例级**：一个实例服务一种场景，环境变量选择 |
| C/C++ 检视结果形态 | 保留**可 Apply 的 GitLab suggestion**（同现有）；首期不实施 |
| 现有 Python 检视兼容性 | **行为严格兼容**：现有测试全绿、断言零改动、prompt/规则逐字节迁移 |
| 规则配置形态 | **Markdown 规则包** + 可解析元数据（frontmatter / 规则表 / 正反例） |
| 规则层 | **独立子系统**：完整模型（RulePack/Rule/RuleSet）、四层来源装配、适用性过滤、覆盖/禁用、键索引 |
| 仓库自定义规则 | **独立模块**处理被检仓库 AGENTS.md / `.agents/rules`，优先级第一 |
| 规则正反例 | **支持**：每条规则可选附带反例/正例，按 prompt 预算注入 |
| Profile 语义 | **场景包**（非语言包）：语言只是属性，同语言可有多场景 |
| `REVIEW_PROFILE` 未设置 | **默认回退 `ssd-python-autotest` 并记 warning**；显式设置了不存在的值才启动失败 |
| 测试耦合处理 | **兼容门面**：`ImproveCommand` 保留同名私有方法（仅委托）与 `config` 属性，136 处测试耦合零改动 |
| 检视数据存储 | **一实例一库**：文档强制 `REVIEWAGENT_DATA_DIR` 按实例隔离，schema 不改 |
| 周报场景化 | **全量纳入场景包**：三个 weekly prompt、规则→动作映射、repo_scan 扩展名、标题 |
| env 与 profile 优先级 | **显式 env > profile.yaml > 内置默认值** |
| 首期实施范围 | **仅 Python 场景**：C/C++ 仅预留接口与设计，稳定后再实施 |
| 分层修正 | 新增 `scenario/` 共享层；`review/` 拆 planner/context/runner；`reporting` 不依赖 `review` |

## 3. 非目标

- 不做仓库级 profile 自动发现（实例级已确认；仓库级留作后续演进）。
- 不做规则 DSL / 规则引擎执行（规则只描述，由 LLM 判读）。
- 不做 C/C++ AST 级符号校验。
- **首期不实施 C/C++ 场景**：`cpp-dev` 场景包、`policies/cpp.py`、C++ 周报内容、`repo_scan` 的 C/C++ 扩展名均推迟；本期只保证接口可承载。
- 不改 webhook / RQ / GitLab / LLM provider 的外部接口；telemetry 表结构不变（靠一实例一库隔离场景）。

## 4. 分层架构

### 4.1 六层与依赖方向

```
L5 入口层      webhook/ · api/
L4 命令任务层  commands/ · workers/
L3 检视引擎层  review/（planner · context · assembly · runner · policy · validation · publishing）
L2 场景装配层  scenario/（profile · prompts · rules 子系统）
L1 场景数据层  profiles/（profile.yaml · prompts/ · rules/）
L0 平台层      config · logging · metrics · telemetry · gitlab · llm · git（凭据与运行参数）
L6 报告层      reporting/（依赖 L1/L2；禁止 import review）
```

依赖规则（单向）：

- `commands/workers → review → scenario → L0`；
- `reporting → scenario`（只消费 RuleSet/Profile，不碰检视引擎）；
- `scenario → L0`；`review` 不得被 `scenario` 反向依赖；
- 入口层只依赖命令/任务层。

### 4.2 目标目录

```
reviewagent/
├── profiles/                     # L1 场景数据（纯数据）
│   ├── _shared/rules/            # 共享领域规则库
│   └── ssd-python-autotest/
│       ├── profile.yaml
│       ├── prompts/improve.md, describe.md, weekly_*.md
│       └── rules/*.md
├── scenario/                     # L2 场景装配
│   ├── profile.py                # 加载/校验/env 合并/单例
│   ├── prompts.py                # 原 prompts/loader，按 profile 解析模板
│   └── rules/                    # 规则层（独立子系统）
│       ├── model.py              # RulePack / Rule / RuleSet
│       ├── parser.py             # Markdown 包解析
│       ├── resolver.py           # 四层来源装配/覆盖/禁用/适用性
│       ├── render.py             # prompt 渲染（仓库规则置顶、预算裁剪）
│       ├── keys.py               # 规则键正则生成与索引
│       └── repo.py               # 被检仓库规则获取（原 repo_context）
├── review/                       # L3 检视引擎
│   ├── planner.py                # 文件过滤/打分/抽样/strategy/分批（纯计算）
│   ├── context.py                # full/partial/patch 源块、跨文件证据、锚点块
│   ├── assembly.py               # prompt 纯函数装配（不调 LLM）
│   ├── runner.py                 # 并行 chunk 执行、合并、预算控制
│   ├── policy.py                 # LanguagePolicy 协议 + 注册表（首期仅 python）
│   ├── policies/python.py
│   ├── policies/cpp.py           # 预留文件，首期不实现
│   ├── validation.py             # 行号校验、锚定、去重指纹、风险判定
│   └── publishing.py             # inline suggestion + 汇总评论（合并 publish_overview）
├── commands/
│   ├── improve.py                # 薄壳 + 兼容门面（委托）
│   └── _common.py                # 通用命令生命周期（telemetry/workspace/异常）
├── repo_context.py               # 兼容 re-export（实现迁入 scenario/rules/repo.py）
├── prompts/loader.py             # 兼容 re-export（实现迁入 scenario/prompts.py）
└── reporting/                    # L6，依赖 scenario
```

## 5. Profile（场景包）规范

### 5.1 目录与选择

- 场景 = 一个目录，含 `profile.yaml` + `prompts/` + `rules/`。
- 实例选择：环境变量 `REVIEW_PROFILE`（场景目录名或绝对路径）。
  - 未设置 → 载入 `ssd-python-autotest`，日志 WARNING；
  - 设置为不存在的目录 → 启动即失败，列出可用场景及 `description`；
  - 必填字段缺失 / `language` 未注册 → 启动即失败，列出缺失字段。
- 加载时机：`scenario/profile.py` 单例，由 `config.py` 加载后、API `main.py` lifespan、`rq_worker.py` bootstrap 三处共同触发。
- **双进程一致性**：RQ job 载荷新增 `profile` 字段；worker 执行前校验与自身场景一致，不一致标记失败并记录日志。

### 5.2 profile.yaml 示例

```yaml
name: ssd-python-autotest
description: SSD 自动化测试 Python 代码检视（现有行为）
language: python
prompts:
  improve: prompts/improve.md
  describe: prompts/describe.md
  weekly_inspection_summary: prompts/weekly_inspection_summary.md
  weekly_change_summary: prompts/weekly_change_summary.md
  weekly_quality_scan: prompts/weekly_quality_scan.md
rules:                          # 场景规则包（按顺序，frontmatter 决定优先级）
  - rules/general_rules.md
  - ../_shared/rules/ssd_storage.md
rule_overrides:                 # 场景级调参（可选）
  R-LOG: {severity: high, weight: 1.2}
disable: []                     # 场景级禁用规则键
repo_context:                   # 仓库规则（第一优先）
  files: [AGENTS.md]
  rules_dir: .agents/rules
  max_lines: 500
rule_keys:
  repo_prefix: SSD-RULE         # 仓库规则键前缀
  display_prefix: SSD           # 周报分类前缀
capabilities:
  ast_validation: true
  cross_file_search: true
  apply_suggestion: true
  full_source_analysis: true
file_filter:
  include_extensions: [.py]
  skip_paths: [conftest.py, tests_loc/, /pages/, /locators/]
  priority_paths: [/pages/, /locators/]
scoring:
  feature_keywords: []          # 取自现 config.improve_test_feature_keywords
  test_paths: [tests/, test_*.py]
  priority_weights: {}          # 取自现 config.improve_priority_weight_*
  review_mode: auto
  test_sample_paths: []
  test_sample_max: 3
reporting:
  weekly_title: "SSD自动化代码检视周报"
  rule_actions: {}              # 规则键→建议动作，替代 renderer 写死 ruff 映射
  repo_scan_extensions: [.py]
  repo_scan_entry_files: [__init__.py, main.py]
```

### 5.3 prompt 模板

- 沿用现有 frontmatter 格式（name / output_schema / tools），`scenario/prompts.py` 保留加载机制，路径改为按 profile 解析；`describe` 与三个 weekly agent 一并纳入映射（未配置回退 `_shared/` 默认模板）。
- 正文使用 `{{placeholder}}` 变量替换：diff 块、源码块、规则渲染块、仓库规则块、profile 元信息；占位符缺失/多余 → 报错带 profile 名与模板路径。
- 现有 prompt 文件**逐字节迁入** `ssd-python-autotest`；`improve.py` 中内联的仓库规则段（:858-885, :940-951）与 "Python 端已 grep" 文案改为模板变量。
- `scripts/sync_agents.py` / `sync_qoder_agents.py` 改为读取当前 profile 的 prompts 目录。

### 5.4 env 与 profile 合并规则

- 顺序：显式环境变量 > `profile.yaml` > 代码内置默认值。
- 迁往 profile 的 config 字段保留 env 读取；profile 加载器把「已显式设置的 env」覆盖到 profile 值上，`config` 对象字段指向合并结果，现有部署不改 env 行为不变。
- 现有两套默认值矛盾处（`improve_review_mode`、优先级权重）统一以 profile 值为准；`config.from_env()` 的 env 往返测试保持通过。

## 6. 规则层（独立子系统）

规则层决定检视效果与质量，与 prompt、语言策略解耦。规则包是数据，解析/装配/渲染是纯逻辑，可独立测试。

### 6.1 两类规则、四层来源

| 来源 | 位置 | 默认优先级 | 说明 |
|---|---|---|---|
| 仓库规则 | 被检仓库 AGENTS.md / `.agents/rules` | **1000（最高）** | 仓库自带规范，冲突时覆盖工具规则 |
| 场景覆盖 | `profile.yaml` 的 `rule_overrides / disable` | 900 | 场景微调严重度/权重/禁用 |
| 场景规则包 | profile `rules/*.md` | 包 frontmatter（如 800） | 决定该场景检视重点的主体 |
| 共享规则库 | `profiles/_shared/rules/*.md` | 包 frontmatter（≤500） | 领域规则多场景复用 |

### 6.2 数据模型

```python
Rule(
    key: str,              # 稳定契约：R-LOG / CPP-R-LEAK / SSD-RULE-XXX
    title: str,
    body: str,             # Markdown 详细说明
    severity: str,         # high/medium/low（可被覆盖）
    weight: float,         # 打分权重（可被覆盖）
    applies_to: dict,      # {languages: [...], paths: [...]}
    examples: list,        # [{"kind": "bad"|"good", "code": str, "lang": str}]
    source: dict,          # {kind: repo|profile|shared, path, pack_id}
    priority: int,
)
RuleSet(rules, index_by_key, key_pattern, sources)   # 装配后的唯一事实源
```

`RuleSet` 一路传给 `review` 的 planner/assembly/validation/publishing 与 `reporting`，各层不再自行解析规则文件或硬编码前缀。

### 6.3 规则包格式（Markdown + 可解析元数据）

````markdown
---
id: ssd-python-autotest/core
title: SSD 自动化测试通用规则
priority: 800
applies_to:
  languages: [python]
  paths: ["tests/**", "**/test_*.py"]
---

| 规则键 | 严重度 | 摘要 |
|---|---|---|
| R-LOG | high | 日志必须包含硬件状态与复现步骤 |
| R-FIX | medium | fixture 必须隔离，禁止共享可变状态 |

### R-LOG 日志规范

（详细要求）

反例：
```bad
log.info("failed")
```

正例：
```good
log.error("nvme read failed: dev=%s, lba=%d, status=0x%x", dev, lba, st)
```
````

解析规则：frontmatter 提供包元数据；规则表提供键/严重度/摘要；`### <规则键>` 段落为正文；`bad`/`good` 围栏为示例。

### 6.4 装配与优先级语义

- **同键覆盖**：优先级高者胜（仓库规则覆盖场景规则；场景覆盖改严重度/权重）；
- **禁用**：`disable: [R-XXX]` 不删包、不改 prompt；
- **适用性过滤**：按 profile 语言与路径裁剪，避免无关规则挤占 budget；
- **冲突声明**：渲染时显式标注「仓库规则优先于工具规则」；
- **键正则生成**：由 RuleSet 已装配键 + `repo_prefix` 生成，替代 `_RULE_REF_REGEX` / `_RULE_EXEMPT_REGEX` 写死逻辑。

### 6.5 渲染与 prompt 预算

- 顺序：仓库规则 block（priority 1）→ 场景规则包（priority 降序）→ 共享规则包；
- 现有 Python 顺序保持不变（仓库规则在前、通用规则在后）；
- 正反例按预算选择性注入：低优先级/低严重度规则先省略示例，再省略正文，最后省略整包，并在 prompt 中注明「因预算省略的规则」；
- 渲染为纯函数，输入 `RuleSet + profile + budget`，输出字符串，可做 golden 测试。

### 6.6 消费方

| 消费方 | 用途 |
|---|---|
| `review/assembly.py` | 渲染规则块（仓库规则置顶） |
| `review/validation.py` | 规则键识别、仓库规则豁免/加分、严重度默认值 |
| `review/publishing.py` | suggestion 的规则归属与标签 |
| `reporting/` | 规则中译、分类、动作映射、低采纳率统计（读 RuleSet 索引，不再正则反解） |
| telemetry | 仍按 rule key 字符串记录，来源由键前缀区分（仓库规则用 `repo_prefix`） |

## 7. 检视引擎拆分与接口

### 7.1 拆分映射

| 现有位置（commands/improve.py） | 去向 |
|---|---|
| 文件扩展名过滤、review_mode、测试路径/抽样、优先级打分（:204-330） | `review/planner.py` |
| `_group_files_for_batching`、`_assign_strategy`、`_sample_case_files`、`_test_feature_density`（:1307-1655） | `review/planner.py`（测试特征密度经 policy 计算） |
| `_build_full_source_block` / `_build_partial_context_block` / `_build_patch_only_block`、跨文件段、已知问题锚点（:857-887, 1656-1770, 1950-1975） | `review/context.py` |
| `_build_chunk_prompt` 模板拼装（:889-986） | `review/assembly.py`（纯函数，不调 LLM） |
| `_call_chunk` 并行调用、结果合并、argv 预算（:622-644, 989-1213, 1542-1615） | `review/runner.py` |
| `_collect_defined_names`、`_safe_parse_defs`、apply 风险、`rg -t py`（:1975-2300, 699-760） | `review/policies/python.py` |
| `_validate_suggestion`、`_normalise_suggestion`、行号校验、去重指纹、规则键正则（:34-70, 86-96, 2905-3239） | `review/validation.py`（正则/默认严重度来自 RuleSet） |
| `_publish`、suggestion 块、`publish_overview` | `review/publishing.py` |
| 命令入口/依赖组装 | `commands/improve.py` 薄壳 |

### 7.2 兼容门面（保证 136 处测试耦合零改动）

`commands/improve.py` 保留：

- `ImproveCommand` 及同名方法，仅委托：`_call_chunk`、`_call_agent`、`_split_diff_by_file`、`_diff_line_map`、`_read_file_lines`、`_collect_cross_file_refs_for_mr`、`_publish`、`_handle_cohort_reoccurrence`、`_code_first_line_matches`；
- `config` 属性（指向合并后的配置对象）；
- 模块级 re-export：`_suggestion_fingerprint`、`_RULE_REF_REGEX`、`_RULE_EXEMPT_REGEX`、`_MAX_ARG_STRLEN`。

现有 68 处 import、10 处 `monkeypatch.setattr("reviewagent.commands.improve.config", …)`、58 处私有方法 monkeypatch 全部继续生效；测试断言零改动。门面只委派，不保留业务逻辑。

### 7.3 LanguagePolicy 边界（消除与 profile 的双源）

```python
class LanguagePolicy(Protocol):
    def is_test_file(self, path: str) -> bool: ...
    def test_feature_density(self, path: str, file_size: int) -> float: ...
    def cross_file_evidence(self, symbol: str, workspace: Path) -> str | None: ...
    def validate_suggestion(self, suggestion, file_ctx) -> RiskVerdict: ...
    def prompt_sections(self) -> dict[str, str]: ...
```

- **profile 只存数据**（扩展名/路径/权重/关键词）；`planner` 用 profile 数据做通用过滤与打分；
- **policy 只负责语言语义**（测试文件判定、特征密度、跨文件检索、防幻觉校验、语言相关 prompt 段）；
- 注册表按 `profile.language` 查表；首期仅 `python`，未注册即启动失败；
- `suggestion_actions.py` 的 adopt 判定改用 policy `is_test_file`。

### 7.4 严格兼容的落地

1. `ssd-python-autotest` 的 prompt/规则逐字节迁移；规则渲染保持现有顺序（仓库规则 → 通用规则）。
2. 现有 548 个测试靠兼容门面零改动全绿；`tests/conftest.py` 不设 `REVIEW_PROFILE`，走默认回退。
3. `config.py` 迁走的项按 §5.4 保留 env 读取与覆盖语义。
4. `_call_chunk` 的模型选择、qodercli 128KB argv 预算随迁 `runner.py`，行为不变。
5. `prompts/loader.py` 改为 profile 解析后，36 处 `provider.run(agent=...)` 测试走默认 profile 继续通过。

## 8. cpp-generic-dev 场景包（预留设计，首期不实施）

以下为后续实施 C/C++ 场景时的说明，本期不创建目录、不写实现、不加测试；接口与配置字段（`language` 注册表、`rule_keys`、`capabilities`、`reporting`）按此预留，届时只需新增场景包与 `policies/cpp.py`，核心代码零改动。

- `language: cpp`；`include_extensions: [.c, .h, .cpp, .cc, .hpp]`；`ast_validation: false`；`apply_suggestion: true`；`full_source_analysis: false`（规避大文件 E2BIG）。
- `rule_keys`：`builtin` 键由规则包声明（`CPP-R-*`）、`repo_prefix: CPP-RULE`、`display_prefix: CPP`。
- `prompts/`：`improve.md`（output_schema 与现有一致）、`describe.md`、三个 `weekly_*.md`。
- `rules/`：内存/资源泄漏、边界与越界、并发安全、错误处理路径、可移植性、风格一致性，附正反例。
- `reporting`：标题、`rule_actions`、`repo_scan_extensions: [.c, .h, .cpp, .cc, .hpp]`、入口文件。

## 9. 启动、错误处理与部署

- profile 加载（硬边界）：包不存在 / 必填字段缺失 / language 未注册 → 启动失败并列出可用场景；未设置 `REVIEW_PROFILE` → 默认回退 + WARNING。
- 规则层：包解析失败（frontmatter/表格损坏、重复键冲突且无法按优先级裁决）→ 启动失败并指明包路径；仓库规则缺失/读取失败 → 视为无项目规则，WARNING，不阻塞检视。
- 模板渲染：占位符缺失/多余 → 报错带 profile 与模板路径。
- RQ job 场景不匹配：worker 拒绝执行并标记失败。
- cpp suggestion 校验失败（后续）：丢弃该条并计入 telemetry，不阻塞其余建议。
- 部署约定：`.env.example`、`deploy/*.service`、`README.md`、`docs/DEPLOYMENT.md` 增加 `REVIEW_PROFILE` 与一实例一库的 `REVIEWAGENT_DATA_DIR`；默认值保持 `ssd-python-autotest`。

## 10. 测试策略

1. **兼容性铁律**：现有测试零改动全绿（兼容门面 + 默认回退）。
2. **规则层**：parser（frontmatter/表格/正文/正反例）、resolver（四层优先级、同键覆盖、禁用、适用性过滤）、render（顺序、预算裁剪、仓库规则置顶）、keys（键正则生成、仓库前缀）、repo（AGENTS.md 获取/缓存，复用现有 mock）。
3. **场景层**：profile 加载（默认回退/非法值/env 覆盖/共享包引用）、prompt 模板变量齐全性。
4. **引擎层**：planner（过滤/打分/抽样/分批）、context（三种 source 块）、assembly（golden prompt）、runner（合并/预算）、validation（锚定/去重/键豁免）、python policy（ast/rg/测试判定）。
5. **集成**：ssd-python-autotest 端到端 fake-LLM 流程（fakeredis/mock provider）；RQ job profile 校验；周报链路 profile 化（标题/动作/扩展名/翻译，python 输出与现有快照一致）。
6. C/C++ 测试随 C/C++ 实施一并新增。

## 11. 实施顺序（首期：Python 场景）

1. `scenario/` 基础：profile 加载器 + prompts 加载器 + `ssd-python-autotest` 数据迁移（默认回退/env 覆盖，现有测试全绿）；
2. 规则层：model/parser/resolver/render/keys/repo + `repo_context.py` 兼容 re-export（规则键/优先级/仓库规则测试）；
3. `review/` 拆分：planner/context/assembly/runner + improve 兼容门面（测试零改动全绿）；
4. policy + validation：LanguagePolicy 协议、`policies/python.py`、规则键驱动的校验/豁免；`suggestion_actions` 去 Python 关键字；
5. publishing 合并 + 周报链路 profile 化（三个 weekly prompt、rule_translate、renderer、repo_scan、标题），python 输出不变；
6. 部署与文档更新（`.env.example`、systemd、README、DEPLOYMENT.md）。

每步独立可验收、可提交。首期稳定后，再按第 8 节新增 `cpp-generic-dev` 场景包与 `policies/cpp.py`（核心代码零改动）。
