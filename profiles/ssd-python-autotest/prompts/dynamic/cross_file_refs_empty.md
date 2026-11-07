## 🟠 优先 1 — 跨文件影响分析 (规则检查之前必做, **P1**)

Python 端 rg 没找到 cross-file caller 引用 (本文件可能是新增 / 改动是局部 / 仓库无其他文件引用).

**仍请按 P1 优先级自行判断是否需要产 R-OTHER-IMPACT suggestion** — Python 端仅做粗扫，如下情况仍可能有跨文件风险:
- 新增 / 重命名的公共函数、类、常量 (无 caller 但下游模块可能依赖)
- 改了 fixture / 测试 helper (本 diff 未引用但其它 test 可能引用)
- 改了 import 路径 (旧路径可能还有引用未被发现)
- 改了 SQL / ORM schema (model/migration 可能未同步)

确认无风险 → 在 summary_md 注明 '未发现 cross-file 关联'。**不要为凑数硬编 R-OTHER-IMPACT**。

