"""Rendering is not sending.

A message appears in the composer's own chat the instant it is typed, and sits
at "Pending" until WhatsApp's servers take it. `post_note` read the text back
and called that delivery -- but it was reading the same browser that had just
drawn it, so it proved nothing about transmission. The daemon then closes the
browser, and a message still Pending at that moment is simply gone: no error,
`summary_posted` in the log, nothing on the user's phone.

Two digests eleven minutes apart on 2026-09-07: 14:22 arrived, 14:33 did not.
Identical markup, identical code path, and the only difference was which side
of the race each landed on. Confirmed by posting a note and holding the
browser open for 30s -- that one arrived, and unlike every digest before it,
carried a status.

Measured on the live self-chat the same evening:

    1.9s  Pending
    2.4s  Read

So there is a real acknowledgement to wait for, and this waits for it.
"""

import pytest

from wa_session.selfchat import delivery_state, wait_for_delivery


class FakePage:
    """A page whose message status advances after `flips` polls."""

    def __init__(self, states):
        self.states = list(states)
        self.polls = 0
        self.waits = 0

    def evaluate(self, script, arg=None):
        state = self.states[min(self.polls, len(self.states) - 1)]
        self.polls += 1
        return state

    def wait_for_timeout(self, ms):
        self.waits += 1


# --- reading the status ---------------------------------------------------

def test_no_message_id_is_absent():
    assert delivery_state(FakePage(["Read"]), "") == "absent"


def test_a_page_that_cannot_be_queried_reports_none():
    """A torn-down context must not look like an acknowledgement."""
    class Broken:
        def evaluate(self, script, arg=None):
            raise RuntimeError("execution context destroyed")

    assert delivery_state(Broken(), "id1") == "none"


# --- waiting for it -------------------------------------------------------

@pytest.mark.parametrize("state", ["Read", "Delivered", "Sent", "read"])
def test_an_acknowledged_message_returns_at_once(state):
    page = FakePage([state])
    assert wait_for_delivery(page, "id1", timeout_s=5) == state
    assert page.waits == 0, "no reason to wait for something already sent"


def test_it_waits_through_pending():
    """The real sequence: Pending for a beat, then Read."""
    page = FakePage(["Pending", "Pending", "Pending", "Read"])
    assert wait_for_delivery(page, "id1", timeout_s=5) == "Read"
    assert page.waits >= 1


def test_a_message_stuck_pending_is_reported_as_pending():
    page = FakePage(["Pending"])
    assert wait_for_delivery(page, "id1", timeout_s=0.05) == "Pending"


def test_a_message_that_vanishes_is_reported_absent():
    page = FakePage(["absent"])
    assert wait_for_delivery(page, "id1", timeout_s=0.05) == "absent"


# --- and the callers refuse to call it delivered --------------------------

def test_post_note_refuses_an_unacknowledged_note(monkeypatch):
    import wa_session.selfchat as selfchat
    from wa_session.tick import post_note

    class _Msg:
        text, msg_id = "hello world", "id1"

    monkeypatch.setattr(selfchat, "post", lambda page, text, dry_run=False: None)
    monkeypatch.setattr(selfchat, "read", lambda page, **kw: [_Msg()])
    page = FakePage(["Pending"])
    with pytest.raises(RuntimeError, match="never acknowledged"):
        post_note(page, "hello world", settle_s=0.05, ack_s=0.05)


def test_post_note_accepts_an_acknowledged_note(monkeypatch):
    import wa_session.selfchat as selfchat
    from wa_session.tick import post_note

    class _Msg:
        text, msg_id = "hello world", "id1"

    monkeypatch.setattr(selfchat, "post", lambda page, text, dry_run=False: None)
    monkeypatch.setattr(selfchat, "read", lambda page, **kw: [_Msg()])
    assert post_note(FakePage(["Read"]), "hello world") == "id1"


def test_a_draft_is_not_armed_without_an_acknowledgement(monkeypatch):
    """Worse than a lost digest: the user is told a draft exists, types OK
    against a message that never arrived, and nothing happens."""
    from wa_session.agent import _post_and_locate

    class _Msg:
        text, msg_id = "DRAFT #ABC body", "id1"

    monkeypatch.setattr("wa_session.agent.post",
                        lambda page, text, dry_run=False: None)
    monkeypatch.setattr("wa_session.agent.read", lambda page, **kw: [_Msg()])
    monkeypatch.setattr("wa_session.agent.find_message_id",
                        lambda msgs, frag: "id1")
    with pytest.raises(RuntimeError, match="never.*acknowledged"):
        _post_and_locate(FakePage(["Pending"]), "DRAFT #ABC body", "#ABC",
                         settle_s=0.05, ack_s=0.05)
