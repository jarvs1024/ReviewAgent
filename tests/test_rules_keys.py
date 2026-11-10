"""scenario.rules.keys — 内置键/仓库规则键抽取形状（与旧正则一致）."""
from __future__ import annotations

import pytest

from reviewagent.scenario.rules.keys import (
    AUTO_REPO_KEY_SOURCE,
    DEFAULT_BUILTIN_PATTERN,
    build_key_patterns,
    normalize_repo_prefix,
    repo_key_source,
)

KEYS = {
    "builtin_pattern": r"(?:R-OTHER-IMPACT:[a-z0-9_]+|R-[A-Z]+(?:-[A-Z0-9_]+)*)",
    "free_prefixes": ["R-OTHER-IMPACT"],
    "repo_prefix": "SSD",
    "display_prefix": "SSD",
}


def test_extract_matches_legacy_shapes():
    kp = build_key_patterns(KEYS)
    assert kp.extract("命中 R-LOG 与 SSD-RULE-NO-LOG-EXC，另有 R-OTHER-IMPACT:caller_param") == [
        "R-LOG",
        "SSD-RULE-NO-LOG-EXC",
        "R-OTHER-IMPACT:caller_param",
    ]
    assert kp.is_repo("SSD-RULE-X") is True
    assert kp.is_repo("R-LOG") is False


def test_is_builtin_and_duplicate_hits_are_collapsed():
    kp = build_key_patterns(KEYS)
    assert kp.is_builtin("R-LOG") is True
    assert kp.is_builtin("SSD-RULE-X") is False
    assert kp.extract("R-LOG again R-LOG, then R-ERR") == ["R-LOG", "R-ERR"]


def test_repo_key_is_not_extracted_as_builtin_fragment():
    kp = build_key_patterns(KEYS)
    assert kp.extract("SSD-RULE-NO-LOG-EXC") == ["SSD-RULE-NO-LOG-EXC"]


def test_missing_rule_keys_fall_back_to_legacy_pattern():
    kp = build_key_patterns({})
    assert kp.builtin_raw.pattern == DEFAULT_BUILTIN_PATTERN
    assert kp.free_prefixes == ()
    assert kp.is_repo("SSD-RULE-X") is True


@pytest.mark.parametrize("prefix", ["SSD", "ssd", "SSD-RULE", "ssd-rule", " SSD "])
def test_repo_prefix_normalizes_to_single_shape(prefix: str):
    """`SSD` / `ssd-rule` 等写法归一化到同一个前缀，不再产生 `SSD-RULE-RULE-*`."""
    assert normalize_repo_prefix(prefix) == "SSD"
    assert repo_key_source(prefix) == r"SSD-RULE-[A-Z0-9-]+"
    kp = build_key_patterns({"repo_prefix": prefix})
    assert kp.is_repo("SSD-RULE-NO-BARE-PRINT") is True
    assert kp.is_repo("ZZZ-RULE-OTHER") is False


@pytest.mark.parametrize("prefix", ["", "auto", "AUTO", "*", None])
def test_auto_prefix_source(prefix: str | None):
    assert normalize_repo_prefix(prefix) == ""
    assert repo_key_source(prefix) == AUTO_REPO_KEY_SOURCE
