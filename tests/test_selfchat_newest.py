"""A read must never lose the NEWEST messages. That is where commands are.

Scrolling fixed the original keyhole (1 row of 19) and quietly created its
opposite. `load_more` reaches its target by setting scrollTop to 0; WhatsApp
then unrenders the rows scrolled away from, leaving them in the DOM with empty
text, and `extract_messages` drops empty rows. So a read that had to scroll
returned the OLDEST messages and omitted the newest.

Measured on a live 45-message self-chat, 2026-09-06:

    read(limit= 8)  -> newest present
    read(limit=12)  -> newest present
    read(limit=25)  -> newest DROPPED
    read(limit=40)  -> newest present
    read(limit=60)  -> newest DROPPED   <- the default read_after uses

Non-monotonic, so it could not even be relied on to fail consistently. The
consequence is the worst one available: `read_after` starts from the draft's
own message, which is one of the newest, so it could not find its marker,
returned nothing by its own safety rule, and an `OK #XXX` sitting plainly in
the chat was never seen. Verified end to end by posting a message after a live
draft: at limit=60 `read_after` returned 0.
"""

import pytest

import wa_session.messages as messages
import wa_session.selfchat as selfchat


class _M:
    def __init__(self, i):
        self.text, self.msg_id = f"m{i}", f"id{i}"

    def as_dict(self):
        return {"text": self.text, "msg_id": self.msg_id}

    def __repr__(self):
        return self.msg_id


class VirtualisedPage:
    """A message list that renders a WINDOW, like WhatsApp's.

    `history` is every message. Only `window` of them are rendered around the
    current offset; everything else is present but empty, which is exactly
    what `extract_messages` filters out.
    """

    def __init__(self, total=45, window=24, loadable=True):
        self.history = [_M(i) for i in range(total)]
        self.window = window
        self.offset = max(0, total - window)     # opens at the bottom
        self.loadable = loadable
        self.waits = 0
        self.scroll_top = float(self.offset)

    def position(self) -> float:
        return self.scroll_top

    def rendered(self):
        return self.history[self.offset:self.offset + self.window]

    def scroll_up(self):
        self.offset = max(0, self.offset - int(self.window * 0.8))
        self.scroll_top = float(self.offset)

    def to_bottom(self):
        self.offset = max(0, len(self.history) - self.window)

    def wait_for_timeout(self, ms):
        self.waits += 1

    def locator(self, selector):
        class _None:
            def count(self):
                return 0

            @property
            def first(self):
                return self
        return _None()


@pytest.fixture
def wired(monkeypatch):
    """Drive the real `_read_windows` against the fake virtualised list."""
    def _wire(page):
        monkeypatch.setattr(selfchat, "open_self_chat", lambda p: "self")
        monkeypatch.setattr(messages, "extract_messages",
                            lambda p: list(p.rendered()))

        def fake_bottom(p):
            p.to_bottom()

        def fake_scroll(p):
            p.scroll_up()
            return p.position()         # stands in for scrollTop

        monkeypatch.setattr(messages, "scroll_to_bottom", fake_bottom)
        monkeypatch.setattr(messages, "scroll_up_one", fake_scroll)
    return _wire


def test_the_newest_message_survives_a_deep_read(wired):
    """The regression, stated as plainly as it can be."""
    page = VirtualisedPage(total=45, window=24)
    wired(page)
    got = selfchat.read(page, limit=60, refresh=True)
    assert got[-1].msg_id == "id44", f"newest lost; got {got[-1]}"


@pytest.mark.parametrize("limit", [8, 12, 25, 40, 60, 100])
def test_the_newest_survives_at_every_limit(wired, limit):
    """It used to depend on the limit in a way nobody could predict."""
    page = VirtualisedPage(total=45, window=24)
    wired(page)
    got = selfchat.read(page, limit=limit, refresh=True)
    assert got[-1].msg_id == "id44"


def test_a_deep_read_really_does_reach_back(wired):
    """Not solved by simply refusing to scroll: depth is still required."""
    page = VirtualisedPage(total=45, window=24)
    wired(page)
    got = selfchat.read(page, limit=45, refresh=True)
    assert len(got) == 45
    assert got[0].msg_id == "id0" and got[-1].msg_id == "id44"


