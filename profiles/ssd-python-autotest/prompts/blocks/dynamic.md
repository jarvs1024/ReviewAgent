<!-- @section:cross_file_refs_empty -->
## 🔴 优先 1 — 跨文件影响分析 (规则检查之前必做, **P1**)

Python 端 rg 没找到 cross-file caller 引用 (本文件可能是新增 / 改动是局部 / 仓库无其他文件引用).

**仍请按 P1 优先级自行判断是否需要产 R-OTHER-IMPACT suggestion** — Python 端仅做粗扫，如下情况仍可能有跨文件风险:
- 新增 / 重命名的公共函数、类、常量 (无 caller 但下游模块可能依赖)
- 改了 fixture / 测试 helper (本 diff 未引用但其它 test 可能引用)
- 改了 import 路径 (旧路径可能还有引用未被发现)
- 改了 SQL / ORM schema (model/migration 可能未同步)

确认无风险 → 在 summary_md 注明 '未发现 cross-file 关联'。**不要为凑数硬编 R-OTHER-IMPACT**。

<!-- @section:cross_file_refs_refs -->
## 🔴 优先 1 — 跨文件影响分析 (Python 端已 grep, **P1, 在所有规则检查之前**)

在 worktree 找到 {{refs_count}} 条其他文件对本文件改动的引用:

{{refs_block}}**请逐一分析每条 caller 是否需要同步更新**：
- 函数签名变了但 caller 没传新参数 / 类型不匹配 → 产 suggestion, `label: cross-file impact`
- 常量改了引用方没同步 → 产 suggestion, `label: cross-file impact`
- 删除/重命名的函数别处还在用 → 写进 summary_md 文字, 格式 `> 跨文件影响: <文件> L<行号> <问题>`
- 跨文件影响类问题**不要求先命中表内 R-* 规则**, 命中即产 suggestion
- `rationale` 必须以 `R-OTHER-IMPACT:<简短描述>` 开头 (例 `R-OTHER-IMPACT:caller_param` / `R-OTHER-IMPACT:schema_drift` / `R-OTHER-IMPACT:import_path` / `R-OTHER-IMPACT:fixture_break`)
- **严格只用于跨文件影响** — 同文件内的资源/异常/循环问题应归 R-RES / R-LOOP / R-ERR 等, 不要错归 R-OTHER-IMPACT

