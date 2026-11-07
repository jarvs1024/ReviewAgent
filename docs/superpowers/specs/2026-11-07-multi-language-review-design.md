# 通用检视场景包架构（Profile-based Review）设计文档

- 日期：2026-11-07
- 分支：new_review_v2（基于 main 新建）
- 状态：已与需求方分段确认；已按完整性核查补齐缺口，待审阅

## 1. 背景与问题

ReviewAgent 当前是「SSD 自动化测试（Python）MR 检视」专用系统：

- 20 条 R-* 通用规则硬编码在 `prompts/_general_rules_block.md`，带大量 SSD 硬件 / pytest 场景假设；
- prompt（`prompts/improve.md`、`describe.md`）及三个 weekly prompt 中角色、示例、缩进约定均写死 Python 测试语境；
- 语言假设散落在核心代码：`commands/improve.py` 中的 ast 符号校验（:1975-2300）、`rg -t py` 跨文件检索（:718,795）、pytest 关键词密度打分（`config.py:140-149`）、4 空格缩进约定（prompt :323）、规则键正则写死 `SSD-RULE-`（:86-96）；
- `commands/improve.py` 为 3354 行巨石模块，prompt 装配、调度、校验、发布、去重全部耦合；
- 周报链路除规则解析外仍写死：标题（`reporting/config.py:50,79`、`artifact.py:49,80,103`）、规则→ruff 动作映射（`renderer.py:392-399`）、代码文件仅 `.py`（`repo_scan.py:298,324`）、三个 weekly prompt 的 SSD 语境；
- 配置纯环境变量，无场景切换概念；`reporting/rule_translate.py` 用正则反解 prompt Markdown 做规则中译。

目标：框架通用化——**切换场景包即可适配不同检视场景**（SSD 存储、自动化测试、通用 C/C++ 开发等），架构可扩展、配置分离、规则可配置。

## 2. 需求决策（已确认）

| 决策点 | 结论 |
|---|---|
| 场景切换作用域 | **部署实例级**：一个实例服务一种场景，环境变量选择 |
| C/C++ 检视结果形态 | 保留**可 Apply 的 GitLab suggestion**（同现有） |
| 现有 Python 检视兼容性 | **行为严格兼容**：现有测试全绿、断言零改动、prompt/规则逐字节迁移 |
| 规则配置形态 | **Markdown 规则包**（延续现有表格风格，程序化消费靠受控格式解析） |
| C/C++ 校验深度 | **暂不做 AST**：不引入 clang/tree-sitter，靠上下文锚定 + 风险策略兜底 |
| Profile 语义 | **场景包**（非语言包）：语言只是属性，同语言可有多场景，规则可组合 |
| `REVIEW_PROFILE` 未设置 | **默认回退 `ssd-python-autotest` 并记 warning**；显式设置了不存在的值才启动失败 |
| 测试耦合处理 | **兼容门面**：`ImproveCommand` 保留同名私有方法（仅委托）与 `config` 属性，136 处测试耦合零改动 |
| 检视数据存储 | **一实例一库**：文档强制 `REVIEWAGENT_DATA_DIR` 按实例隔离，schema 不改 |
| 周报场景化 | **全量纳入场景包**：三个 weekly prompt、规则→动作映射、repo_scan 扩展名、标题 |
| env 与 profile 优先级 | **显式 env > profile.yaml > 内置默认值** |

## 3. 非目标

- 不做仓库级 profile 自动发现（实例级已确认；仓库级留作后续演进）。
- 不做结构化规则 DSL / 规则命中统计闭环（YAGNI）。
- 不做 C/C++ AST 级符号校验。
- 不改 webhook / RQ / GitLab / LLM provider 的外部接口；telemetry 表结构不变（靠一实例一库隔离场景）。

## 4. 总体架构

主干不动：`webhook → RQ worker → GitLab 拉 diff → git worktree → LLM → 发布 suggestion → telemetry`。重构只发生在 worker 内部检视引擎层。

