"""Pytest conftest — set baseline env so importing reviewagent.config succeeds.

`config = Config.from_env()` runs at module import time, which means it runs
before any test fixture can patch env. We set sensible defaults at import
time here; tests that need different values should use `monkeypatch.setenv`
inside the test body (monkeypatch will tear them down at fixture end).
"""
from __future__ import annotations

import os
from collections.abc import Iterator

_BASELINE = {
    "GITLAB_URL": "https://gitlab.example.test",
    "GITLAB_PERSONAL_ACCESS_TOKEN": "test-token",
    "GITLAB_WEBHOOK_SECRET": "test-secret",
    "QODERCLI_NODE_PATH": "/usr/bin/node",
    "QODERCLI_JS_PATH": "/opt/qodercli.js",
    "QODERCLI_MODEL": "DeepSeek-V4-Flash",
}

# 部署层变量 —— 单测必须与「谁在跑、在什么 shell 里跑」无关.
#
# Why 要清: 这些变量不在 _BASELINE 里, 会被外部环境带进来并**改变测试行为**:
#   REVIEW_PROFILE            → 隐式场景包解析用例失效 (显式 profile 存在时无歧义)
#   QODER_SDK_EDITION         → config 校验直接抛 ValueError → 整个文件 collection error
#   QODER_SDK_PAT/MODEL/...   → provider 诊断字段走「已配置」分支, 与「未配置」用例冲突
#   QODERCLI_MODEL/TIMEOUT... → 超时/重试分支参数被外部值改写
#   REDIS_URL / RQ_*          → 队列与 Redis 指向真实实例
#   REVIEWAGENT_WEEKLY_*      → 周报外发门控读到真实部署配置
# 实测: 带 `set -a; . config/instance.env` 跑单测 → 41 failed (其中 30 例是 collection error);
#       清掉这些变量后 → 10 failed (与 HEAD 基线同一批 qodercli.js 环境问题).
_DEPLOY_ENV_KEYS = (
    "REVIEW_PROFILE",
    "QODER_SDK_EDITION",
    "QODER_SDK_PAT",
    "QODER_SDK_MODEL",
    "QODER_SDK_TIMEOUT",
    "QODER_SDK_CLI_PATH",
    "LLM_PROVIDER",
    "GITLAB_BOT_USERNAME",
    "QODERCLI_TIMEOUT",
    "QODERCLI_MAX_TURNS",
    "REDIS_URL",
    "RQ_QUEUE_NAME",
    "RQ_WEEKLY_QUEUE_NAME",
    "REVIEWAGENT_WEEKLY_TARGET_PROJECT_ID",
    "REVIEWAGENT_WEEKLY_CRON_SCHEDULE",
    "REVIEWAGENT_WEEKLY_DINGTALK_WEBHOOK_URL",
    "REVIEWAGENT_WEEKLY_DINGTALK_SECRET",
    "REVIEWAGENT_WEEKLY_DINGTALK_DRY_RUN",
)

for _key, _value in _BASELINE.items():
    os.environ.setdefault(_key, _value)

# 必须在**模块导入期**清掉: `reviewagent.config` 在 import 时就执行
# `Config.from_env()` 绑定全局配置 —— 那时任何 fixture 都还没跑,
# 带着 REVIEW_PROFILE / QODER_SDK_EDITION 导入会直接改变行为
# (config 校验抛 ValueError → 整个测试文件 collection error).
# 记录「导入前存在哪些」+「实际清掉了哪些」, 供 tests/test_conftest_env_isolation.py
# 守门 (只靠 fixture 层的 delenv 守不住这一层 —— fixture 只在运行时生效).
_PRE_IMPORT_DEPLOY_PRESENT = tuple(k for k in _DEPLOY_ENV_KEYS if os.environ.get(k) is not None)
_MODULE_LEVEL_CLEANED: list[str] = []
for _key in _DEPLOY_ENV_KEYS:
    if os.environ.pop(_key, None) is not None:
        _MODULE_LEVEL_CLEANED.append(_key)


import pytest  # noqa: E402  (must come after env baseline)


@pytest.fixture(autouse=True)
def _baseline_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-assert baseline before each test so test order does not matter."""
    for _key, _value in _BASELINE.items():
        monkeypatch.setenv(_key, _value)
    for _key in _DEPLOY_ENV_KEYS:
        monkeypatch.delenv(_key, raising=False)


@pytest.fixture(scope="session", autouse=True)
def _isolate_user_config(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """仓库 `config/` 是部署覆盖层；单测默认隔离，避免随部署调参漂移.

    必须是 session 级：module 级 fixture 早于 function 级 fixture 实例化。
    `tests/test_config_docs.py` 需要校验仓库真实 `config/`，用 monkeypatch 覆盖。
    """
    path = tmp_path_factory.mktemp("no-user-config")
    previous = os.environ.get("REVIEW_CONFIG_DIR")
    os.environ["REVIEW_CONFIG_DIR"] = str(path)
    yield
    if previous is None:
        os.environ.pop("REVIEW_CONFIG_DIR", None)
    else:
        os.environ["REVIEW_CONFIG_DIR"] = previous


@pytest.fixture(autouse=True)
def _restore_config_snapshots() -> Iterator[None]:
    """Snapshot/restore binder's explicit-env and the global current config.

    Both are module-level snapshots refreshed by config parsing (`from_env`)
    and scenario application (`apply_scenario`); monkeypatch removing env
    cannot roll them back, so restoring them keeps test ordering irrelevant.
    """
    # Imported lazily: reviewagent.config parses env at import time, so the
    # baseline above must be in place first.
    from reviewagent import config as config_module
    from reviewagent.settings import binder

    saved_explicit = binder._EXPLICIT_ENV
    saved_current = config_module._current
    yield
    binder._EXPLICIT_ENV = saved_explicit
    config_module._current = saved_current
