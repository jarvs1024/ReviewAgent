# tests/test_rule_checksum.py
import hashlib

RULE_BODY_SHA256 = "963ce3843726312aae2647371f98518e716add4bf96f349b58ad62b2cb97ba96"


def test_general_rules_block_checksum():
    from reviewagent.scenario.prompts import load_block

    body = load_block("_general_rules_block")
    assert hashlib.sha256(body.encode("utf-8")).hexdigest() == RULE_BODY_SHA256