```
reviewagent/
├── profiles/                     # 新：场景包根目录
│   ├── _shared/                  # 共享规则与默认 prompt 库（可被多场景引用）
│   ├── ssd-python-autotest/      # 场景包 1：现有行为严格迁移
│   │   ├── profile.yaml
│   │   ├── prompts/improve.md, describe.md, weekly_*.md
│   │   └── rules/*.md
│   └── cpp-generic-dev/          # 场景包 2：通用 C/C++ 开发检视
├── review/                       # 新包：检视引擎
│   ├── engine.py                 # 调度：文件过滤/排序/分批/chunk
│   ├── assembly.py               # prompt 装配：profile 模板渲染 + repo 上下文注入 + argv 预算
│   ├── policy.py                 # LanguagePolicy 接口 + 注册表
│   ├── policies/python.py
│   ├── policies/cpp.py
│   ├── validation.py             # 语言无关防护：行号校验、去重指纹、锚定校验
│   └── publishing.py             # GitLab suggestion 构造与发布
├── profile.py                    # 新：profile 加载器（yaml 解析 + 校验 + env 合并 + 单例）
├── commands/improve.py           # 瘦身为命令入口 + 兼容门面（委托壳）
└── prompts/loader.py             # 改为按当前 profile 解析模板路径
```

## 5. Profile（场景包）规范

### 5.1 目录与选择

- 场景 = 一个目录，含 `profile.yaml` + `prompts/` + `rules/`。
- 实例选择：环境变量 `REVIEW_PROFILE`（场景目录名或绝对路径）。
  - 未设置 → 载入 `ssd-python-autotest`，日志 WARNING 提示显式声明；
  - 设置为不存在的目录 → **启动即失败**，错误列出可用场景及各自 `description`；
  - 必填字段缺失 / `language` 未注册 → 启动即失败，列出缺失字段。
- 加载时机：`reviewagent/profile.py` 提供 `load_profile()` 单例，由 `config.py` 模块加载后、API `main.py` lifespan、`rq_worker.py` bootstrap 三处共同触发；所有进程读同一 env，保证 API / worker 一致。
- **双进程一致性**：RQ job 载荷新增 `profile` 字段（enqueue 时写入当前场景名）；worker 执行前校验与自身场景一致，不一致则标记失败并记录日志——防止切场景后残留队列任务用错误模板执行。

### 5.2 profile.yaml 示例

```yaml
name: ssd-python-autotest
description: SSD 自动化测试 Python 代码检视（现有行为）
language: python              # 决定 LanguagePolicy 查表
prompts:
  improve: prompts/improve.md
  describe: prompts/describe.md
  weekly_inspection_summary: prompts/weekly_inspection_summary.md
  weekly_change_summary: prompts/weekly_change_summary.md
  weekly_quality_scan: prompts/weekly_quality_scan.md
rules:                        # 列表，支持相对 profile 目录与 ../_shared/ 引用
  - rules/general_rules.md
rule_keys:                    # 规则键命名与解析，替代写死正则
  builtin_regex: 'R-[A-Z]+(?:-[A-Z0-9_]+)*'
  repo_prefix: SSD-RULE       # 被检仓库自定义规则前缀（repo_context 抽取用）
  display_prefix: SSD         # 周报翻译分类前缀
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
  feature_keywords: []        # 取自现 config.improve_test_feature_keywords，逐项搬入
  test_paths: [tests/, test_*.py]
  priority_weights: {}        # 取自现 config.improve_priority_weight_*
  review_mode: auto           # 现 improve_review_mode
  test_sample_paths: []       # 取自现 config.improve_test_sample_paths
  test_sample_max: 3
reporting:
  weekly_title: "SSD自动化代码检视周报"
  rule_actions: {}            # 规则键→建议动作，替代 renderer 写死 ruff 映射
  repo_scan_extensions: [.py]
  repo_scan_entry_files: [__init__.py, main.py]
```

- `REVIEW_PROFILE` 之外，通用配置（GitLab/Redis/LLM/超时/队列/数据目录）仍走 `config.py`。
- **边界**：`config.py` = 凭据与运行参数；`profile.yaml` = 场景内容。迁移进 profile 的字段保留 env 读取，**显式 env 值覆盖 profile 值**（见 §5.5）。

### 5.3 prompt 模板

