# tests/test_rule_checksum.py
import hashlib

RULE_BODY_SHA256 = "e77f5eaf9ae380d11f3d08c1d0e513ffcf8554829b3616b3768c1b3d9a8c12e5"


def test_general_rules_block_checksum():
    from reviewagent.scenario.prompts import load_block

    body = load_block("_general_rules_block")
    assert hashlib.sha256(body.encode("utf-8")).hexdigest() == RULE_BODY_SHA256
