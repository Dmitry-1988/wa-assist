"""`propose` must not arm a draft it has not seen land.

It used to post and then read ONCE, immediately. Two things go wrong there,
and on 2026-09-06 both did, seven ticks in a row:

* `selfchat.post` returns a SendResult and does not raise when the send is
  refused after the click. `propose` discarded it, so "never posted" was
  reported as the much more confusing "posted #XXX but could not locate its
  message id".
* A genuine "sent" is not transmitted yet when the call returns. The immediate
  read found nothing, `propose` raised, and the caller closed the browser --
  leaving a draft in the self-chat that no pending entry armed. `OK #XXX` on
  one of those reaches no code path at all and looks like a dead daemon.

`post_note` had already learned this for digests. Its docstring even claimed
drafts were immune because propose "reads its message back" -- true of the
reading, but not of the waiting.
"""

import pytest

from wa_session.agent import _post_and_locate


class _Result:
    def __init__(self, ok=True, dry_run=False, detail=""):
        self.ok, self.dry_run, self.detail = ok, dry_run, detail


class _Msg:
    def __init__(self, text, msg_id):
        self.text, self.msg_id = text, msg_id


class FakePage:
    """A self-chat where a posted message appears after `delay` reads."""

    def __init__(self, delay=0, result=None, never=False):
        self.delay, self.result, self.never = delay, result, never
        self.reads = 0
        self.waits = 0
        self.posted = []

    def wait_for_timeout(self, ms):
        self.waits += 1


@pytest.fixture
def wire(monkeypatch):
    def _wire(page):
        def fake_post(p, text, dry_run=False):
            p.posted.append(text)
            return p.result

        def fake_read(p, limit=60, refresh=False, scroll=True):
            p.reads += 1
            if p.never or p.reads <= p.delay:
                return []
            return [_Msg(t, f"id{i}") for i, t in enumerate(p.posted)]

        monkeypatch.setattr("wa_session.agent.post", fake_post)
        monkeypatch.setattr("wa_session.agent.read", fake_read)
    return _wire


def test_a_message_that_lands_immediately_is_found(wire):
    page = FakePage()
    wire(page)
    assert _post_and_locate(page, "DRAFT #ABC body", "#ABC") == "id0"


def test_a_message_that_lands_late_is_still_found(wire):
    """The whole point: transmission is not finished when post() returns."""
    page = FakePage(delay=4)
    wire(page)
    assert _post_and_locate(page, "DRAFT #ABC body", "#ABC") == "id0"
    assert page.waits >= 4, "it must actually have waited, not just re-read"


def test_a_message_that_never_appears_gives_up_and_reports_nothing(wire):
    page = FakePage(never=True)
    wire(page)
    assert _post_and_locate(page, "DRAFT #ABC", "#ABC", settle_s=0.05) == ""


def test_a_refused_send_raises_rather_than_blaming_the_read_back(wire):
    """The old message accused the read-back of losing a message that was
    never sent. The SendResult says plainly that it was refused."""
    page = FakePage(result=_Result(ok=False, detail="composer refused"))
    wire(page)
    with pytest.raises(RuntimeError, match="was not posted: composer refused"):
        _post_and_locate(page, "DRAFT #ABC", "#ABC")


def test_a_dry_run_is_never_mistaken_for_a_posted_draft(wire):
    page = FakePage(result=_Result(ok=True, dry_run=True))
    wire(page)
    with pytest.raises(RuntimeError, match="dry run"):
        _post_and_locate(page, "DRAFT #ABC", "#ABC")


def test_a_read_that_throws_does_not_end_the_wait(wire, monkeypatch):
    """A transient DOM error mid-transmission must not be read as failure."""
    page = FakePage()
    wire(page)
    real_read = __import__("wa_session.agent", fromlist=["read"]).read
    calls = {"n": 0}

    def flaky(p, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("detached node")
        return real_read(p, **kw)

    monkeypatch.setattr("wa_session.agent.read", flaky)
    assert _post_and_locate(page, "DRAFT #ABC", "#ABC") == "id0"


def test_the_settle_read_does_not_pay_for_scrolling(wire, monkeypatch):
    """A scrolling read costs ~6s of a 12s budget, and only the newest
    messages can possibly hold a draft just posted."""
    page = FakePage()
    seen = {}

    def fake_read(p, limit=60, refresh=False, scroll=True):
        seen.update(limit=limit, refresh=refresh, scroll=scroll)
        return [_Msg("DRAFT #ABC", "id0")]

    monkeypatch.setattr("wa_session.agent.post", lambda p, text, dry_run=False: None)
    monkeypatch.setattr("wa_session.agent.read", fake_read)
    _post_and_locate(page, "DRAFT #ABC", "#ABC")
    assert seen["scroll"] is False and seen["refresh"] is True
    assert seen["limit"] == 8
