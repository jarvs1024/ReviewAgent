---
schema_version: 1
id: ssd-python-autotest/core
title: SSD 自动化测试通用规则
priority: 800
render: verbatim
applies_to:
  languages: [python]
---
### 🟡 优先 3 — 通用规则（常规代码 + pytest 测试代码）

完成仓库规则扫描后，用下表覆盖剩余问题。规则键以 `R-` 开头，命中后在 `rationale`
开头引用对应键。表内规则**不需要全部命中**：只报 diff 中真实出现的问题。

**严重度口径（全表统一）**
- `high` — 会导致用例假通过/假失败、硬件损坏、数据错误或 CI 长期不稳定
  （资源泄漏、裸异常、无效断言、并发无隔离、设备状态未校验、NVME 报文错误）
- `medium` — 可维护性 / 可观测性缺陷，短期不炸但会累积（日志、常量、错误信息、
  mock 作用域、参数化重复、临时路径共用）
- `low` — 仅限**会误导人**的一致性缺陷（过期注释、易混淆的死代码），统一走 `R-OTHER`；
  纯拼写 / 格式 / 命名口味不报（见规则块开头「⛔ 检视边界」）

**本场景不报（与规则块开头「⛔ 检视边界」一致，边界优先）**：
- `R-SHELL`（命令 / SQL 注入、`shell=True` 拼接外部入参）—— 被检对象是**内网测试台上的
  内部自动化测试代码**，命令只在本机 / 受控设备上执行，不存在外部攻击面；
- 兜底里的安全类：明文凭据、越权、路径穿越、不安全反序列化（`pickle` / `eval`）、
  弱哈希 / 非密码学随机、敏感信息（口令 / 密钥）打到日志、文件权限过宽；
- `R-DEP` 中的「三方依赖未锁定版本」与「强绑系统独有命令无多系统兼容兜底」两项
  （CI 镜像已固定依赖与系统）—— 但「可选依赖 `import` 失败导致用例无法执行」**仍要报**。

> 注意：下发 SSD 载荷 / LBA 地址 / 扇区长度的值域与对齐校验属于 `R-DATA` / `R-NVME`，
> 会造成硬件损坏或数据错误，**仍要照常报**（不在上述不报范围内）。

**适用范围**：`R-NVME` / `R-DATA` / `R-STATE` / `R-LOCK` / `R-MEM` 只在 diff 真正涉及
硬件、设备协议、大文件/全盘数据时适用；纯逻辑、纯单测代码不要硬套硬件规则
（否则会把普通变量误报成"报文未校验"）。通用 Python / pytest 规则始终适用。

**规则仲裁（同一行命中多条时只取一条，按序优先）**
1. 跨文件 caller / 引用失配 → `R-OTHER-IMPACT`（仅限跨文件，见系统提示）
2. 硬件与数据安全 → `R-DATA` > `R-NVME` > `R-STATE` > `R-LOCK`
3. 资源与并发 → `R-RES` > `R-LOCK` > `R-CI` > `R-CLEAN`
4. 用例可靠性 → `R-ASSERT` > `R-FIX` > `R-RAISE` > `R-PARAM` > `R-MOCK` > `R-SKIP`
5. 其余按影响排序 → `R-ERR` > `R-SHELL` > `R-LOOP` > `R-TIME` > `R-MEM` >
   `R-PERF` > `R-CONST` > `R-REPRO` > `R-LOG` > `R-DEP`
同一行不要发两条互斥建议；不同行 / 不同文件的问题各自独立、不得合并。

**规则表**