- 沿用现有 frontmatter 格式（name / output_schema / tools），`prompts/loader.py` 加载机制保留，但**路径解析改为按当前 profile**：`loader.load(agent)` 从 profile 的 `prompts` 映射取路径；`load_block` 从 profile 的规则/共享目录解析。三个 weekly agent 与 `describe` 一并纳入映射（未配置时回退 `_shared/` 同名默认模板）。
- 正文使用简单 `{{placeholder}}` 变量替换。变量：diff 块、repo 上下文（AGENTS.md / .agents/rules）、规则包内容（rules 列表按序拼接）、profile 元信息（语言、能力开关派生的段落开关）。
- 现有 `improve.md` / `describe.md` / `weekly_*.md` / `_general_rules_block.md` **逐字节迁入** `ssd-python-autotest` 包；improve.py 中内联拼装的 SSD 规则段（:858-885, :943-951）与 "Python 端已 grep" 段改为模板变量注入，文案由 profile 提供。
- `scripts/sync_agents.py` / `sync_qoder_agents.py` 改为读取当前 profile 的 prompts 目录（不再固定 `reviewagent/prompts`）。

### 5.4 规则包与规则键

- Markdown，延续两列表格（`| 规则 | 说明 |`）。场景包内建规则键按 `rule_keys.builtin_regex` 命名（SSD 场景 `R-*`，C++ 场景 `CPP-R-*`）。
- 组合：`rules:` 列表可引用 `../_shared/*.md` 共享规则库，多场景复用（如 SSD 硬件规则被多个 SSD 场景引用）。
- **规则键解析统一配置化**（修复核查发现的断裂点）：
  - `_RULE_REF_REGEX` 由 profile `rule_keys.builtin_regex` 动态构建——`CPP-R-LEAK` 不再被截断为 `R-LEAK`；
  - `_RULE_EXEMPT_REGEX` 由 `rule_keys.repo_prefix` 构建——C++ 场景 `CPP-RULE-*` 同样享受严重度豁免与加分；
  - `repo_context.py` 抽取前缀改用 `rule_keys.repo_prefix`，C++ 仓库的 `.agents/rules` 不再提取为空；
  - `reporting/rule_translate.py` 的通用映射与 `_shared` 默认模版从当前 profile 的规则文件读取；`translate_rule_key` 的 `SSD-RULE-` 写死改为 `repo_prefix`。
- `reporting/renderer.py` 的 `_RULE_ACTIONS`（ruff 映射）改为 profile `reporting.rule_actions`，未命中时使用通用建议文案。

### 5.5 env 与 profile 合并规则

- 顺序：显式设置的环境变量 > `profile.yaml` > 代码内置默认值。
- `config.py` 中迁往 profile 的字段（`improve_test_feature_keywords`、`improve_skip_test_paths`、`improve_priority_weight_*`、`improve_review_mode`、`improve_test_sample_*`、`improve_keyword_paths`、`rule_key_prefix`、周报标题/目录等）保留 env 读取；profile 加载器将「已显式设置的 env」覆盖到 profile 值上，`config` 对象字段指向合并结果，现有部署不改 env 行为不变。
- 现有两套默认值互相矛盾处（如 `improve_review_mode` "test" vs "auto"、权重 15/30/20/20 vs 20/25/20/15）统一以 `ssd-python-autotest` profile 中的值为准，`config.from_env()` 的测试断言保持通过（断言的是 env 往返，不受影响）。

## 6. improve.py 拆分与 LanguagePolicy

### 6.1 拆分映射

| 现有位置（commands/improve.py） | 去向 |
|---|---|
| `_call_chunk` LLM 调用、chunk prompt 拼装、128KB argv 预算（:98-102,1542,1560-1575） | `review/assembly.py`（预算计算语言无关；是否启用 full-source 由 profile `capabilities.full_source_analysis` 决定） |
| 文件过滤/排序/分批/主流程 | `review/engine.py`（参数来自 profile `file_filter` / `scoring`） |
| `_collect_defined_names`、`_safe_parse_defs`、apply 风险检查（:1975-2300） | `review/policies/python.py` |
| `rg -t py` 跨文件检索（:718,795）、pytest 密度打分（:1520）、测试文件判定 | `review/policies/python.py` |
| 行号校验/规范化、去重指纹（:34-70）、规则键正则（:86-96） | `review/validation.py`（正则从 profile 注入） |
| `_publish`、suggestion 块构造（:2423, :2666-2670, :3211-3225） | `review/publishing.py` |
| 命令入口/依赖组装 | `commands/improve.py` 薄壳 |

