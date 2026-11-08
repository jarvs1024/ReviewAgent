# 生效配置（生成文件）

> 本文件由下列命令生成，**不要手工编辑值**。改配置请改 `config/instance.env` /
> `config/profiles/<name>/review.yaml`，再重新生成。
> 以下命令只重写「生成输出」小节之后的段落，保留本文件上方的说明与指纹表格：
>
> ```bash
> tmp=$(mktemp)
> GITLAB_URL=https://gitlab.example.invalid GITLAB_PERSONAL_ACCESS_TOKEN=stub \
>   GITLAB_WEBHOOK_SECRET=stub REVIEW_PROFILE=ssd-python-autotest \
>   .venv/bin/python -m reviewagent config show --diff-default --sources > "$tmp"
> # 保留到「生成输出」标题（按前缀匹配，避免正文重复出现完整标题）
> sed -n '1,/^## 生成输/p' docs/CONFIG.md > "$tmp.doc"
> echo >> "$tmp.doc"
> cat "$tmp" >> "$tmp.doc"
> mv "$tmp.doc" docs/CONFIG.md
> ```
>
> GitLab 三项必填项生成时使用临时 stub 值（secret 一律渲染为 `***`，`GITLAB_URL`
> 在 schema 中也按 secret 处理），不把真实凭证写进文件。
>
> `--diff-default` 只列出与 schema 默认值不同的字段，因此本文件很短不代表配置缺失：
> 它说明该实例几乎全部字段都落在 `reviewagent/settings/schema.py` 的默认值上。
> 完整字段与默认值以 `reviewagent/settings/schema.py` 为唯一来源；实例平面
> （必须写进 `config/instance.env` 的键）见
> [`config/examples/instance.env.example`](../config/examples/instance.env.example)，
> 场景平面见 [`docs/RULES.md`](RULES.md)、`profiles/` 与
> `config/profiles/<name>/review.yaml`。

## 两个指纹

| 指纹 | 计算对象 | 用途 |
|---|---|---|
| `profile_fingerprint` | `profile.yaml` 合并后的场景值（含用户 `config/profiles/<name>/review.yaml`）+ 其引用文件（prompts / rules / dynamic_rules / actions_file）逐个内容的 SHA-256 | 场景包或用户调参任何改动都会变。`reviewagent config check` 打印它；RQ job manifest 携带它，`verify_job` 用它拒绝「API 与 worker 加载了不同场景」的 job |
| `config_fingerprint` | `SCHEMA` 全部字段生效值的 SHA-256（secret 以 `***` 参与哈希，Path 转字符串、tuple 转 list） | 实例配置（env / data_dir / redis 等）改动会变。写入 RQ job manifest，worker 接受 job 前比对，防止 API 与 worker 配置漂移 |

两个指纹都由启动路径（`bootstrap.startup`）输出在 `config.resolved` 与
`bootstrap.ready` 日志行里，可用
`journalctl -u reviewagent-webhook -u reviewagent-worker` 核对；`GET /readyz`
只返回 `ok` / `unready` + 原因列表（profile / RuleSet / instance / SQLite / Redis），
切换场景或升级后先看它再放流量。

## 生成输出

gitlab_pat: ***  # source=env
gitlab_url: ***  # source=env
gitlab_webhook_secret: ***  # source=env
improve_full_files: 5  # source=config
improve_keyword_paths: /services/,/api/,/core/,/main.,/models/,/handlers/,/routers/,/pages/,/locators/,/fixtures/,/conftest.py  # source=config
improve_max_suggestions: 10  # source=config
improve_min_severity: medium  # source=config
improve_review_mode: test  # source=config
improve_skip_test_paths: conftest.py  # source=config
improve_test_sample_max: 50  # source=config
improve_test_sample_paths: testcase/  # source=config
review_profile: ssd-python-autotest  # source=env
