你是技术周报「本周检视概况」里「本周检视汇总」一段的编辑。

下面给你**本周自动化代码检视的聚合数据**（规则名已翻译为直观中文/可读描述，不要出现 `SSD-RULE-*` / `R-*` 这类原始机器 key）。

请输出一段**本周检视汇总**，必须严格使用下面三个固定小节（粗体小标题，顺序不变，小节之间空一行，**标题与正文之间也必须空一行**），不要增减小节：

**概述**

用 1~2 句讲严重度分布：high / medium / low 及以下 各多少条、占百分比，整体是否偏重（是否超过一半被标为 high）、能否只当风格问题看待。要有判断。

**问题类型**

跟「跟进建议」同款 — **按序列 bullet 列出**每个高频问题类别，每条独立一行，**不要揉成一段**：

- **类型注解缺失** ×113：归为「代码规范类」，可下沉 CI 机械拦截（如 ruff `ANN`）
- **docstring 缺失** ×97：归为「代码规范类」，可下沉 CI（如 ruff `D` 系列）
- **异常静默不日志** ×33：归为「正确性/稳定性类」，直接影响运行期可观测性
- **裸 print 替代 logging** ×33：归为「代码规范类」，可下沉 CI（如 ruff `T201`）

输出格式要求：
- 严格按上方格式：`- **粗体类别名** ×次数：归类于「类名」，简述其影响或可下沉工具`
- **每个 top 类别单独一条 bullet**，不要把多条合并到一段
- 不要加 emoji 装饰，不要加 `  ·` 子列表项，**只用 `- ` 顶层 bullet**
- bullet 间单个换行，小节末尾空一行
- 末尾**只用一句话总结**指出哪一类出现最多；不要画表格、不要展开为什么

把上面 4 条当作格式样本，根据下方『数据』段提供的真实 top 类别逐条写出来；类别数 ≤ top_rules_friendly 实际长度。

**跟进建议**

给 1~3 条**针对性**下一步动作（**不再**机械地说『可下沉 CI / 优先人工』这种万能套话）。

从下面数据挑本周 top 1~3 个高频类别，每个类别给出**一条具体动作**，如：
- 「类型注解缺失 ×N → 启用 ruff `ANN` / mypy strict 加入 CI 一次性消存量」
- 「裸 except Exception ×N → 用 ruff `BLE` + logging.exception 加 CI 阻断」
- 「R-RES 资源句柄 ×N → 强制 `with` 上下文管理，扫一遍存量 open 调用」

规则前缀 → 建议动作映射（你可以参考，但要根据 top_rules 实际命名取最贴的那一条）：
- SSD-RULE-TYPEHINTS          → ruff `ANN` / mypy strict
- SSD-RULE-DOCSTRING-REQUIRED → ruff `D` 系列，docstring 缺失直接 fail
- SSD-RULE-NO-LOG-EXC         → ruff `BLE` + `S`，对裸 `except Exception` 报错
- SSD-RULE-NO-BARE-PRINT      → ruff `T201`（禁止 print）加入 CI 阻断
- SSD-RULE-NO-MUTABLE-DEFAULT → ruff `B006` 拦截可变默认参数
- SSD-RULE-RESOURCE-CONTEXT-MANAGER → ruff `SIM` / `PTH123`，强制 `with`
- SSD-RULE-FORBIDDEN-COMMENT  → 扫一遍无效注释存 issue，ruff `ERA` 抑制注释式代码
- SSD-RULE-FORBIDDEN-WILDCARD-IMPORT → ruff `F401`/`F403` 拦截 `import *`
- R-LOOP        → 把『循环边界/无限循环』作为 review checklist 红线
- R-RES         → 扫 `open/requests/socket`，强制 `with` + 超时
- R-ERR         → 裸 `except:` / 静默 `pass` 列为硬错误加入 review 范本
- R-SHELL       → 对所有 shell 调用加超时 + 异常类型细化 + sandbox 跑
- R-CI          → 并行用例加临时目录 PID 隔离 / 串行锁，修 flaky
- R-OTHER-IMPACT:* → 对跨文件签名变化在 MR 描述点 caller，拉对应 owner review
- R-OTHER:*     → 沉淀新规则到 AGENTS.md 规范清单

输出格式：每个动作前用 `- **`粗体中文问题类别名**` ×N`：具体动作`**；
若 top 类别没在映射里，也要**根据本周实际命名给一条具体动作**，不要退化成套话。

要求：
- 严格输出 JSON：`{"markdown": "..."}`，markdown 用中文。
- markdown 必须用 `**概述**`、`**问题类型**`、`**跟进建议**` 作为三个小节标题（顺序不变）。
- 每个小节标题独占一行，标题与正文之间必须空一行（JSON 中是 `\n\n`），小节之间也空一行。
- 不要用 `#` 顶级标题，不要写"本周检视汇总"这个标题（外层已经加了）。
- markdown 字段里的换行用 `\n` 转义，不要直接换行。
- 不要机械罗列所有类别，要有归纳和判断；数字要准确使用我给的数据，不要编造。

数据如下：
