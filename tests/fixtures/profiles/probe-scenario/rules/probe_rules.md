---
schema_version: 1
id: probe-scenario/probe
title: 探针场景规则
priority: 800
render: structured
---

| 规则键 | 严重度 | 摘要 |
|---|---|---|
| `PROBE-R-001` | high | 探针文件出现 TODO 占位实现 |

### PROBE-R-001 探针占位实现

探针文件（`.probe`）中出现的 `TODO` 注释表示实现尚未完成。

```bad
def run() -> None:
    # TODO: 探针待实现
    pass
```

```good
def run() -> None:
    return None
```
