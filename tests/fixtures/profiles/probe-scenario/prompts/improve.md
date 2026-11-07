---
name: probe-improver
description: 探针场景的最小改进 prompt（仅用于证明数据驱动可接入）
output_schema:
  type: object
  properties:
    summary_md:
      type: string
    suggestions:
      type: array
  required:
    - summary_md
    - suggestions
tools:
  write: false
  edit: false
  bash: false
  webfetch: false
---

# 探针场景改进角色

你检视 `.probe` 数据文件。规则键形如 `PROBE-R-*`，每条 suggestion 的
`rationale` 必须以命中的规则键开头。

共享引擎不包含任何 probe 语法知识；本 prompt 是场景数据的一部分。