def test_messages_come_back_in_order_without_duplicates(wired):
    page = VirtualisedPage(total=45, window=24)
    wired(page)
    got = selfchat.read(page, limit=60, refresh=True)
    ids = [m.msg_id for m in got]
    assert ids == sorted(ids, key=lambda s: int(s[2:]))
    assert len(ids) == len(set(ids))


def test_a_short_history_needs_no_scrolling_at_all(wired):
    page = VirtualisedPage(total=10, window=24)
    wired(page)
    got = selfchat.read(page, limit=60, refresh=True)
    assert [m.msg_id for m in got] == [f"id{i}" for i in range(10)]


def test_it_stops_when_the_history_runs_out(wired):
    """Asking for more than exists must terminate, not spin to max_steps."""
    page = VirtualisedPage(total=30, window=24)
    wired(page)
    got = selfchat.read(page, limit=500, refresh=True)
    assert len(got) == 30


def test_the_page_is_left_at_the_bottom(wired):
    """Later callers pass scroll=False and read whatever is rendered; leaving
    the list parked halfway up would hand them the wrong messages."""
    page = VirtualisedPage(total=45, window=24)
    wired(page)
    selfchat.read(page, limit=60, refresh=True)
    assert page.offset == len(page.history) - page.window


# --- the merge itself ------------------------------------------------------

def test_merge_stitches_on_the_overlap():
    older = [_M(0), _M(1), _M(2), _M(3)]
    newer = [_M(2), _M(3), _M(4)]
    assert [m.msg_id for m in messages.merge_older(older, newer)] == [
        "id0", "id1", "id2", "id3", "id4"]


def test_merge_without_an_overlap_keeps_everything():
    """A jump too far must not silently drop the messages in between."""
    older = [_M(0), _M(1)]
    newer = [_M(8), _M(9)]
    assert [m.msg_id for m in messages.merge_older(older, newer)] == [
        "id0", "id1", "id8", "id9"]


def test_merge_onto_nothing_is_the_window_itself():
    merged = messages.merge_older([_M(1), _M(2)], [])
    assert [m.msg_id for m in merged] == ["id1", "id2"]


def test_merge_never_duplicates_when_windows_repeat():
    same = [_M(1), _M(2)]
    merged = messages.merge_older(same, same)
    assert [m.msg_id for m in merged] == ["id1", "id2"]


# --- and the approval path, which is what this is for ----------------------

def test_read_after_finds_a_command_typed_below_the_draft(wired):
    """The end-to-end property. The draft sits near the bottom; the approval
    below it. Both are in the half a scrolling read used to discard."""
    page = VirtualisedPage(total=45, window=24)
    wired(page)
    marker = "id43"                       # the draft
    after = selfchat.read_after(page, marker, limit=60)
    assert [m.msg_id for m in after] == ["id44"], "the approval must be visible"


# --- depth must not be traded away for the newest --------------------------

class LazyLoadingPage(VirtualisedPage):
    """Like WhatsApp: older messages arrive only after the pane reaches the
    top, and not on the very first step once it does."""

    def __init__(self, loaded=25, total=45, window=24, latency=2):
        super().__init__(total=loaded, window=window)
        self.full = [_M(i) for i in range(total)]
        self.loaded = loaded
        self.latency = latency
        self.at_top_for = 0
        self._resync()

    def _resync(self):
        self.history = self.full[len(self.full) - self.loaded:]

    def scroll_up(self):
        was_top = self.offset == 0
        super().scroll_up()
        if was_top and self.offset == 0:
            self.at_top_for += 1
            if self.at_top_for > self.latency and self.loaded < len(self.full):
                # Older messages are prepended ABOVE the current position, and
                # we are pinned at the top, so they render straight away.
                self.loaded = min(len(self.full), self.loaded + 10)
                self.offset = 0
                self._resync()
        else:
            self.at_top_for = 0


def test_a_deep_read_waits_for_lazily_loaded_history(wired):
    """Breaking on the first step that added nothing capped every deep read at
    one screenful: 45 messages became 25 on the live chat."""
    page = LazyLoadingPage(loaded=25, total=45)
    wired(page)
    got = selfchat.read(page, limit=45, refresh=True)
    assert len(got) > 25, f"only reached {len(got)}"
    assert got[-1].msg_id == "id44", "and still ends at the newest"


