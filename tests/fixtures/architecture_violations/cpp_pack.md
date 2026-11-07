---
schema_version: 1
id: cpp-probe/cross-scenario
title: 负向 fixture：跨场景命名空间的规则包
priority: 800
render: structured
---

| 规则键 | 严重度 | 摘要 |
|---|---|---|
| CPP-R-001 | high | 裸指针所有权未声明 |

### CPP-R-001 裸指针所有权未声明

`T*` 返回值未标注所有权归属（借用 / 独占 / 共享），调用方无法判断是否需要释放。

```bad
Widget* make_widget();
```

```good
std::unique_ptr<Widget> make_widget();
```
