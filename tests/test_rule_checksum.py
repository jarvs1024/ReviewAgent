# tests/test_rule_checksum.py
import hashlib

RULE_BODY_SHA256 = "84ed4563f513be9589e8ab4aa97c49d2699bc226348377455dd65a8b2fa9712c"


def test_general_rules_block_checksum():
    from reviewagent.prompts.loader import load_block

    body = load_block("_general_rules_block")
    assert hashlib.sha256(body.encode("utf-8")).hexdigest() == RULE_BODY_SHA256
