from types import SimpleNamespace

from reviewagent.webhook.locks import MRLockManager


def test_is_bot_matches_either_side_of_display_username(monkeypatch):
    import reviewagent.webhook.locks as locks_module

    monkeypatch.setattr(
        locks_module,
        "config",
        SimpleNamespace(
            gitlab_bot_username="review-agent,review-bot-v2",
            gitlab_disable_bot_loop_check=False,
        ),
    )
    locks = MRLockManager.__new__(MRLockManager)

    assert locks.is_bot("review-bot-v2@review-bot-v2") is True
    assert locks.is_bot("Review Bot@review-bot-v2") is True
    assert locks.is_bot("review-agent") is True
    assert locks.is_bot("Administrator@root") is False
