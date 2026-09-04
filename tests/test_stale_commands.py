"""A command must never vanish without a word.

A sent, withdrawn or expired draft is filtered out of `pending_drafts`, so
`OK #XXX` on one reached no code path at all -- no send, no note, nothing in
the log. From the user's side that is exactly what a broken daemon looks like,
which is the failure that cost most of 2026-09-02.
"""

import datetime

import pytest

from wa_session.agent import PendingDraft, journal_path, save_pending
from wa_session.approval import Decision, Draft, Journal
from wa_session.config import Config
from wa_session.tick import _answer_stale_commands


@pytest.fixture
def config(tmp_path) -> Config:
    (tmp_path / "p").mkdir()
    return Config(profile_dir=tmp_path / "p" / ".wa-profile",
                  state_dir=tmp_path / "p" / ".wa-state", rotate_after_hours=24.0)


class _Msg:
    def __init__(self, text, msg_id):
        self.text, self.msg_id = text, msg_id


class Page:
    def __init__(self, *texts):
        self.incoming = [_Msg(t, f"m{i}") for i, t in enumerate(texts)]
        self.posted = []

    def wait_for_timeout(self, ms):
        pass


@pytest.fixture(autouse=True)
def fake_selfchat(monkeypatch):
    import wa_session.selfchat as selfchat

    def post(page, text, dry_run=False):
        page.posted.append(text)
        page.incoming.append(_Msg(text, f"p{len(page.posted)}"))
        return None

    monkeypatch.setattr(selfchat, "post", post)
    monkeypatch.setattr(selfchat, "read", lambda page, limit=60, **kw: page.incoming)


def stored(config, draft_id, hours_old=0.0):
    created = (datetime.datetime.now(datetime.timezone.utc)
               - datetime.timedelta(hours=hours_old))
    d = Draft(draft_id=draft_id, recipient="X", source_chat="X", body="b",
              quoted="", sources=[], created_at=created, ttl_hours=2)
    save_pending(config, PendingDraft(draft=d, marker_id="mk"))


def test_approving_a_withdrawn_draft_says_so(config):
    stored(config, "#AAA")
    Journal(journal_path(config)).retire("#AAA", "superseded by EDIT")
    page = Page("OK #AAA")
    result = {"actions": []}
    _answer_stale_commands(page, config, result)
    assert len(page.posted) == 1
    assert "#AAA" in page.posted[0] and "withdrawn" in page.posted[0]
    assert "Nothing was sent" in page.posted[0]


def test_approving_an_already_sent_draft_says_so(config):
    stored(config, "#BBB")
    Journal(journal_path(config)).record_send_attempt("#BBB", "X")
    page = Page("OK #BBB")
    _answer_stale_commands(page, config, {"actions": []})
    assert "already sent" in page.posted[0]


def test_approving_an_expired_draft_says_so(config):
    stored(config, "#CCC", hours_old=5)      # ttl is 2h
    page = Page("OK #CCC")
    _answer_stale_commands(page, config, {"actions": []})
    assert "expired" in page.posted[0]


def test_a_cyrillic_ok_on_a_stale_draft_is_understood(config):
    """Same look-alike problem as a live approval."""
    stored(config, "#DDD")
    Journal(journal_path(config)).retire("#DDD", "rejected")
    page = Page("ОК #DDD")
    _answer_stale_commands(page, config, {"actions": []})
    assert page.posted, "Cyrillic ОК went unanswered"


def test_a_live_draft_is_left_to_the_normal_path(config):
    """It must not answer for a draft that is still awaiting a decision."""
    stored(config, "#EEE")
    page = Page("OK #EEE")
    _answer_stale_commands(page, config, {"actions": []})
    assert page.posted == []


def test_an_unknown_id_is_ignored(config):
    """Never ours; staying quiet beats guessing."""
    page = Page("OK #ZZZ")
    _answer_stale_commands(page, config, {"actions": []})
    assert page.posted == []


def test_the_draft_message_itself_is_not_mistaken_for_a_command(config):
    """A posted draft ends with 'OK #AAA | EDIT ... | NO #AAA'."""
    stored(config, "#AAA")
    Journal(journal_path(config)).retire("#AAA", "superseded")
    page = Page("DRAFT #AAA → Ann\\nbody\\n\\nOK #AAA | EDIT #AAA <changes> | NO #AAA")
    _answer_stale_commands(page, config, {"actions": []})
    assert page.posted == []


def test_it_answers_once_not_every_tick(config):
    stored(config, "#AAA")
    Journal(journal_path(config)).retire("#AAA", "superseded")
    page = Page("OK #AAA")
    for _ in range(4):
        _answer_stale_commands(page, config, {"actions": []})
    assert len(page.posted) == 1


def test_the_reason_is_recorded_for_the_log(config):
    stored(config, "#AAA")
    Journal(journal_path(config)).retire("#AAA", "superseded")
    result = {"actions": []}
    _answer_stale_commands(Page("OK #AAA"), config, result)
    assert any(a.get("stale_command") == "#AAA" for a in result["actions"])


# --- the edit acknowledgement ----------------------------------------------

def test_an_accepted_edit_is_acknowledged_immediately(config, monkeypatch):
    """A redraft takes a tick plus an LLM run; without a word that is minutes
    of silence indistinguishable from a dead daemon."""
    from wa_session.approval import Command
    import wa_session.tick as tick

    monkeypatch.setattr(tick, "poll",
                        lambda page, config, draft_id: Command(
                            Decision.EDIT, draft_id, instructions="убери обещание"))
    page = Page()
    result = {"actions": []}
    tick._handle_pending(page, config,
                         {"draft_id": "#AAA", "recipient": "X", "body": "b",
                          "revision": 0}, result)
    assert len(page.posted) == 1
    note = page.posted[0]
    assert "Redrafting #AAA" in note
    assert "revision 1" in note
    assert "убери обещание" in note
    assert "can no longer be approved" in note


def test_hitting_the_revision_cap_is_explained(config, monkeypatch):
    from wa_session.approval import Command
    from wa_session.pipeline import MAX_REVISIONS
    import wa_session.tick as tick

    monkeypatch.setattr(tick, "poll",
                        lambda page, config, draft_id: Command(
                            Decision.EDIT, draft_id, instructions="again"))
    page = Page()
    tick._handle_pending(page, config,
                         {"draft_id": "#AAA", "recipient": "X", "body": "b",
                          "revision": MAX_REVISIONS}, {"actions": []})
    assert "cap" in page.posted[0]
    assert "withdrawn either way" in page.posted[0]
