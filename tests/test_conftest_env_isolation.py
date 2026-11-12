"""D15 守卫: 单元测试必须与「谁在跑、在什么 shell 里跑」无关.

真实缺陷 (2026-11-12 第 1 轮复验时抓到):
    带 `set -a; . config/instance.env` 跑 `pytest -q` → **41 failed**
    (干净环境只有 10 failed) —— 多出的 31 例全是**环境变量污染**:
      * REVIEW_PROFILE      → 隐式场景包解析用例失效
      * QODER_SDK_EDITION   → `Config.from_env()` 在 import 期抛 ValueError
                              → 整个 tests/test_qoder_sdk_provider_errors.py collection error (61 例)
    这意味着「单元测试通过数」取决于开发者的 shell 状态 —— 门禁不可信。

本文件守两件事:
    1. conftest 的清理清单 `_DEPLOY_ENV_KEYS` 覆盖了 instance.env 里所有
       「会改变单测行为」的部署变量（新增部署变量时会被这条测试拦下）;
    2. 进程里这些变量确实已被清掉。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.conftest import (
    _BASELINE,
    _DEPLOY_ENV_KEYS,
    _MODULE_LEVEL_CLEANED,
    _PRE_IMPORT_DEPLOY_PRESENT,
)

REPO = Path(__file__).resolve().parents[1]
INSTANCE_ENV = REPO / "config" / "instance.env"

# 允许留在环境里的键: baseline 本身, 或与单测无关的键
_ALLOWED_TO_REMAIN = set(_BASELINE) | {
    "REVIEW_CONFIG_DIR",      # session 级隔离 fixture 会自己设
}


def _instance_env_keys() -> set[str]:
    if not INSTANCE_ENV.exists():
        return set()
    keys = set()
    for line in INSTANCE_ENV.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        keys.add(line.split("=", 1)[0].strip())
    return keys


@pytest.mark.skipif(not INSTANCE_ENV.exists(), reason="本地部署文件不存在（CI 跳过）")
def test_deploy_env_keys_cover_instance_env():
    """instance.env 里新增的部署变量必须同时进清理清单，否则单测结果会随 shell 变。

    Why: 这条测试是「以后新增部署变量」的提醒器 —— 忘了加进清理清单，
         就会重演 D15（带着真环境跑 → 一片假失败 / collection error）。
    """
    missing = sorted(_instance_env_keys() - set(_DEPLOY_ENV_KEYS) - _ALLOWED_TO_REMAIN)
    assert not missing, (
        f"这些部署变量不在 conftest 的 _DEPLOY_ENV_KEYS 里, 会污染单测: {missing}\n"
        f"请加入 tests/conftest.py::_DEPLOY_ENV_KEYS"
    )


def test_deploy_env_keys_are_actually_cleaned():
    """清单里的变量在测试进程里确实不存在（import 期已清）。"""
    leaked = sorted(k for k in _DEPLOY_ENV_KEYS if os.environ.get(k) is not None)
    assert not leaked, f"这些部署变量没被清掉: {leaked}"


def test_baseline_env_still_present():
    """反向断言: baseline 必须还在（清部署变量不能把必需变量一起清了）。"""
    missing = sorted(k for k in _BASELINE if not os.environ.get(k))
    assert not missing, f"baseline 变量缺失: {missing}"


def test_module_level_cleanup_actually_ran():
    """守住 **import 期**那一层清理（fixture 层守不住它）。

    Why: `reviewagent.config` 在 import 期绑定配置 —— 那时 fixture 还没跑。
         若有人删掉 conftest 的模块级 pop, 带 `instance.env` 跑时:
           - REVIEW_PROFILE  → 隐式场景包用例失效;
           - QODER_SDK_EDITION → config 校验 ValueError → 整个文件 collection error;
         而 fixture 层的 delenv 仍然在跑, 只看运行期环境是抓不到的。
    """
    if not _PRE_IMPORT_DEPLOY_PRESENT:
        pytest.skip("导入前环境本来就没有部署变量（干净环境，无需守这一层）")
    not_cleaned = sorted(set(_PRE_IMPORT_DEPLOY_PRESENT) - set(_MODULE_LEVEL_CLEANED))
    assert not not_cleaned, (
        f"这些部署变量在 import 期没被清掉: {not_cleaned}\n"
        f"conftest 的模块级 os.environ.pop 循环被删了?"
    )
