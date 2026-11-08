"""业务配置 — L0 配置层入口.

字段默认值的唯一来源是 `reviewagent/settings/schema.py`（SCHEMA），本文件里的
dataclass 默认值必须与之逐字一致（`tests/test_settings_schema.py` 会把两者对齐）。

启动时 `Config.from_env()` 一次性加载，启动后 frozen 不可变.
模块级 `config` 是一个"活的"只读代理：`apply_scenario()` 只替换代理背后的
`_current`，因此所有在 import 时 `from reviewagent.config import config` 的模块
都会立即看到新值，而不需要重新绑定全局.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

from reviewagent.settings import (
    SCHEMA_BY_NAME,
    UNSET,
    coerce_scenario,
    profiles_root,
    validate,
)


@dataclass(frozen=True)
class Config:
    # ---- GitLab ----
    # 前三项必须显式提供（env / 构造参数），没有回退值.
    gitlab_url: str
    gitlab_pat: str
    gitlab_webhook_secret: str
    gitlab_bot_username: str = "review-agent"
    gitlab_disable_bot_loop_check: bool = False  # True 则 is_bot() 永远返回 False；测试/临时场景用 (避免 bot 误判)

    # ---- opencode ----
    opencode_url: str = "http://localhost:4096"
    opencode_username: str = "opencode"
    opencode_password: str = ""
    opencode_model: str = "deepseek/deepseek-v4-flash"
    opencode_timeout: int = 900  # opencode HTTP 请求默认超时（秒）

    # ---- LLM Provider 适配层 ----
    # 通过 LLM_PROVIDER=opencode|qodercli 在两者之间切换；详见 docs/LLM_PROVIDER_ADAPTER.md.
    llm_provider: str = "qodercli"          # "opencode" | "qodercli" (qodercli 是当前推荐路径)

    # ---- qodercli 专属配置 ----
    qodercli_node_path: str = ""            # node 可执行文件；空则用 shutil.which("node")
    qodercli_js_path: str = ""              # qodercli.js 绝对路径；空则用 readlink -f $(which qodercli)
    qodercli_model: str = "DeepSeek-V4-Flash"
    qodercli_timeout: int = 600             # subprocess 默认超时（秒）
    # ---- qodercli driver ----
    # subprocess 是当前唯一启用路径；ACP 代码保留用于上游 stdin hang 修复后的复测.
    qodercli_permission_mode: str = ""        # Headless --permission-mode (accept_edits / bypass_permissions / dont_ask / auto). 空字符串 = 不传.
    qodercli_max_turns: int = 20               # Headless --max-turns 上限 (兜底, 防 qodercli 无限循环; 0 = 不传)
    qodercli_fallback_model: str = ""          # 备用模型 (空=不回退); 主模型失败时自动切换, 每个任务独立尝试主模型

    # ---- qoder-agent-sdk 专属配置 (QODER_SDK_*，替换 subprocess 模式的新路径) ----
    # SDK 通过 PAT (Qoder Personal Access Token) 鉴权，不再依赖本地 qodercli login 状态。
    # 通过 LLM_PROVIDER=qoder-sdk 切到 SDK；保留 qodercli 字段是为了旧路径兜底/灰度。
    qoder_sdk_pat: str = ""                    # PAT (QODER_SDK_PAT)；切换 LLM_PROVIDER=qoder-sdk 时必填
    qoder_sdk_edition: str = "intl"            # SDK 品牌: intl=国际版(qoder-agent-sdk) / cn=国内版(qodercn-agent-sdk)
    qoder_sdk_cli_path: str = ""              # qodercli 可执行文件绝对路径；空则按 edition 的 CLI 命令名查 PATH
    qoder_sdk_model: str = ""                  # 主模型；空则回落 qodercli_model
    qoder_sdk_fallback_model: str = ""         # 备用模型；主模型失败时自动切换
    qoder_sdk_timeout: int = 600               # 单次 query 超时（秒），与 asyncio.wait_for 配合
    qoder_sdk_max_turns: int = 0                # 0=不传；>0 写入 options.max_turns
    qoder_sdk_permission_mode: str = ""        # default / acceptEdits / plan / bypassPermissions / yolo / dontAsk / auto；空=不传
    qoder_sdk_disallowed_tools: tuple[str, ...] = (  # 禁用的工具列表（与原 subprocess 一致）
        "write", "edit", "bash", "webfetch", "websearch",
    )

    # ---- Redis / RQ ----
    redis_url: str = "redis://localhost:6379/0"
    rq_queue_name: str = "review"
    rq_weekly_queue_name: str = "review-weekly"  # 周报专用队列，与 review 命令队列物理隔离（缺省派生自主队列）
    rq_worker_timeout: int = 1200  # RQ job_timeout（秒），必须 >= max(QODERCLI_TIMEOUT, OPENCODE_TIMEOUT) + 300s buffer，否则 RQ 会 SIGKILL horse 引发 stuck-run
    rq_worker_count: int = 3  # 并发 worker 数

    # ---- 命令链（每个 MR 按顺序串行执行）----
    # 检视顺序统一为 describe → improve: describe 先产出 MR title + description 概要,
    # 然后 improve 在该概要上做代码级建议. 之前 push_commands=("improve",) 单跑,
    # 会让 push 触发的 chain 不带 describe, 跟 MR open 触发的 chain 行为不一致;
    # 历史 (UI) 上会看到 "improve → describe → improve" 错序. 统一后:
    #   - MR open / push 都跑 describe → improve
    #   - describe 会刷新 title + description (幂等, 跑多次无害)
    #   - cooldown 60s 仍兜底, 避免 push 风暴时重复入队
    pr_commands: tuple[str, ...] = ("describe", "improve")
    push_commands: tuple[str, ...] = ("describe", "improve")

    # ---- 存储 ----
    data_dir: Path = field(default_factory=lambda: Path("./data"))
    log_level: str = "INFO"
    log_format: str = "text"        # text=现网观感(默认) / json=机读单行 (LOG_FORMAT)
    log_rotation: str = "single"    # single=loguru 进程内轮转 / none=外部 logrotate 或 journald 接管 (LOG_ROTATION)

    # ---- 场景包 (Profile) ----
    review_profile: str = ""                  # 空=内置默认场景（当前 Python/SSD 行为）
    review_profile_root: str = field(default_factory=profiles_root)  # profiles/ 根目录
    review_config_dir: str = ""               # 用户配置目录；空=与 profiles/ 同级

    # ---- 限制 ----
    mr_cooldown_seconds: int = 60
    max_review_calls_per_mr: int = 30  # 同 MR 最多 30 轮 review, 防无限循环; 0=不限

    # ---- Diff 限制 ----
    max_diff_chars: int = 50000              # 超过则跳过检视并评论告知
    opencode_max_diff_chars: int = 20000     # opencode prompt 内联 diff 截断阈值

    # ---- AGENTS.md 仓库规则 ----
    repo_context_files: tuple[str, ...] = ("AGENTS.md",)  # 规则入口文件（跟随其中 Markdown 链接发现规则）
    repo_context_rules_dir: str = ""          # 显式规则目录覆盖；空=从入口文件链接自动发现
    rule_key_prefix: str = "auto"             # 规则键前缀；auto=任意 <前缀>-RULE-<键>
    repo_context_max_lines: int = 2000       # 规则文件最大行数 (超出截断)

    # ---- improve 并行 + 限流 ----
    improve_parallel_workers: int = 3        # 按文件分块并行调 opencode 的路数
    improve_full_files: int = 20            # full source 桶文件配额, 超出降级 partial/patch (0=不限)
    improve_full_source_max_bytes: int = 0  # full 源码块字节预算 (E2BIG 防线): 0=自适应(128KB参数上限−repo_context−规则块−diff估算), >0=固定值, <0=关闭
    improve_keyword_paths: tuple[str, ...] = (  # 文件优先级 — 关键路径加分 (排序权重)
        # 业务核心 (web 通用)
        "/services/", "/api/", "/core/", "/main.",
        "/models/", "/handlers/", "/routers/",
    )
    improve_skip_test_paths: tuple[str, ...] = (  # 测试/配置文件路径跳过 (不进 LLM)
        "tests/", "/tests/", "test_", "_test.", ".test.", "conftest.py",
    )
    improve_partial_context_lines: int = 20  # partial 模式 ±N 行 (overflow 文件 context 兜底)
    improve_patch_context_lines: int = 3    # patch 模式 diff 行 ±N 行最小 context (V7: 不再纯行号)
    improve_max_suggestions: int = 15        # 单次最大 inline 建议数 (0=不限, 超出只写总览)
    improve_max_suggestions_per_file: int = 5  # 单文件最大建议数 (V7: 防噪音文件吃光槽位, 0=不限)
    improve_min_suggestions_per_file: int = 2  # V9: 单文件最少建议数 (priority 加权预算的下限, 保证高优先级文件不被饿死)
    improve_min_score: int = 0               # 改进建议最低分数 (0=不过滤, 建议值 20~40)
    improve_min_severity: str = ""           # 最低严重度过滤 (空=不过滤, 可选: critical/high/medium/low)

    # ---- V7 检视模式 + priority 权重 (可配, 适配业务/测试项目) ----
    improve_review_mode: str = "auto"        # "auto"/"business"/"test": 默认 auto (按文件特征自动判定)
    improve_priority_weight_diff: float = 20.0       # log1p(diff_size) 系数
    improve_priority_weight_keyword: float = 25.0    # 关键路径加分
    improve_priority_weight_density: float = 20.0    # 高密度改动加分 (改动占比>30%)
    improve_priority_weight_test_feature: float = 15.0  # 测试特征密度加分 (test 模式)
    improve_test_feature_keywords: tuple[str, ...] = (  # 测试特征关键字 (命中→高风险测试文件)
        "pytest.fixture", "fixture(", "@fixture",
        "mock.patch", "MagicMock", "patch(",
        "parametrize", "@pytest.mark",
        "setup_module", "teardown_module", "setup_class", "teardown_class",
        "setUp", "tearDown",
        "assert",
        "yield fixture",
        "pytest.raises", "contextmanager", "tmp_path", "caplog", "monkeypatch",
    )

    # ---- 检视文件过滤 ----
    review_exclude_extensions: tuple[str, ...] = (".md", ".doc", ".docx", ".txt", ".rst", ".csv", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico")

    # ---- 文件筛选 / 抽样护栏 (2026-08-17) ----
    improve_max_files_to_review: int = 60  # D: 送 LLM 的文件数硬上限 (必保新增+关键路径, tail 截断)
    improve_new_file_full: bool = True  # A: 新增文件优先 (加分+必保+建议截断豁免)
    improve_test_sample_paths: tuple[str, ...] = ()  # C: 抽样目标路径模式 (空=不抽样)
    improve_test_sample_max: int = 0  # C: 抽样上限 (0=不抽样, 正整数=确定性抽样 N 个)

    # ---- V10 批次合并 + 大 diff 截断 (2026-08-18) ----
    improve_batch_enabled: bool = True  # 小 diff 文件合并批次 (减少 LLM 调用次数)
    improve_batch_max_diff_lines: int = 60  # 参与 合并的单文件 diff 行数上限 (超过则独立 chunk)
    improve_batch_max_files: int = 5  # 单批次最多合并文件数
    improve_batch_max_total_lines: int = 300  # 单批次累计 diff 行数上限
    improve_clip_diff_lines: int = 800  # 单文件 diff 行数超过此值时截断 (0=不截断)

    # ---- 周报 (instance 平面；迁移自 reporting/config.py，唯一 schema 见 settings/schema.py) ----
    weekly_enabled: bool = True
    weekly_target_project_id: int = 0
    weekly_target_branch: str = "main"
    weekly_timezone: str = "Asia/Shanghai"
    weekly_collectors: tuple[str, ...] = ("telemetry", "merged_mrs", "repo_scan")
    weekly_notifier: str = "dingtalk"
    weekly_dingtalk_webhook_url: str = ""  # 空 = dry_run；别名 DINGTALK_WEBHOOK 兼容
    weekly_dingtalk_secret: str = ""
    weekly_dingtalk_dry_run: bool = True
    weekly_dingtalk_retry: int = 3
    weekly_md_chunk_limit: int = 18000
    weekly_report_title: str = ""  # 空 = 用 profile.reporting.weekly_title
    weekly_report_emoji: str = "📊"
    weekly_cron_schedule: str = "Mon 10:30"
    weekly_dashboard_url: str = "http://127.0.0.1:8080/code-review"

    # ---------- 派生路径 ----------
    @property
    def sqlite_path(self) -> Path:
        return self.data_dir / "telemetry.db"

    @property
    def repos_dir(self) -> Path:
        """bare repos 持久化目录（多 MR 共用 git 对象）."""
        return self.data_dir / "repos"

    @property
    def worktrees_dir(self) -> Path:
        """git worktree 临时目录（每次任务临时，应放 tmpfs）."""
        return Path("/tmp/reviewagent-worktrees")

    @property
    def weekly_reports_dir(self) -> Path:
        return self.data_dir / "weekly_reports"

    # ---------- 工厂 ----------
    @classmethod
    def from_env(cls) -> Config:
        """环境变量加载统一委托 settings.binder（唯一解析入口）."""
        from reviewagent.settings.binder import from_env

        return from_env()


_current: Config = Config.from_env()


class _ConfigProxy:
    """模块级稳定引用；scenario 应用时只替换 `_current`，不重绑全局.

    `dataclasses.replace/asdict/fields` 都按真实 dataclass 处理本代理：
    `__dataclass_fields__` 指向 `Config` 的真实字段映射（不是空 dict），因此
    `replace(replace(config, a=...), b=...)` 的链式覆盖、无参 `replace(config)`
    与 `asdict(config)` 都会返回真实值。`replace` 会调用
    `proxy.__class__(**all_fields)`，这里用 `_current` + changes 生成一份不可变快照.

    没有 `__slots__`：既有测试用 `object.__setattr__(config, ...)` 就地覆盖单个字段
    （绕过 frozen）。这是 `test_improve_strategy` 依赖的测试旋钮，语义上是一层
    独立于 scenario 的 shadow 覆盖层，优先级高于 `_current`/scenario：普通读取先
    命中实例 __dict__，只有未被遮蔽的字段才转发到 `_current`。普通赋值
    `config.x = ...` 仍被 `__setattr__` 拒绝.
    """

    # 真实字段映射（dataclasses.replace/asdict/fields 依赖它遍历字段）。
    __dataclass_fields__: dict[str, Any] = Config.__dataclass_fields__

    def __init__(self, **overrides: Any) -> None:
        if overrides:
            base: dict[str, Any] = {f.name: getattr(_current, f.name) for f in fields(Config)}
            base.update(overrides)
            object.__setattr__(self, "_snapshot", Config(**base))
        else:
            object.__setattr__(self, "_snapshot", None)

    def _target(self) -> Config:
        try:
            snapshot = object.__getattribute__(self, "_snapshot")
        except AttributeError:  # __init__ 尚未执行（异常路径）时退回当前配置
            return _current
        return _current if snapshot is None else snapshot

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target(), name)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(f"config is read-only; use apply_scenario() (field '{name}')")

    def __repr__(self) -> str:
        return repr(self._target())


config: Config = _ConfigProxy()  # type: ignore[assignment]


def current() -> Config:
    """返回调用时生效的 Config（scenario 应用后即为新值）."""
    return _current


def apply_scenario(values: Mapping[str, Any]) -> None:
    """把场景包的值应用到当前配置（不落盘、不改 env）.

    场景平面空值语义（刻意不复用 env binder 的 `"" -> 未设置`）：
      * `None` = 继承：跳过该字段，保留当前值；
      * `""` / `()` / `[]` = 显式空值：照常写入，不回退 schema 默认值；
      * 字符串字段保留 `""`；tuple 字段的显式空保留 `()`。

    权限边界：未知字段 -> KeyError；secret 字段或 `scope="instance"` 的字段
    -> PermissionError（消息含字段名），避免运行时静默改 redis/data_dir 等实例参数。
    类型不符 -> ValueError（消息含字段名）。
    """
    global _current
    updates: dict[str, Any] = {}
    for name, raw in values.items():
        spec = SCHEMA_BY_NAME[name]
        if spec.secret:
            raise PermissionError(f"scenario must not set secret field: {name}")
        if spec.scope != "scenario":
            raise PermissionError(
                f"scenario must not set instance-scoped field: {name} (scope={spec.scope})"
            )
        value = coerce_scenario(spec, raw)
        if value is UNSET:  # 只有 None = 继承；显式空值不会走这里
            continue
        updates[name] = validate(spec, value)
    if updates:
        _current = replace(_current, **updates)