### 6.2 兼容门面（保证 136 处测试耦合零改动）

`commands/improve.py` 保留：

- `ImproveCommand` 类及以下同名方法，仅委托到新模块：`_call_chunk`、`_split_diff_by_file`、`_diff_line_map`、`_read_file_lines`、`_collect_cross_file_refs_for_mr`、`_publish`、`_handle_cohort_reoccurrence`、`_code_first_line_matches`；
- `config` 属性（指向合并后的配置对象）；
- 模块级 re-export：`_suggestion_fingerprint`、`_RULE_REF_REGEX`、`_RULE_EXEMPT_REGEX`、`_MAX_ARG_STRLEN`。

现有 68 处 import、10 处 `monkeypatch.setattr("reviewagent.commands.improve.config", …)`、58 处对 `ImproveCommand` 私有方法的 monkeypatch 全部继续生效；测试断言零改动。门面内不保留业务逻辑，仅委派，防止二次腐化。

### 6.3 LanguagePolicy 接口

```python
class LanguagePolicy(Protocol):
    def filter_files(self, files: list[DiffFile]) -> list[DiffFile]: ...
    def score_file(self, path: str, content: str) -> float: ...
    def is_test_file(self, path: str) -> bool: ...
    def cross_file_evidence(self, symbol: str, workspace: Path) -> str | None: ...
    def validate_suggestion(self, suggestion: Suggestion, file_ctx: FileContext) -> RiskVerdict: ...
    def prompt_sections(self) -> dict[str, str]: ...
```

- **python.py**：全量实现，行为与现有逐项对齐（ast 校验结果格式不变、`rg -t py`、pytest 关键词、4 空格约定）。
- **cpp.py**：
  - `validate_suggestion`：仅上下文锚定（suggestion 的 `existing_code` 逐行匹配 diff 真实行）+ 风险降级规则（如建议删除行数超阈值 → 降级为纯评论，不带 suggestion 块）；
  - `cross_file_evidence`：`rg -t c -t cpp`，段落文案随 profile 注入（不写死 "Python 端已 grep"）；
  - `score_file`：扩展名 + 路径权重 + 热点词（memcpy / malloc / goto 等，来自 profile `scoring`）；
  - `is_test_file`：按路径/文件名约定判断测试代码（供 adopt 判定等复用）。
- 注册表：`profile.yaml` 的 `language` → policy 类。未注册语言启动即报错，不静默降级。
- `commands/suggestion_actions.py:367-375` 的 adopt 判定改用 policy `is_test_file`，不再硬编码 Python 关键字。

### 6.4 严格兼容的落地

1. `ssd-python-autotest` 的 prompt/规则逐字节迁移（文件移动不改内容）。
2. 现有 548 个测试：靠 §6.2 兼容门面保持零改动，全绿是验收标准；`tests/conftest.py` 不设 `REVIEW_PROFILE`，走默认回退。
3. `config.py` 迁走的项按 §5.5 保留 env 读取与覆盖语义，现有部署不改 env 行为不变。
4. `_call_chunk` 的模型选择、qodercli 128KB argv 预算逻辑随迁 `assembly.py`，行为不变。
5. `prompts/loader.py` 改为 profile 解析后，4 个测试文件 36 处 `provider.run(agent="improve"/"describe")` 走默认 profile，断言（含 `improve.md` frontmatter `tools_disabled`）继续通过。

## 7. cpp-generic-dev 场景包（首版内容）