| 规则键 | 严重度 | 类别 | 反模式（命中条件）| 修复方向 |
|---|---|---|---|---|
| `R-RES` | high | 资源句柄 | `open()` / `serial` / `socket` / `subprocess.Popen` 创建后无 `with` / `close`；`Popen` 未 `wait` 产生僵尸进程；异常路径句柄泄漏 | 文件、串口、套接字优先 `with` 托管；非托管资源用 `try-finally` 强制关闭；子进程必须等待回收 |
| `R-TIME` | high | 时序与超时 | `sleep` 阻塞主流程；同步 IO 未设置 `timeout`；`while` 忙等待无最大等待时长 | IO 请求强制配置 `timeout`；阻塞等待改用轮询 + 超时阈值；禁止长 `sleep` 卡死进程 |
| `R-ASSERT` | high | 断言强度 | 断言被 `try/except` 吞掉；只断言"不抛异常"而不校验返回值；`unittest` 风格 `assertEqual(actual, expected)` 顺序颠倒；复杂条件无 `msg` 失败时无法定位 | 断言必须校验关键返回值；比较顺序统一为 `assertEqual(expected, actual)`；复杂断言补 `msg`；禁止吞断言异常 |
| `R-ERR` | high | 错误处理 | 裸 `except` / `except Exception` 静默吞异常；仅打印异常不记录堆栈；吞错不更新测试状态；`except` 后继续执行依赖失败数据的代码 | 捕获细分异常；用 `logger.exception` 记录全堆栈；异常同步标记用例失败；致命硬件异常向上抛出 |
| `R-LOG` | medium | 日志可观测 | 模块 / 函数内 `print` 打印信息；用例失败不导出 `dmesg` / `smartctl -a` / `nvme list` 等硬件日志；关键分支无任何日志 | 业务代码用分级 `logger`；用例失败自动采集硬件上下文日志留存；关键分支补结构化日志 |
| `R-PERF` | medium | 精确测量 | 微秒级耗时测量用 `time.time()`；测量区间含 setup / 打印等无关耗时 | 高精度测量固定用 `perf_counter()`；精简计时区间；剔除冗余耗时代码 |
| `R-CONST` | medium | 配置常量校验 | 超时 / 重试 / 地址 / 容量等字面量硬编码；配置读取不校验字段存在 / 类型 / 值域 | 统一收拢至配置 / 常量文件；读取强制多层校验；非法配置直接终止 |
| `R-SHELL` | high | Shell 执行安全（**本场景不报**，见上方「本场景不报」） | `subprocess` 用 f-string 拼接外部参数；直接透传设备原始入参；`shell=True` 拼接用户输入 | 命令固定用 `args` 列表传参；外部输入加白名单过滤；杜绝命令注入 |
| `R-LOOP` | high | 循环熔断管控 | 无限 `while` 无 `max_retry` / 整体截止 deadline；遍历时原地修改容器；重试循环无退避 | 重试循环配最大次数 + 全局截止超时；遍历修改走副本，不动原容器；重试加退避 |
| `R-MEM` | high | 内存与大批量 IO | 全盘数据 / 海量日志一次性全量加载；循环频繁创建大对象无生命周期释放；结果集无上限 | 大文件分片流式读写；缩小大对象生命周期；临时缓存过期清理；结果集加上限 |
| `R-LOCK` | high | 硬件资源互斥 | 多线程 / 多进程并发操作同一块 SSD 无锁；异常崩溃后硬件锁长期持有；文件锁未用 `finally` 释放 | 独占硬件操作加进程 / 文件锁；finally 统一解锁与硬件释放 |
| `R-DEP` | medium | 依赖与环境兼容 | 三方依赖无版本锁定；强绑系统独有命令无多系统兼容兜底；import 可选依赖失败直接崩 | 固化依赖版本；调用系统指令先做可用性检测；多系统降级兜底 |
| `R-STATE` | high | 设备前置状态校验 | 下发 NVMe / 磁盘 IO 前未校验设备在位 / 健康 / 固件就绪；失败后不复位直接继续 | 硬件操作前置校验在线与健康；异常设备直接终止并告警；失败后执行复位 |
| `R-DATA` | high | 原始报文校验 | 下发 SSD 载荷 / LBA 地址 / 扇区长度等数据不校验值域 / 对齐 / 长度；随机数据未固定种子 | 硬件交互报文前置合法性校验；拦截非法参数；随机数据固定种子以复现 |
| `R-CLEAN` | high | 测试现场清理 | 用例异常崩溃残留挂载点 / 裸盘占用 / 临时镜像文件；session/module fixture 修改的全局状态未复原 | 配置统一后置清理（`finally` / fixture teardown）；挂载 / 镜像 / 磁盘占用无论成败全部兜底回收 |
| `R-NVME` | high | 固件协议 | 结构体打包字节序错误、buffer 长度不匹配、扇区大小硬编码；指令超时无 abort / 设备复位 | 统一字节序对齐配置常量；扇区参数使用设备实际查询值；指令超时执行 abort 与硬件复位流程 |
| `R-REPRO` | medium | 可重复性 | `time.time()` / `datetime.now()` / `random` 未固定种子写死在测试函数；用例依赖执行顺序或上一条用例的残留状态 | 计时改 `perf_counter()`；时间/随机用 mock 或固定种子隔离；用例独立自洽 |
| `R-FIX` | high | fixture 隔离 | pytest fixture 无 `yield`/teardown 配对；`session`/`module` 级 fixture 共享可变硬件状态且不恢复；setup 抛错后资源未释放 | fixture 前置资源必须配对 teardown（`yield` + `finally`）；共享 fixture 显式复原状态；临时资源用专用路径兜底释放 |
| `R-SKIP` | medium | 跳过与平台适配 | `pytest.skip` / `skipif` 缺 `reason`；`xfail` 无原因且无 `strict`；平台路径 / root 权限硬编码无降级 | skip / xfail 必填 reason；系统 / 设备路径加存在性判断；配降级兜底 |
| `R-CI` | high | CI 并行 | 多用例共用临时路径 / 硬件无隔离；独占 SSD 未加串行标记；pytest-xdist 共享可变全局状态 | 临时目录拼 PID / 用例 ID 隔离；独占硬件加串行标记或文件锁；xdist 用 `loadscope` + 资源锁 |
| `R-PARAM` | medium | pytest 参数化 | `@pytest.mark.parametrize` 参数组合重复；缺 `ids` 导致失败用例无法定位；参数化内再写死循环；可变默认参数被多个参数共享 | 去重参数组合；复杂参数补 `ids`；参数化与循环二选一；可变对象按用例构造 |
| `R-RAISE` | high | pytest.raises 断言 | `pytest.raises(Exception)` 范围过宽；只断言异常类型、不校验 `match` 消息；`raises` 块内包含无关代码导致假命中 | 捕获具体异常类型；补 `match=` 或断言消息；`raises` 块只包被测调用 |
| `R-MOCK` | high | mock / patch 作用域 | `patch` 目标写成定义处而非使用处；`patch` 未用 `with` / 装饰器导致未生效；mock 了被测对象自身使测试失去意义；mock 未清理污染其它用例 | patch 指向使用处；用 `with` / 装饰器确保生效与还原；只 mock 外部依赖；必要时 `monkeypatch` 自动清理 |
| `R-TMP` | medium | 临时路径隔离 | 硬编码 `/tmp/xxx` 共用路径；并行用例写同一临时文件；临时文件未清理 | 优先 `tmp_path` / `tmp_path_factory`；必须共用时拼 PID / 用例 ID；用 fixture 自动清理 |
| `R-OTHER-IMPACT:*` | high | 跨文件影响 | 函数签名 / 常量 / schema / import 路径 / fixture 变更后，其他文件的 caller / 引用方未同步（必须实际 read 到调用点） | 在 summary 点出 caller；caller 在 diff 内则产 apply 建议；不在 diff 内则写 `> 跨文件影响:` 行 |
| `R-OTHER:*` | low | 野生问题兜底 | 未命中上表但**确有影响**的问题：标识符拼写、易混淆死代码、会误导的过期注释、导致误用的重复定义（须先通过「⛔ 检视边界」） | 以 `R-OTHER:<具体短描述>` 标注；高频问题应沉淀为新规则键 |

