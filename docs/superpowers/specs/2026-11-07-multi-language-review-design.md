# 通用检视场景包架构（Profile-based Review）设计文档

- 日期：2026-11-07
- 分支：new_review_v2（基于 main 新建）
- 状态：已与需求方分段确认，待审阅

## 1. 背景与问题

ReviewAgent 当前是「SSD 自动化测试（Python）MR 检视」专用系统：

- 20 条 R-* 通用规则硬编码在 `prompts/_general_rules_block.md`，带大量 SSD 硬件 / pytest 场景假设；
- prompt（`prompts/improve.md`、`describe.md`）中角色、示例、缩进约定均写死 Python 测试语境；
- 语言假设散落在核心代码：`commands/improve.py` 中的 ast 符号校验（:1975-2300）、`rg -t py` 跨文件检索（:718,795）、pytest 关键词密度打分（`config.py:140-149`）、4 空格缩进约定（prompt :323）；
- `improve.py` 为 3354 行巨石模块，prompt 装配、调度、校验、发布、去重全部耦合；
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

## 3. 非目标

- 不做仓库级 profile 自动发现（实例级已确认；仓库级留作后续演进）。
- 不做结构化规则 DSL / 规则命中统计闭环（YAGNI）。
- 不做 C/C++ AST 级符号校验。
- 不改 webhook / RQ / GitLab / LLM provider / telemetry 各层的外部接口。

## 4. 总体架构

主干不动：`webhook → RQ worker → GitLab 拉 diff → git worktree → LLM → 发布 suggestion → telemetry`。重构只发生在 worker 内部检视引擎层。

```
reviewagent/
├── profiles/                     # 新：场景包根目录
│   ├── _shared/                  # 共享规则库（可被多场景引用）
│   ├── ssd-python-autotest/      # 场景包 1：现有行为严格迁移
│   │   ├── profile.yaml
│   │   ├── prompts/improve.md, describe.md
│   │   └── rules/*.md
│   └── cpp-generic-dev/          # 场景包 2：通用 C/C++ 开发检视
├── review/                       # 新包：检视引擎
│   ├── engine.py                 # 调度：文件过滤/排序/分批/chunk
│   ├── assembly.py               # prompt 装配：从 profile 渲染 + repo 上下文注入
│   ├── policy.py                 # LanguagePolicy 接口 + 注册表
│   ├── policies/python.py
│   ├── policies/cpp.py
│   ├── validation.py             # 语言无关防护：行号校验、去重指纹、锚定校验
│   └── publishing.py             # GitLab suggestion 构造与发布
├── commands/improve.py           # 保留为薄壳：组装依赖、调 engine，入口不变
└── profile.py                    # 新：profile 加载器（yaml 解析 + 校验）
```

## 5. Profile（场景包）规范

### 5.1 目录与选择

- 场景 = 一个目录，含 `profile.yaml` + `prompts/` + `rules/`。
- 实例选择：环境变量 `REVIEW_PROFILE`（值为场景目录名或绝对路径）。未设置或指向不存在的包 → **启动即失败**，错误信息列出可用场景及各自 `description`。
- `profile.yaml` 必填字段：`name`、`description`、`language`、`prompts.improve`、`rules`（非空列表）。缺字段启动即失败，不静默回退。

### 5.2 profile.yaml 示例

```yaml
name: ssd-python-autotest
description: SSD 自动化测试 Python 代码检视（现有行为）
language: python              # 决定 LanguagePolicy 查表
prompts:
  improve: prompts/improve.md
  describe: prompts/describe.md
rules:                        # 列表，支持相对 profile 目录与 ../_shared/ 引用
  - rules/general_rules.md
capabilities:
  ast_validation: true
  cross_file_search: true
  apply_suggestion: true
file_filter:
  include_extensions: [.py]
  skip_paths: [conftest.py, tests_loc/, /pages/, /locators/]
  priority_paths: [...]
scoring:
  feature_keywords: [pytest.fixture, MagicMock, ...]
reporting:
  weekly_title: "SSD自动化代码检视周报"
  rule_key_prefix: SSD
```

### 5.3 prompt 模板

- 沿用现有 frontmatter 格式（name / output_schema / tools），`prompts/loader.py` 加载机制不变。
- 正文使用简单 `{{placeholder}}` 变量替换。变量：diff 块、repo 上下文（AGENTS.md / .agents/rules）、规则包内容（rules 列表按序拼接）、profile 元信息（语言、能力开关派生的段落开关）。
- 现有 `improve.md` / `describe.md` / `_general_rules_block.md` **逐字节迁入** `ssd-python-autotest` 包；improve.py 中内联拼装的 "SSD 自定义规则" 段（:858-885, :943-951）与 "Python 端已 grep" 段改为模板变量注入。

### 5.4 规则包格式

- Markdown，延续两列表格（`| 规则 | 说明 |`），规则键形如 `R-*` / `CPP-R-*` / `SSD-RULE-*`。
- 组合：`rules:` 列表可引用 `../_shared/*.md` 共享规则库，多场景复用（如 SSD 硬件规则被多个 SSD 场景引用）。
- `reporting/rule_translate.py` 的解析对象从硬编码路径改为「当前实例 profile 的规则文件列表」；对 `ssd-python-autotest` 场景输入文件与现在完全一致，周报输出不变。

## 6. improve.py 拆分与 LanguagePolicy

