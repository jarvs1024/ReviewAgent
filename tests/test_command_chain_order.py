"""验证 pr_commands / push_commands 顺序统一为 describe → improve.

Why: 历史 UI 上曾出现 "improve → describe → improve" 错序 (chain 1 = push→improve,
chain 2 = MR open→describe→improve, chain 3 = push→improve). 统一两个 tuple
避免 push / open 触发 chain 顺序不一致.
"""
from reviewagent.config import config


def test_pr_commands_has_describe_before_improve():
    assert "describe" in config.pr_commands
    assert "improve" in config.pr_commands
    assert config.pr_commands.index("describe") < config.pr_commands.index("improve")


def test_push_commands_has_describe_before_improve():
    assert "describe" in config.push_commands
    assert "improve" in config.push_commands
    assert config.push_commands.index("describe") < config.push_commands.index("improve")


def test_pr_and_push_commands_aligned():
    """两个 tuple 顺序必须一致, 避免 chain 1 和 chain 2 出现错序."""
    assert config.pr_commands == config.push_commands
