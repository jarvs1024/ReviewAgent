# ReviewAgent 配置目录

本目录是**用户改配置的唯一入口**。其它位置（`profiles/`、`reviewagent/settings/schema.py`）
是场景包作者与代码维护者的领域，日常调参不需要改。

## 目录结构

```
config/
├── README.md                          # 本文件：配置地图与切换流程
├── instance.env                       # 本机实例配置（含密钥，不入库，权限 600）
├── profiles/
│   └── ssd-python-autotest/
│       └── review.yaml                # 该场景的检视调参（入库，禁止放密钥）
└── examples/
    └── instance.env.example           # instance.env 模板（不含真实密钥）
```

## 三个配置平面

| 平面 | 文件 | 管什么 | 是否入库 |
| --- | --- | --- | --- |
| 实例 | `config/instance.env` | GitLab 连接、LLM、Redis、数据目录、场景选择 | 否（含密钥） |
| 场景 | `config/profiles/<name>/review.yaml` | 检视范围 / 打分 / 限额 / 上下文 / 周报展示 | 是（纯调参） |
| 规则 | `profiles/<name>/rules/*.md`（随场景包发布） | 规则条文、规则键索引、verbatim/structured 渲染 | 是（随代码库） |

场景包本体（`profiles/<name>/`：prompt 模板、规则条文、能力开关）随代码发布，
不在本目录维护；规则平面是其中的内容，用户不需要也不应该在 `config/` 下覆写它。
生效优先级（后者覆盖前者）：

```
schema 默认值（reviewagent/settings/schema.py）
  < config/profiles/<name>/review.yaml（本文件所在目录，用户调参）
  < 环境变量（仅实例平面；场景键的环境变量仍可用于临时调试）
```

`config/instance.env` 只写**非默认值**。留空的键会回退 `reviewagent/settings/schema.py`
里的默认值，所以不要为了"看得全"把默认值抄进来。

## 改完怎么生效

```bash
# 1. 校验（缺必填、类型错误、场景包非法都会在这里失败，退出码 2）
.venv/bin/python -m reviewagent config check

# 2. 看生效值与来源（env / config / profile / default）
.venv/bin/python -m reviewagent config show --sources

# 3. 重启（Web / Worker / 周报 Worker 必须一起重启，指纹才一致）
bash scripts/restart_local.sh
```

`config show --diff-default` 只列出与默认值不同的项，适合快速核对。

## 切换场景包

```bash
# 1. 查看可用场景包（* 标记当前生效）
.venv/bin/python -m reviewagent profile list

# 2. 切换：自动校验目标场景包，并把 REVIEW_PROFILE / REVIEWAGENT_DATA_DIR
#    原位改写入 instance.env（保留注释与权限；目标数据目录已绑定其它
#    场景包时拒绝执行）
.venv/bin/python -m reviewagent profile switch <场景名>

# 3. 重启全部服务（Web + 所有 Worker + 周报 Worker）
bash scripts/restart_local.sh
```

切换规则：

* 目标数据目录 = 现目录的同级 `<场景名>` 目录（相对目录则沿用 `data/<场景名>`）；
  `instance.json` 会记住首次绑定的场景包，同一目录换场景会被拒绝。
* `REVIEW_PROFILE` / `REVIEWAGENT_DATA_DIR` 手改 `instance.env` 同样生效，
  `profile switch` 只是替你完成"校验 + 改写 + 重启提示"。

场景包自身的目录位置默认是仓库下的 `profiles/`；只有把场景包放到别处时才需要
`REVIEW_PROFILE_ROOT=/path/to/profiles`。用户调参放在
`config/profiles/<场景名>/review.yaml`，与场景包所在位置无关。

## 注意

* `instance.env` 会被 systemd `EnvironmentFile` 和 shell `source` 同时读取，
  因此**注释必须单独成行**，不要写行尾 `#`（systemd 不会剥离行尾注释）。
* 含空格的值必须加双引号，例如 `REVIEWAGENT_WEEKLY_CRON_SCHEDULE="Mon 09:00"`。
* `review.yaml` 只允许 `file_filter` / `scoring` / `limits` / `context` /
  `repo_context` / `reporting` 六组（`reporting` 仅开放展示/扫描类键：
  `weekly_title`、`repo_scan_extensions`、`repo_scan_entry_files`）；
  写错组名或字段名会启动失败（fail-fast），避免"改了没生效"。
* 调参只写在 `review.yaml`；写在场景包 `profile.yaml` 的调参组会在启动时
  报错并提示迁移（preset/extends 继承层已删除）。
* 密钥只放 `instance.env`；`review.yaml` 会入库，禁止写入 token / webhook。
* 本目录默认取 `profiles/` 的同级目录；整个目录搬到别处时才需要
  `REVIEW_CONFIG_DIR=/path/to/config`（一般不用改）。
