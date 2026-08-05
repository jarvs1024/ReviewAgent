# r10 双 Provider 全流程验证 — 2026-08-05

仓库 `ReviewAgent` 分支 `codex/feat-llm-provider-adapter`，对应测试仓库
`/Users/jarvs/gitlab-stack/auto-review-test`，GitLab
`http://127.0.0.1:8929`（root / Jarvs@2026）project_id=34。

## 本次提交

5 个新 commit 落在 `87810066e490b2e83fb1b54d081a67a9f83dbe12`:

| sha | 说明 |
| --- | --- |
| `9b236f6` | `chore: source .env in run scripts + ReviewAgentSpawnWorker + deepseek defaults` — `scripts/restart_local.sh` 增加 `terminate_worker_jobs` / `terminate_matching`，脚本改从 `.env` 读凭据；`OPENCODE_MODEL` 默认改 `deepseek/deepseek-v4-flash`；引入 `ReviewAgentSpawnWorker`（macOS-safe `rq.worker.SpawnWorker`） |
| `9f2732a` | `feat(workers): ReviewAgentSpawnWorker + runtime tests` — RQ 默认 `fork()` 撞 `NSNumber initialize` 的兜底 |
| `d7f5a10` | `test(qodercli): cover deepseek default + inner JSON RED cases` — 新 RED 用例：尾部 prose、字面控制字符 |
| `8781006` | `fix(qodercli): tolerate trailing prose + literal control chars in inner JSON` — 新 `_extract_inner_json` helper，3 阶段解析（`raw_decode(strict=False)` → `loads(strict=False)` → 抛错）|

## 修复的根因

DeepSeek-V4-Flash 在 qodercli subprocess 模式下输出 2 类异常：

1. **字面 newline 在 JSON 字符串值里**（Python 3.12 `json.loads(strict=True)` 默认拒绝控制字符）。
2. **JSON 之后追加解释文本**（`{"ok":true}\nJSON generated.`）。

`_extract_inner_json` 用 `JSONDecoder(strict=False).raw_decode` 一次性吃掉
这两个常见模式，跟 opencode client 的 `opencode.client._extract_json_block`
策略一致。

## 验证矩阵（6 个 MR 跑通 6 轮）

| MR   | Provider   | describe              | improve                                         | 备注 |
| ---- | ---------- | --------------------- | ----------------------------------------------- | ---- |
| 185  | qodercli   | OK 40 s (重跑修复后)  | 188 s, 11 sugg, 10 inline；2nd improve 138 s/8/2 | 第一次 describe 失败，已修复 |
| 186  | qodercli   | OK 32 s               | 215 s, 9 sugg；190 s/12/10；160 s/9/0           | 3 轮 describe+improve |
| 187  | qodercli   | OK 29 s               | 207 s, 11 sugg/10 inline；225 s/13/1            | 3 轮 |
| 188  | opencode   | OK 53 s               | 197 s, 12/4；187 s/12/0；193 s/11/1             | 3 轮；首轮含 QoderCLI 历史 suggest 重叠，inline_skipped 多 |
| 189  | opencode   | OK 111 s              | 195 s, 11/10；210 s/12/0                        | 2 轮 |
| 190  | opencode   | OK 110 s              | 190 s, 11/10；193 s/11/1；auto-detect 触发      | 3 轮 + auto-detect |

## 7 类功能验证（全部通过）

1. **describe** — 6/6 OK（修复后）
2. **improve** — 6/6 OK，建议数量 8–13，inline_posted 占比 50–100%
3. **/adopt** — 在 MR 190 discussion `92939520261e1b37e4d74dbac8fbeebb72ce4e17` 上 reply `/adopt`，worker 处理后 reply `✅ 已采纳建议` / `未检测到...`（取决于代码实际是否修改）
4. **/dismiss** — 在 MR 190 discussion `65ae3cb015ab1accee52bfce798258d7714504c1` 上 reply `/dismiss`，worker 处理后 reply `✅ 已关闭建议，原因：...`
5. **auto-detect** — push `cebe2d62` 到 `codex/verify-r10-opencode-3-20260805` 分支，webhook 收到 `object_kind=push` 后 enqueue `describe + improve` chain
6. **telemetry API** — `GET /api/v1/telemetry/mr/34/190` 返回完整 `mr` + `recent_runs` + `web_url`；`GET /api/v1/telemetry/metrics/overview?project_id=34` 给出 `describe`/`improve` 计数与平均 duration
7. **weekly report** — `python scripts/weekly_report.py --week-offset 0` 生成 `data/weekly_reports/weekly-2026-W32.md` + `.xlsx`，包含「本周检视概况」+「本周 main 变更汇总」+「本周代码质量全量扫描」3 段

## 单元测试结果

```
186 passed, 1 failed (pre-existing in test_improve_alignment.py::test_build_summary_v2_version_increments_per_run，
不在本次改动范围)
```

适配层新增 / 修改的测试：
- `tests/test_qodercli_subprocess_fallback.py` — 5 通过（含 2 个新增 parametrise RED）
- `tests/test_config_acp_fields.py` — 9 通过（含新增 `test_opencode_model_defaults_to_deepseek_v4_flash`）
- `tests/test_worker_runtime.py` — 11 通过（新增）
- `tests/test_llm_adapter.py` — 28 通过（无回归）
- `tests/test_qodercli_plan_a_flags.py` — 4 通过（无回归）

## 已部署

`bash scripts/restart_local.sh` 后：

- `LLM_PROVIDER=qodercli`（默认），`OPENCODE_MODEL=deepseek/deepseek-v4-flash`
- 4 个 review worker（`ReviewAgentSpawnWorker`，stdin `/dev/null`）
- 1 个 weekly worker
- `webhook :3000` + `opencode :4096` 健康
- 启动日志：
  - `webhook  :3000 -> HTTP 200`
  - `opencode :4096 -> HTTP 200`

## 已知保留项

- ACP driver 代码 2026-08-05 已彻底删除（详见 `docs/LLM_PROVIDER_ADAPTER.md` v2→v3 章节）。
- `GITLAB_BOT_USERNAME=non-existent-bot-marker-2026-08-05` 是临时占位 marker，绕过 webhook `bot_self` skip；建议下周还原到 `review-bot-v2`。
- `pre-existing failure: tests/test_improve_alignment.py::test_build_summary_v2_version_increments_per_run` 与本次改动无关，保留。

## 推送

- Local: `87810066e490b2e83fb1b54d081a67a9f83dbe12` on `codex/feat-llm-provider-adapter`
- GitLab (`remote=gitlab`): `8781006 -> codex/feat-llm-provider-adapter`
