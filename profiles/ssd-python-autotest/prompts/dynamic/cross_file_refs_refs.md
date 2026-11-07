## 🟠 优先 1 — 跨文件影响分析 (Python 端已 grep, **P1, 在所有规则检查之前**)

在 worktree 找到 {{refs_count}} 条其他文件对本文件改动的引用:

{{refs_block}}**请逐一分析每条 caller 是否需要同步更新**：
- 函数签名变了但 caller 没传新参数 / 类型不匹配 → 产 suggestion, `label: cross-file impact`
- 常量改了引用方没同步 → 产 suggestion, `label: cross-file impact`
- 删除/重命名的函数别处还在用 → 写进 summary_md 文字, 格式 `> 跨文件影响: <文件> L<行号> <问题>`
- 跨文件影响类问题**不要求先命中 R-XXX 20 类**, 命中即产 suggestion
- `rationale` 必须以 `R-OTHER-IMPACT:<简短描述>` 开头 (例 `R-OTHER-IMPACT:caller_param` / `R-OTHER-IMPACT:schema_drift` / `R-OTHER-IMPACT:import_path` / `R-OTHER-IMPACT:fixture_break`)
- **严格只用于跨文件影响** — 同文件内的资源/异常/循环问题应归 R-RES / R-LOOP / R-ERR 等, 不要错归 R-OTHER-IMPACT