**pytest 专项落地要点**
- fixture：`session` 级 fixture 若修改设备配置/挂载/全局变量，必须 teardown 复原，否则后续用例互相污染（`R-FIX` / `R-CLEAN`）
- 参数化：同一组参数重复出现、或只差一个字段的组合，优先 `parametrize` 而不是复制函数（`R-PARAM`）
- 异常：业务失败用断言，不用 `try/except` 静默通过；期望异常用 `pytest.raises(具体类型, match=...)`（`R-RAISE` / `R-ERR`）
- mock：patch 的目标是"被使用的位置"，不是"定义的位置"；mock 硬件层时保留真实协议数据结构（`R-MOCK`）
- 临时资源：`tmp_path` 优先；需要固定路径时按 `PID + 用例名` 隔离（`R-TMP` / `R-CI`）

**通用规则使用方式**
- 每命中一条**且有影响**（未被「⛔ 检视边界」过滤）→ 产一条 suggestion，`rationale` 字段以对应表内 **规则键**（如 `R-ERR` / `R-LOOP`）开头
- 同一行命中多条 → 按上面的「规则仲裁」只取一条，避免重复
- **命中且有影响才产 suggestion**（锚点须在 diff 的 `+` 行）；属于「⛔ 检视边界」的候选直接丢弃，
  数量上限由程序统一截断
