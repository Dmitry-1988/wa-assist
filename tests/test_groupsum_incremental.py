"""GROUPSUM collection must skip groups with nothing new, and say so."""

import pytest

from wa_session.config import Config
from wa_session.pipeline import read_queue
from wa_session.tick import _collect_group_messages
from wa_session.watermarks import advance, read_watermarks


@pytest.fixture
def config(tmp_path) -> Config:
    (tmp_path / "p").mkdir()
    cfg = Config(profile_dir=tmp_path / "p" / ".wa-profile",
                 state_dir=tmp_path / "p" / ".wa-state", rotate_after_hours=24.0)
    agent = tmp_path / "p" / ".wa-agent"
    agent.mkdir(parents=True, exist_ok=True)
    (agent / "allowlist.json").write_text(
        '[{"name": "G1", "is_group": true, "mode": "summarize"},'
        ' {"name": "G2", "is_group": true, "mode": "summarize"}]',
        encoding="utf-8")
    return cfg


class FakePage:
    def __init__(self):
        self.posted: list[str] = []


CHATS = {
    "G1": [{"msg_id": "a1", "text": "one"}, {"msg_id": "a2", "text": "two"}],
    "G2": [{"msg_id": "b1", "text": "three"}],
}


class FakeUnread:
    def __init__(self, name, count):
        self.name, self.unread_count = name, count


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    monkeypatch.setattr("wa_session.tick.read_chat",
                        lambda page, name: {"ok": True, "messages": CHATS[name]})
    monkeypatch.setattr("wa_session.tick.list_unread", lambda page: [])
    import wa_session.selfchat as selfchat
    monkeypatch.setattr(selfchat, "post",
                        lambda page, text, dry_run=False: page.posted.append(text))


def test_a_first_digest_takes_everything(config):
    result = {"actions": []}
    assert _collect_group_messages(FakePage(), config, result) is not None
    queued = read_queue(config)[0]
    assert {b["chat"]: len(b["messages"]) for b in queued.messages} == {"G1": 2, "G2": 1}


def test_a_group_with_nothing_new_is_left_out(config):
    advance(config, [{"chat": "G1", "messages": CHATS["G1"]}])
    result = {"actions": []}
    _collect_group_messages(FakePage(), config, result)
    queued = read_queue(config)[0]
    assert [b["chat"] for b in queued.messages] == ["G2"]
    assert {"groupsum_unchanged": ["G1"]} in result["actions"]


def test_nothing_new_anywhere_queues_no_paid_run(config):
    advance(config, [{"chat": "G1", "messages": CHATS["G1"]},
                     {"chat": "G2", "messages": CHATS["G2"]}])
    result = {"actions": []}
    assert _collect_group_messages(FakePage(), config, result) is None
    assert read_queue(config) == []


def test_nothing_new_still_answers_the_user(config):
    """Silence after a GROUPSUM is indistinguishable from a failure."""
    advance(config, [{"chat": "G1", "messages": CHATS["G1"]},
                     {"chat": "G2", "messages": CHATS["G2"]}])
    page = FakePage()
    _collect_group_messages(page, config, {"actions": []})
    assert len(page.posted) == 1
    assert "Nothing new" in page.posted[0]


def test_marks_are_not_advanced_at_capture_time(config):
    """They advance only when a digest actually posts -- five posts failed
    silently on 2026-09-01, and advancing here would have lost those messages."""
    _collect_group_messages(FakePage(), config, {"actions": []})
    assert read_watermarks(config) == {}


def test_the_quiet_note_lists_which_groups_it_actually_checked(config):
    """'Monitored' is doing a lot of work in that sentence."""
    advance(config, [{"chat": "G1", "messages": CHATS["G1"]},
                     {"chat": "G2", "messages": CHATS["G2"]}])
    page = FakePage()
    _collect_group_messages(page, config, {"actions": []})
    note = page.posted[0]
    assert "G1" in note and "G2" in note


def test_an_unread_chat_outside_the_allowlist_is_named(config, monkeypatch):
    """A newly joined group sitting on 9 unread read as a broken digest,
    because nothing said it was simply not on the list."""
    monkeypatch.setattr("wa_session.tick.list_unread",
                        lambda page: [FakeUnread("קבוצת הורים 2026", 9)])
    advance(config, [{"chat": "G1", "messages": CHATS["G1"]},
                     {"chat": "G2", "messages": CHATS["G2"]}])
    page = FakePage()
    result = {"actions": []}
    _collect_group_messages(page, config, result)
    note = page.posted[0]
    assert "NOT monitored" in note
    assert "קבוצת הורים 2026 (9)" in note
    assert "wa-agent allow" in note
    assert {"groupsum": "nothing new",
            "unmonitored_unread": ["קבוצת הורים 2026"]} in result["actions"]


def test_an_allowlisted_chat_is_not_reported_as_unmonitored(config, monkeypatch):
    monkeypatch.setattr("wa_session.tick.list_unread",
                        lambda page: [FakeUnread("G1", 3)])
    advance(config, [{"chat": "G1", "messages": CHATS["G1"]},
                     {"chat": "G2", "messages": CHATS["G2"]}])
    page = FakePage()
    _collect_group_messages(page, config, {"actions": []})
    assert "NOT monitored" not in page.posted[0]


def test_listing_unread_never_opens_a_chat(config, monkeypatch):
    """It reads the chat list only -- naming a chat must cost no read receipt."""
    monkeypatch.setattr("wa_session.tick.read_chat",
                        lambda page, name: pytest.fail("must not open a chat"))
    advance(config, [{"chat": "G1", "messages": CHATS["G1"]},
                     {"chat": "G2", "messages": CHATS["G2"]}])
    from wa_session.tick import _unmonitored_unread
    from wa_session.allowlist import Allowlist
    from wa_session.agent import allowlist_path
    monkeypatch.setattr("wa_session.tick.list_unread",
                        lambda page: [FakeUnread("stranger", 2)])
    assert _unmonitored_unread(FakePage(), Allowlist(allowlist_path(config))) == [
        ("stranger", 2)]
