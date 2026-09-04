"""Reading the self-chat: the one place every instruction arrives.

WhatsApp virtualises the message list. Reopening the self-chat rendered ONE row
out of nineteen, and `read` did not scroll -- so a GROUPSUM or an approval
outside that keyhole was silently never seen. `read_chat` had always scrolled;
the self-chat had not. Found 2026-09-04 after the daemon appeared to ignore a
request that was plainly there.

No autouse stub of `selfchat.read` here: these test the real thing.
"""

import pytest

import wa_session.selfchat as selfchat


class _M:
    def __init__(self, i=0):
        self.text, self.msg_id = f"m{i}", f"id{i}"


class _Page:
    pass


@pytest.fixture
def scrolls(monkeypatch):
    calls = []
    monkeypatch.setattr(selfchat, "open_self_chat", lambda page: "self")
    monkeypatch.setattr(selfchat, "load_more",
                        lambda page, minimum: calls.append(minimum))
    monkeypatch.setattr(selfchat, "extract_messages",
                        lambda page: [_M(i) for i in range(19)])
    return calls


def test_read_scrolls_to_the_depth_it_promises(scrolls):
    got = selfchat.read(_Page(), limit=25)
    assert scrolls == [25]
    assert len(got) == 19


def test_a_second_read_in_the_same_tick_does_not_rescroll(scrolls):
    """Scrolling costs seconds and several callers read per cycle."""
    page = _Page()
    selfchat.read(page, limit=25)
    selfchat.read(page, limit=12)      # shallower: the cache covers it
    assert scrolls == [25]


def test_a_deeper_read_is_not_served_from_a_shallower_cache(scrolls):
    page = _Page()
    selfchat.read(page, limit=12)
    selfchat.read(page, limit=40)
    assert scrolls == [12, 40]


def test_refresh_forces_a_fresh_read(scrolls):
    page = _Page()
    selfchat.read(page, limit=25)
    selfchat.read(page, limit=25, refresh=True)
    assert scrolls == [25, 25]


def test_scroll_false_skips_the_expensive_part(scrolls):
    """The delivery read-back only looks for the newest message, which is
    always rendered."""
    selfchat.read(_Page(), limit=8, scroll=False)
    assert scrolls == []


def test_a_non_scrolling_read_is_not_cached(scrolls):
    """It saw only the rendered tail; a later caller wanting depth must scroll."""
    page = _Page()
    selfchat.read(page, limit=8, scroll=False)
    selfchat.read(page, limit=25)
    assert scrolls == [25]


def test_posting_invalidates_the_cached_history(scrolls, monkeypatch):
    """Otherwise post_note reads a history predating its own note and concludes
    the note was never delivered."""
    monkeypatch.setattr(selfchat, "send_message",
                        lambda page, name, text, dry_run=False, **kw: None)
    page = _Page()
    selfchat.read(page, limit=25)
    selfchat.post(page, "a note")
    selfchat.read(page, limit=25)
    assert scrolls == [25, 25]


def test_two_pages_do_not_share_a_cache(scrolls):
    """Each tick opens its own browser; one tick's history is not another's."""
    selfchat.read(_Page(), limit=25)
    selfchat.read(_Page(), limit=25)
    assert scrolls == [25, 25]