### 6.1 拆分映射

| 现有位置（commands/improve.py） | 去向 |
|---|---|
| `_call_chunk` LLM 调用与 chunk prompt 拼装（:622 附近） | `review/assembly.py` |
| 文件过滤/排序/分批/主流程 | `review/engine.py`（参数来自 profile `file_filter`） |
| `_collect_defined_names`、`_safe_parse_defs`、apply 风险检查（:1975-2300） | `review/policies/python.py` |
| `rg -t py` 跨文件检索（:718,795）、pytest 密度打分（:1520） | `review/policies/python.py` |
| 行号校验/规范化、去重指纹（:34-70） | `review/validation.py` |
| `_publish`、suggestion 块构造（:2423, :2666-2670, :3211-3225） | `review/publishing.py` |
| 命令入口/依赖组装 | `commands/improve.py` 薄壳 |

### 6.2 LanguagePolicy 接口

```python
class LanguagePolicy(Protocol):
    def filter_files(self, files: list[DiffFile]) -> list[DiffFile]: ...
    def score_file(self, path: str, content: str) -> float: ...
    def cross_file_evidence(self, symbol: str, workspace: Path) -> str | None: ...
    def validate_suggestion(self, suggestion: Suggestion, file_ctx: FileContext) -> RiskVerdict: ...
    def prompt_sections(self) -> dict[str, str]: ...
```

- **python.py**：全量实现，行为与现有逐项对齐（ast 校验结果格式不变、`rg -t py`、pytest 关键词、4 空格约定）。
- **cpp.py**：
  - `validate_suggestion`：仅上下文锚定（suggestion 的 `existing_code` 逐行匹配 diff 真实行）+ 风险降级规则（如建议删除行数超阈值 → 降级为纯评论，不带 suggestion 块）；
  - `cross_file_evidence`：`rg -t c -t cpp`，段落文案随 profile 注入（不写死 "Python 端已 grep"）；
  - `score_file`：扩展名 + 路径权重 + 热点词（memcpy / malloc / goto 等，来自 profile `scoring`）。
- 注册表：`profile.yaml` 的 `language` → policy 类。未注册语言启动即报错，不静默降级。

### 6.3 严格兼容的落地

1. `ssd-python-autotest` 的 prompt/规则逐字节迁移（文件移动不改内容）。
2. 现有 548 个测试：涉及 improve 内部函数的改为从新模块导入，**断言零改动**；全绿是验收标准。
3. `config.py` 中迁走的项（`improve_test_feature_keywords`、`improve_skip_test_paths`、`rule_key_prefix`、周报标题等）保留 env 读取，值注入 profile 加载链路——现有部署不改 env 行为不变。
4. `commands/_common.py` 的模型选择、qodercli 128KB argv 预算逻辑随 `_call_chunk` 整体迁往 `assembly.py`，行为不变。

## 7. cpp-generic-dev 场景包（首版内容）

- `language: cpp`；`include_extensions: [.c, .h, .cpp, .cc, .hpp]`；`ast_validation: false`；`apply_suggestion: true`。
- `prompts/improve.md`：frontmatter 的 output_schema 与现有完全一致（suggestions 结构不变，adopt/dismiss 下游零改动）；正文规则/示例全部换 C/C++ 开发场景；缩进约定为「遵循文件现有缩进」（`ssd-python-autotest` 包保留 4 空格原文）。
- `rules/`：初版按维度组织骨架——内存/资源泄漏、边界与越界、并发安全、错误处理路径、可移植性、风格一致性，键前缀 `CPP-R-*`；共享规则放 `_shared/`。具体条目实施阶段逐条打磨。
- 周报：读当前 profile 规则包，翻译/统计逻辑同构，无语言特判。

## 8. 错误处理

原则：系统边界校验，内部信任。

- profile 加载（启动路径硬边界）：包不存在 / 必填字段缺失 / language 未注册 → 启动失败，错误列出可用场景与缺失字段。
- 模板渲染：占位符缺失/多余 → 报错带 profile 名与模板路径；单测覆盖内置模板变量齐全性。
- cpp suggestion 校验失败：丢弃该条并计入 telemetry（与 Python 侧丢弃行为同构），不阻塞其余建议。
- 其余（GitLab 失败重试、RQ 异常路径等）沿用现有行为，不改。

## 9. 测试策略

1. **兼容性铁律**：现有测试改导入路径后全绿、断言零改动——行为严格兼容的验收标准。
2. 新增：profile 加载器（合法/非法/共享规则引用）、模板渲染变量齐全性、`policies/cpp.py`（锚定校验、降级）、cpp-generic-dev 端到端 fake-LLM 流程（复用 fakeredis / mock provider 模式）。
3. 新增结构测试：两个内置场景的 output_schema 与解析器兼容（防漂移）。
4. prompt 正文内容不做断言（与现状一致）。

## 10. 实施顺序（供 writing-plans 细化）

1. profile 加载器 + `ssd-python-autotest` 包（纯迁移，现有测试全绿）；
2. improve.py 拆分（engine / assembly / validation / publishing，测试改导入，全绿）；
3. LanguagePolicy 接口 + python.py（行为对齐，全绿）；
4. `policies/cpp.py` + `cpp-generic-dev` 场景包 + 新增测试；
5. rule_translate / 周报读 profile 化（python 场景输出不变）。

每步独立可验收、可提交。