- `language: cpp`；`include_extensions: [.c, .h, .cpp, .cc, .hpp]`；`ast_validation: false`；`apply_suggestion: true`；`full_source_analysis: false`（规避大文件 E2BIG，后续按需开启）。
- `rule_keys`：`builtin_regex: 'CPP-R-[A-Z0-9_]+'`、`repo_prefix: CPP-RULE`、`display_prefix: CPP`。
- `prompts/`：`improve.md`（frontmatter 的 output_schema 与现有完全一致，suggestions 结构不变，adopt/dismiss 下游零改动；正文换 C/C++ 开发场景；缩进约定为「遵循文件现有缩进」，`ssd-python-autotest` 包保留 4 空格原文）、`describe.md`（C/C++ MR 描述，去除 E2E/fixture/测试覆盖语境）、`weekly_*.md` 三个周报 prompt。
- `rules/`：初版按维度组织骨架——内存/资源泄漏、边界与越界、并发安全、错误处理路径、可移植性、风格一致性，键前缀 `CPP-R-*`；共享规则放 `_shared/`。具体条目实施阶段逐条打磨。
- `reporting`：`weekly_title`、`rule_actions`（规则→动作，替代 ruff）、`repo_scan_extensions: [.c, .h, .cpp, .cc, .hpp]`、`repo_scan_entry_files: [main.c, main.cpp]`。
- 周报：读当前 profile 规则包，翻译/统计逻辑同构，无语言特判。

## 8. 启动、错误处理与部署

原则：系统边界校验，内部信任。

- profile 加载（启动路径硬边界）：包不存在 / 必填字段缺失 / language 未注册 → 启动失败，错误列出可用场景与缺失字段；未设置 `REVIEW_PROFILE` → 默认回退并 WARNING（§5.1）。
- 模板渲染：占位符缺失/多余 → 报错带 profile 名与模板路径；单测覆盖内置模板变量齐全性。
- RQ job 场景不匹配：worker 拒绝执行并标记失败（§5.1）。
- cpp suggestion 校验失败：丢弃该条并计入 telemetry（与 Python 侧丢弃行为同构），不阻塞其余建议。
- 部署约定（文档与配置同步更新）：`.env.example`、`deploy/*.service`、`README.md`、`docs/DEPLOYMENT.md` 增加 `REVIEW_PROFILE` 与「一实例一库」的 `REVIEWAGENT_DATA_DIR` 说明；默认值保持 `ssd-python-autotest`，现有部署零改动可升级。
- 其余（GitLab 失败重试、RQ 异常路径等）沿用现有行为，不改。

## 9. 测试策略

1. **兼容性铁律**：现有测试零改动全绿（§6.2 门面 + §5.1 默认回退保障）——行为严格兼容的验收标准。
2. 新增：profile 加载器（默认回退/非法值/env 覆盖/共享规则引用）、模板渲染变量齐全性、规则键正则（`CPP-R-*` 完整匹配、`CPP-RULE-*` 豁免生效、repo_context 抽取）、`policies/cpp.py`（锚定校验、降级）、cpp-generic-dev 端到端 fake-LLM 流程（复用 fakeredis / mock provider 模式）。
3. 新增：RQ job profile 校验测试（场景不匹配拒绝执行）、周报链路 profile 化测试（标题/动作映射/扩展名/规则翻译）。
4. 新增结构测试：两个内置场景的 output_schema 与解析器兼容（防漂移）。
5. prompt 正文内容不做断言（与现状一致）。

## 10. 实施顺序（供 writing-plans 细化）

1. profile 加载器 + `ssd-python-autotest` 包（纯迁移 + 默认回退 + env 覆盖，现有测试全绿）；
2. improve.py 拆分 + 兼容门面（engine / assembly / validation / publishing，测试零改动全绿）；
3. 规则键正则/`repo_context`/`suggestion_actions` 的 profile 化（含 `CPP-R-*` 校验用例）；
4. LanguagePolicy 接口 + python.py（行为对齐，全绿）；
5. `policies/cpp.py` + `cpp-generic-dev` 场景包 + 新增测试；
6. 周报链路 profile 化（三个 weekly prompt、rule_translate、renderer、repo_scan、标题），python 场景输出不变；
7. 部署与文档更新（`.env.example`、systemd、README、DEPLOYMENT.md）。

每步独立可验收、可提交。