def test_patience_is_bounded(wired):
    """A chat that really has ended must not spin for the full budget."""
    page = VirtualisedPage(total=12, window=24)
    wired(page)
    got = selfchat.read(page, limit=500, refresh=True)
    assert len(got) == 12
    assert page.waits <= messages.STALL_STEPS + 1


class SlowTopPage(VirtualisedPage):
    """The live behaviour: several steps of travel before the top is reached,
    and only then does older history appear.

    Measured on the real self-chat: scrollTop fell 4509 -> 801 over six steps
    with the row count pinned at 25, and only on the seventh did it jump to 48
    rows of loaded history.
    """

    def __init__(self, loaded=25, total=45, window=24, steps_to_top=6):
        super().__init__(total=loaded, window=window)
        self.full = [_M(i) for i in range(total)]
        self.loaded = loaded
        self.steps_to_top = steps_to_top
        self.scroll_top = 4509.0
        self.drop = self.scroll_top / steps_to_top
        self._resync()

    def _resync(self):
        self.history = self.full[len(self.full) - self.loaded:]

    def scroll_up(self):
        if self.scroll_top > 0:
            # Crossing a page already read: the position falls, but every
            # message here is one we have seen.
            self.scroll_top = max(0.0, self.scroll_top - self.drop)
            self.offset = max(0, self.offset - 1)
            return
        if self.loaded < len(self.full):
            self.loaded = min(len(self.full), self.loaded + 20)
            self.offset = 0
            self.scroll_top = 4509.0        # WhatsApp restores the position
            self._resync()


def test_travelling_to_the_top_is_not_mistaken_for_the_end(wired):
    """Six no-growth steps precede the first older page on the live chat. A
    stall counter that could not tell travel from exhaustion stopped after
    three, and a read(limit=60) that had reached 45 messages returned 25."""
    page = SlowTopPage(loaded=25, total=45, steps_to_top=6)
    wired(page)
    got = selfchat.read(page, limit=45, refresh=True)
    assert len(got) > 25, f"gave up while still travelling; got {len(got)}"
    assert got[-1].msg_id == "id44", "and still ends at the newest"


def test_a_pinned_scroll_position_still_terminates(wired):
    """If the position stops falling and nothing loads, that IS the end."""
    page = SlowTopPage(loaded=25, total=25, steps_to_top=3)
    wired(page)
    got = selfchat.read(page, limit=500, refresh=True)
    assert len(got) == 25


# --- and the group path, which is where this actually bit ------------------

def test_capture_chat_keeps_the_newest_at_depth(wired):
    """GROUPSUM's watermark is a RECENT message. `capture_chat` used
    `load_more`, so a deep capture scrolled to the top, unrendered the bottom,
    and dropped the mark -- every group then reported a gap and the whole
    history was re-summarised as if new.

    Live on 2026-09-06: capture_chat(expected_unread=10) kept the newest,
    capture_chat(expected_unread=60) lost it. Raising SUMMARY_CAPTURE_DEPTH
    from 15 to 60 is what exposed it; at 15 `load_more` never scrolled at all,
    which is the only reason it had ever looked correct.
    """
    page = VirtualisedPage(total=45, window=24)
    wired(page)
    cap = messages.capture_chat(page, "a group", expected_unread=60)
    assert cap.messages[-1].msg_id == "id44", "the watermark's end was lost"


def test_a_watermark_near_the_end_is_still_findable(wired):
    """The property GROUPSUM actually depends on: the last summarised message
    must be inside the captured window, or `window()` reports a gap and
    everything captured is treated as new."""
    from wa_session.watermarks import window as watermark_window

    page = VirtualisedPage(total=45, window=24)
    wired(page)
    cap = messages.capture_chat(page, "a group", expected_unread=60)
    captured = [m.as_dict() for m in cap.messages]

    seen = watermark_window(captured, "id42", cap=120)
    assert seen.gap is False, f"spurious gap: {seen.reason}"
    assert [m["msg_id"] for m in seen.messages] == ["id43", "id44"]