- 只有关联代码不在 diff 内（GitLab 无法 Apply）时才写 summary 文字

### 兜底：未命中表内 R-* 规则 但确有价值的野生问题

表内 R-*（非穷举）之外，diff 中可能命中"不在表内、但确有价值"的问题。

**兜底规则**：
- 确有影响时所有 severity 都可产 suggestion；severity 如实标注（`high` / `medium` / `low`），由程序排序与截断；
  其中 `low` 须先通过「⛔ 检视边界」的 low 两问自检（不改它具体会出什么后果？后果能在本 diff 验证吗？），答不上就丢弃
- `rationale` 字段以 `R-OTHER:<简短描述>` 开头，常用前缀：
  - `R-OTHER:magic_number` — 硬编码魔法数（端口 / 超时 / 重试等）
  - `R-OTHER:typo` — **标识符**拼写错误（注释 / docstring 文案拼写不报）
  - `R-OTHER:dead_code` — diff 引入后立即未使用 / 死代码
  - `R-OTHER:naming_inconsistency` — 同一含义两种命名并存**导致误用**（单纯口味不报）
  - `R-OTHER:duplicated_definition` — 与已有常量 / 函数重复定义且会分叉
  - `R-OTHER:stale_comment` — 注释与代码实际行为不一致**且会误导**
  - `R-OTHER:unused_param` — 函数参数定义后从未引用
- `rationale` 末尾加一句"未命中表内 R-* 规则，原因: <具体理由>"，方便事后归类
- **不要为了凑数硬编** — 真的没找到就空着；野生规则（`R-OTHER:<具体描述>`）用于统计频率，后续高频可升级为新的 `R-*` 规则键并在 `rules/general_rules.md` 规则表中显式声明。

### ⛔ 严禁规则键字面占位符

**绝对不要**把以下占位词当作真实的 `rule_key` 写进 `rule_keys` 列表或 `rationale` 字段：

- ❌ `R-XXX` / `R-X` / `R-FOO` / `R-TODO` / `R-...` 等一切"看着像规则但其实是占位"的字符串
- ❌ 把 `R-XXX` 这种字面字符串当 `R-*` 通用规则的代号 — 它**不存在**，只是行文占位

**命中不上的统一写法**：

| 情况 | 正确写法 | 例子 |
|---|---|---|
| 兜底违规（不在上表 R-* 规则内）| `R-OTHER:<具体短描述>` | `R-OTHER:magic_number` / `R-OTHER:typo` |
| 跨文件影响类违规 | `R-OTHER-IMPACT:<具体短描述>` | `R-OTHER-IMPACT:caller_param` / `R-OTHER-IMPACT:import_path` |
| 不写 rule_key 但要保留 | 写单行 `> 无法自动应用: ...` 段，不强行命名新键 | `> 无法自动应用: a.py L20 需人工评估` |

历史 telemetry.db 里发现过 `R-XXX` 字面作为 `rule_key` 落到 suggestions 表里 (LLM 幻觉)，这会让周报"问题类型"段出现 `XXX ×N` 的诡异条目。**因此从本次起 `R-XXX` / 类似占位词严禁写出**。
