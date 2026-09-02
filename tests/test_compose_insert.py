"""Text must reach the composer unchanged, in ONE insertion.

WhatsApp's composer auto-continues lists. Typing a body line-by-line with
Shift+Enter between made the editor add its own list markers on top of the
ones already in the text: "2." arrived as "2. 2.", and once a list was running
every later line was numbered too ("• bullet" -> "4. • bullet"). A 3000
character group digest failed its pre-send check on this five ticks running.
Verified against the live site 2026-09-01.
"""

import pytest

from wa_session.compose import _settle_ms, describe_mismatch, type_text


class FakeBox:
    def __init__(self):
        self.presses: list[str] = []
        self.clicked = False

    def click(self, timeout=None):
        self.clicked = True

    def press(self, key):
        self.presses.append(key)


class FakeKeyboard:
    def __init__(self):
        self.inserted: list[str] = []

    def insert_text(self, text):
        self.inserted.append(text)


class FakePage:
    def __init__(self):
        self.keyboard = FakeKeyboard()
        self.waits: list[int] = []

    def wait_for_timeout(self, ms):
        self.waits.append(ms)


BODY = "Дайджест\n1. first\n2. second\n3. third\n• bullet\n• two\nend"


def test_the_whole_body_goes_in_as_one_insertion():
    box, page = FakeBox(), FakePage()
    type_text(box, page, BODY)
    assert page.keyboard.inserted == [BODY]


def test_no_shift_enter_is_pressed():
    """Shift+Enter is what triggers the editor's list continuation."""
    box, page = FakeBox(), FakePage()
    type_text(box, page, BODY)
    assert box.presses == []


def test_nothing_is_typed_key_by_key():
    """Per-character typing blew Playwright's 30s timeout on long bodies, and
    a literal Enter would send a fragment mid-body."""
    box, page = FakeBox(), FakePage()
    type_text(box, page, BODY)
    assert len(page.keyboard.inserted) == 1


def test_the_composer_is_focused_first():
    box, page = FakeBox(), FakePage()
    type_text(box, page, BODY)
    assert box.clicked is True


def test_a_longer_body_is_given_longer_to_settle():
    assert _settle_ms("x" * 3000) > _settle_ms("short")


def test_settle_time_stays_bounded():
    """A huge body must not stall the tick waiting on the DOM."""
    assert _settle_ms("x" * 100_000) <= 3000


# --- the diagnostic --------------------------------------------------------

def test_mismatch_reports_where_not_just_the_opening_characters():
    """The old message printed the first 60 characters of each, which for a
    long body is identical prose -- it could not diagnose a real corruption."""
    expected = "same opening text\n" * 5 + "2. second"
    got = "same opening text\n" * 5 + "2. 2. second"
    message = describe_mismatch(expected, got)
    assert "diverges at character" in message
    assert "2. 2. second" in message


def test_mismatch_names_both_lengths():
    message = describe_mismatch("abcdef", "abcXef")
    assert "of 6" in message and "has 6" in message


def test_an_empty_composer_is_reported_plainly():
    assert "composer is empty" in describe_mismatch("some text", "")


def test_a_truncated_composer_is_caught():
    message = describe_mismatch("one two three four", "one two")
    assert "diverges at character" in message


# --- waiting for a chat to open --------------------------------------------

class _Node:
    def __init__(self, present=True, text=""):
        self._present, self._text = present, text

    def count(self):
        return 1 if self._present else 0

    def inner_text(self, timeout=None):
        return self._text

    def get_attribute(self, name):
        return None

    @property
    def first(self):
        return self


class _Locator:
    """Returns the header after `header_after` polls, composer always."""

    def __init__(self, header_after=0):
        self.calls, self.header_after = 0, header_after

    def __call__(self, selector):
        from wa_session import selectors as sel
        if selector == sel.HEADER_TITLE:
            self.calls += 1
            if self.header_after is None:      # never opens
                return _Node(text="")
            return _Node(text="Ann" if self.calls > self.header_after else "")
        return _Node(present=True)


class _Page:
    def __init__(self, header_after=0):
        self.locator = _Locator(header_after)
        self.waited = 0

    def wait_for_timeout(self, ms):
        self.waited += ms


def test_it_returns_as_soon_as_the_chat_is_open():
    """A flat 2.5-3.5s sleep was paid on every capture, draft and send, twice
    per productive tick, whether the pane took 200ms or three seconds."""
    from wa_session.compose import wait_for_chat_ready
    page = _Page(header_after=0)
    assert wait_for_chat_ready(page, expected="Ann") == "Ann"
    assert page.waited == 0, "returned without sleeping when already open"


def test_it_keeps_polling_until_the_pane_appears():
    from wa_session.compose import wait_for_chat_ready
    page = _Page(header_after=3)
    assert wait_for_chat_ready(page, expected="Ann") == "Ann"
    assert page.waited > 0


def test_it_gives_up_at_the_deadline_rather_than_hanging():
    """Callers verify the recipient themselves; that check is the one that
    must refuse, so this returns what it has instead of raising."""
    from wa_session.compose import wait_for_chat_ready
    page = _Page(header_after=None)      # the pane never appears
    assert wait_for_chat_ready(page, expected="Ann", timeout_ms=120) == ""


def test_a_different_chat_does_not_satisfy_the_wait():
    from wa_session.compose import wait_for_chat_ready
    page = _Page(header_after=0)
    assert wait_for_chat_ready(page, expected="Bob", timeout_ms=120) == "Ann"
