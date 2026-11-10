# 规则包编写指南（最小模板）

规则包是**场景数据**，不是代码：一个 Markdown 文件 = 一个 `RulePack`，由
`reviewagent/scenario/rules/parser.py` 解析、`resolver.assemble_base()` 装配成
不可变的 `RuleSet`，再由 `render_rule_block()` 渲染后经 prompt 的
`{{rules_block}}` 占位注入 LLM（`verbatim` 原样注入正文，`structured` 注入
`| 规则键 | 严重度 | 摘要 |` 表）。

- 放哪：profile 目录下任意 `.md`，由 `profile.yaml` 的 `rules: [...]` 引用；
  多个包按 frontmatter `priority` 降序装配（同优先级按声明顺序），同键**首次命中获胜**。
- 开关：`profile.disable: [键]` 禁用；`profile.rule_overrides` 只允许改
  `severity` / `weight`。
- 语言：`applies_to.languages` 不包含当前 profile 语言时，该包返回空规则 + 空正文
  （"本包不适用于该语言"，不报错）。

## frontmatter 字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `schema_version` | 是 | 目前只接受 `1`，否则启动失败 |
| `id` | 是 | 合并范围内唯一；重复即启动失败（不靠加载顺序静默覆盖） |
| `title` | 否 | 缺省取 `id` |
| `priority` | 否 | `0..1000`，默认 800；越大越先装配 |
| `render` | 否 | `verbatim`（默认）或 `structured` |
| `applies_to` | 否 | `languages: [python]`、`paths: ["**/*.py"]`；`paths` 必须是仓库相对 POSIX glob，禁止以 `/` 开头、禁止 `\\` 与 `..` |
| `aliases` / `actions` | 否 | 包级默认值；表格同名列可覆盖 |

未知 frontmatter 字段一律拒绝（fail-fast，防止拼写错误被静默忽略）。

## 模式一：`verbatim`（正文逐字节保留）

正文原样进入 prompt，框架只从表里建立键索引，**不改写一个字**。适合沿用现有
"表格即 prompt" 的规则包。

````markdown
---
schema_version: 1
id: ssd-python-autotest/example-verbatim
title: 示例：verbatim 规则包
priority: 700
render: verbatim
applies_to:
  languages: [python]
---
### 示例：通用规则清单（正文逐字节进入 prompt）

下表既是键索引，也是模型看到的内容；`verbatim` 不改写正文一个字，
因此表格写作要直接可读。

| 键 | 类别 | 反模式（命中条件）| 修复方向 |
|---|---|---|---|
| `SSD-RULE-NO-BARE-EXCEPT` | 错误处理 | `except:` / `except Exception:` 静默吞异常 | 捕获具体异常并用 `logger.exception` 记录堆栈 |
| `SSD-RULE-NO-BARE-PRINT` | 日志 | 业务代码用 `print` 输出 | 改用分级 `logger` |

命中后 `rationale` 以规则键开头，例如 `SSD-RULE-NO-BARE-EXCEPT: ...`。
````

注意：`verbatim` 下 `disable` 只从索引/覆盖/统计里排除该键，**prompt 正文不变**，
LLM 仍可能引用被禁用的键（只失去归属统计）。需要强禁用语义请用 `structured`。

## 模式二：`structured`（表格元数据 + 段落正文 + 正反例）

表格提供元数据（严重度 / 别名 / 动作 / 检测提示），`### <规则键> [标题]` 段落提供
正文与正反例；两者按规则键合并成同一个 `Rule`。`disable` 时正文与示例一起排除。

````markdown
---
schema_version: 1
id: ssd-python-autotest/example-structured
title: 示例：structured 规则包
priority: 700
render: structured
applies_to:
  languages: [python]
  paths: ["**/*.py"]
---
| 规则键 | 严重度 | 摘要 | aliases |
|---|---|---|---|
| `SSD-RULE-RESOURCE-CONTEXT-MANAGER` | high | 资源句柄必须托管 | `R-RES` |

### SSD-RULE-RESOURCE-CONTEXT-MANAGER 资源句柄必须托管

表格提供元数据（severity / aliases），本节提供正文与正反例；`structured`
模式在 `disable` 时会把正文与示例一起排除，语义比 `verbatim` 强。

反例：

```bad
fh = open(path)
data = fh.read()
```

正例：

```good
with open(path) as fh:
    data = fh.read()
```
````

表格列名兼容两套写法：

- 现代三列：`| 规则键 | 严重度 | 摘要 |`（可选 `aliases` / `actions` / `detect_hints`）；
- 既有四列：`| 键 | 类别 | 反模式（命中条件）| 修复方向 |`（无严重度列时按 `medium`）。

## 正反例围栏写法

`structured` 段落里的示例必须用**恰好** `bad` / `good` 两种标签的三反引号围栏：

- 开围栏行：三个反引号紧跟 `bad` 或 `good`，随后直接换行（标签后不能写语言名）；
- 闭围栏行：单独一行三个反引号；
- 解析器用非贪婪匹配，一个段落可放多组；围栏内的 `###` / `|` 行不会被当成规则标题或表格。

因为本文档自身要在围栏里展示这些围栏，示例整包统一用**四个反引号**包裹，
内部正反例仍是三个反引号——`tests/test_rules_docs.py` 会真实解析这两段示例，
文档写坏即 CI 失败。

## 命名空间与共享包

`assemble_base()` 在装配期校验每个键，越界报
`ValueError("rule key '...' is outside profile namespace")`，服务启动失败：

- **内置通用键**：`R-*`（如 `R-ERR` / `R-LOOP`），匹配 profile `rule_keys.builtin_pattern`；
- **仓库 / 场景键**：`<前缀>-RULE-*`（默认 `rule_keys.repo_prefix: auto`，即任意
  大写前缀）。被检仓库的规则文件在运行时从默认分支**递归扫描配置的规则目录**
  （`repo_context_rules_dir`，默认 `.agents/rules`）下的所有 `.md`；目录、前缀、
  条数都不在工具里写死。`rule_keys.repo_prefix` 可在 `review.yaml` 收紧为
  `SSD`（或 `SSD-RULE`，两者归一化等价），此时只接受该前缀。内置键之外、
  不符合 `*-RULE-*` 形状的键（例如 `CPP-R-*`）会被拒绝。

共享包（`profiles/_shared/**/*.md`，被多个 profile 引用）**必须**声明
`applies_to.languages`，否则装配报 `shared rule pack ... must declare applies_to.languages`：
共享包没有语言归属就会静默混入别的语言场景。

## 校验：`reviewagent rules check`

作者本地与 CI 使用同一入口（装配逻辑只有一份，CLI 不复制校验）：

```bash
# 逐条打印 key \t severity \t title，末尾 ruleset_fingerprint= / render_checksum=
.venv/bin/python -m reviewagent rules check --profile ssd-python-autotest

# 退出码：0 = 装配成功；2 = 解析 / 命名空间 / 重复键 / paths / 共享包声明失败
# 失败时 stderr 只有一行 "rules check failed: ..."（不打印 traceback，可直接进 CI / ExecStartPre）
```

配合 `config check`（profile / 配置 / instance 绑定）可覆盖启动前的全部静态校验：

```bash
.venv/bin/python -m reviewagent config check --profile ssd-python-autotest
.venv/bin/python -m reviewagent rules check --profile ssd-python-autotest
```
