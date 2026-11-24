"""A reply to a contact counts as sent only once WhatsApp acknowledges it.

Self-chat posts have waited for the acknowledgement since 2026-09-07. Replies
did not: an empty composer was reported as "sent", and the browser closed on
a reply that could still be "Pending" -- lost, with "sent" in the journal.
"""

import pytest

from wa_session import agent


class Msg:
    def __init__(self, text, msg_id):
        self.text, self.msg_id, self.sender = text, msg_id, "me"


class Page:
    def __init__(self, status="Read"):
        self.status = status

    def wait_for_timeout(self, ms):
        pass

    def evaluate(self, script, arg=None):
        return self.status


@pytest.fixture
def chat(monkeypatch):
    def set_messages(messages):
        monkeypatch.setattr("wa_session.messages.extract_messages",
                            lambda page: list(messages))
    return set_messages


def test_an_acknowledged_reply_is_confirmed(chat):
    chat([Msg("старое", "a"), Msg("Да, в среду свободен", "b")])
    assert agent._confirm_sent(Page("Delivered"), "Да, в среду свободен",
                               already={"a"}) == ""


def test_a_reply_stuck_pending_is_not_confirmed(chat):
    chat([Msg("Да, в среду свободен", "b")])
    why = agent._confirm_sent(Page("Pending"), "Да, в среду свободен",
                              already=set(), settle_s=0.01, ack_s=0.01)
    assert "never acknowledged" in why and "will not be retried" in why


def test_a_reply_that_never_appears_is_not_confirmed(chat):
    chat([Msg("старое", "a")])
    why = agent._confirm_sent(Page("Read"), "Да, в среду свободен",
                              already={"a"}, settle_s=0.01)
    assert "never appeared" in why


def test_an_identical_earlier_reply_does_not_count(chat):
    """The same words sent yesterday are not today's reply."""
    chat([Msg("ок", "yesterday")])
    why = agent._confirm_sent(Page("Read"), "ок", already={"yesterday"},
                              settle_s=0.01)
    assert "never appeared" in why


def test_an_unconfirmed_send_is_announced_as_uncertain(tmp_path, monkeypatch):
    """Past the click nothing is certain, so the note must not claim
    "Nothing was delivered" -- and must not invite a resend either way."""
    from wa_session import tick
    from wa_session.approval import Command, Decision
    from wa_session.config import Config

    config = Config(profile_dir=tmp_path / "p", state_dir=tmp_path / "s",
                    rotate_after_hours=24.0)
    notes = []
    monkeypatch.setattr(tick, "poll", lambda page, config, draft_id:
                        Command(Decision.APPROVE, draft_id))
    monkeypatch.setattr(tick, "deliver", lambda *a, **k: {
        "ok": False, "clicked": True, "reason": "WhatsApp never acknowledged it"})
    monkeypatch.setattr(tick, "post_note",
                        lambda page, text, **kw: notes.append(text) or "n1")
    tick._handle_pending(object(), config, {"draft_id": "#AAA"}, {"actions": []})
    assert "may NOT have arrived" in notes[0]
    assert "Nothing was delivered" not in notes[0]
