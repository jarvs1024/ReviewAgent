"""配置生效性快速档：`catalog.BEHAVIOR_CHECKS` 每项都必须有行为 delta 证明.

参数化自 `scripts/e2e/quick_witnesses.QUICK`；新增行为类配置字段而没写 witness
时，`tests/test_config_catalog.py` 会先失败，这里保证 witness 真实生效。
"""
from __future__ import annotations

import pytest

from reviewagent.config import current
from scripts.e2e import quick_witnesses


@pytest.mark.parametrize("check", sorted(quick_witnesses.QUICK))
def test_witness_changes_behavior(check: str) -> None:
    evidence = quick_witnesses.QUICK[check](current())
    assert evidence, f"{check} 未返回证据"


def test_run_quick_covers_every_check() -> None:
    results = quick_witnesses.run_quick()
    assert {name for name, _ in results} == set(quick_witnesses.QUICK)
    assert all(evidence for _, evidence in results)
