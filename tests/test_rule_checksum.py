# tests/test_rule_checksum.py
import hashlib

RULE_BODY_SHA256 = "e3e87ab8d36a432ad0964581070e8f29886ebf2415bbd9db4c585967de40420b"


def test_general_rules_block_checksum():
    from reviewagent.scenario.prompts import load_block

    body = load_block("_general_rules_block")
    assert hashlib.sha256(body.encode("utf-8")).hexdigest() == RULE_BODY_SHA256
