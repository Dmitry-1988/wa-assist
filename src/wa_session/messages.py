"""Reading message text out of an opened chat.

WARNING: everything here requires OPENING a chat, which marks it read and sends
read receipts to the sender. That is irreversible and visible to them. Nothing
in this module runs unless the caller explicitly asks for it.

Because the unread boundary is destroyed the moment a chat opens, callers get
exactly one attempt: `capture_chat` therefore returns the raw pane HTML
alongside the parsed messages so a parsing mistake can be fixed offline instead
of needing a second run that no longer exists.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from . import selectors

# "[17:05, 8/27/2026] Dmitrymel: "
_PRE_PLAIN = re.compile(
    r"^\[(?P<time>[^,\]]+),\s*(?P<date>[^\]]+)\]\s*(?P<sender>.*?):\s*$"
)


@dataclass(frozen=True)
class Message:
    sender: str = ""
    time: str = ""
    date: str = ""
    text: str = ""
    msg_id: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class ChatCapture:
    """One chat's captured content, plus the raw HTML that produced it."""

    name: str
    expected_unread: int
    messages: list[Message] = field(default_factory=list)
    raw_html: str = ""
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "expected_unread": self.expected_unread,
            "message_count": len(self.messages),
            "messages": [m.as_dict() for m in self.messages],
            "error": self.error,
        }


def parse_pre_plain(raw: str | None) -> tuple[str, str, str]:
    """Split "[17:05, 8/27/2026] Dmitrymel: " into (time, date, sender)."""
    if not raw:
        return ("", "", "")
    match = _PRE_PLAIN.match(raw.strip())
    if not match:
        return ("", "", "")
    return (match.group("time"), match.group("date"), match.group("sender"))


_EXTRACT_JS = """
(sel) => {
  const rows = Array.from(document.querySelectorAll(sel.rows));
  return rows.map(r => {
    const pre = r.querySelector(sel.pre);
    const txt = r.querySelector(sel.text);
    const holder = r.querySelector('[data-id]');
    return {
      pre: pre ? pre.getAttribute('data-pre-plain-text') : null,
      text: txt ? (txt.innerText || '') : '',
      rowText: (r.innerText || ''),
      msgId: holder ? (holder.getAttribute('data-id') || '') : '',
    };
  });
}
"""


def extract_messages(page) -> list[Message]:
    """Read every message currently rendered in the open conversation."""
    raw_rows = page.evaluate(
        _EXTRACT_JS,
        {
            "rows": selectors.MESSAGE_ROWS,
            "pre": selectors.MSG_PRE_PLAIN,
            "text": selectors.MSG_TEXT,
        },
    )
    messages: list[Message] = []
    for row in raw_rows:
        time_, date_, sender = parse_pre_plain(row.get("pre"))
        text = (row.get("text") or "").strip()
        if not text:
            # System notices (encryption banners, date separators) have no
            # message body; keep them out rather than emitting blanks.
            continue
        messages.append(
            Message(
                sender=sender,
                time=time_,
                date=date_,
                text=" ".join(text.split()),
                msg_id=(row.get("msgId") or ""),
            )
        )
    return messages


def load_more(page, minimum: int, max_scrolls: int = 12) -> int:
    """Scroll the message pane up until at least `minimum` messages exist."""
    previous = -1
    for _ in range(max_scrolls):
        count = page.locator(selectors.MESSAGE_ROWS).count()
        if count >= minimum or count == previous:
            return count
        previous = count
        for scroller in selectors.MSG_SCROLLER:
            try:
                node = page.locator(scroller).first
                if node.count():
                    node.evaluate("el => { el.scrollTop = 0; }")
                    break
            except Exception:
                continue
        page.wait_for_timeout(1200)
    return page.locator(selectors.MESSAGE_ROWS).count()


# Consecutive scroll steps that may add nothing before a read concludes it has
# reached the start of the conversation.
STALL_STEPS = 3


def scroll_to_bottom(page) -> None:
    """Put the newest messages back in the viewport, and rendered."""
    for scroller in selectors.MSG_SCROLLER:
        try:
            node = page.locator(scroller).first
            if node.count():
                node.evaluate("el => { el.scrollTop = el.scrollHeight; }")
                break
        except Exception:
            continue
    page.wait_for_timeout(900)


def scroll_up_one(page) -> float | None:
    """Scroll up ONE viewport. Returns the resulting scrollTop, None if there
    is no scroller.

    One viewport at a time, not `scrollTop = 0`: the jump to the very top is
    what unrenders everything below it. Reaching the top is still how WhatsApp
    is asked to load older history, this just gets there in steps that can be
    read on the way.

    The returned position is what tells the caller whether it is still
    travelling or has arrived -- measured on the live chat, it takes six steps
    to cross a loaded page before any older history appears.
    """
    for scroller in selectors.MSG_SCROLLER:
        try:
            node = page.locator(scroller).first
            if node.count():
                return node.evaluate(
                    "el => { el.scrollTop = Math.max("
                    "0, el.scrollTop - el.clientHeight * 0.8);"
                    " return el.scrollTop; }")
        except Exception:
            continue
    return None


def merge_older(window: list[Message], newer: list[Message]) -> list[Message]:
    """Prepend the part of `window` that sits above `newer`.

    The two overlap, because a scroll step is smaller than a viewport. Find
    where `newer` begins inside `window` and keep everything before it.
    """
    if not newer:
        return list(window)
    head = newer[0].msg_id
    if head:
        for index, message in enumerate(window):
            if message.msg_id == head:
                return window[:index] + newer
    known = {m.msg_id for m in newer if m.msg_id}
    return [m for m in window if m.msg_id and m.msg_id not in known] + newer


def read_window(page, minimum: int, max_steps: int = 20) -> list[Message]:
    """Read upwards from the bottom, keeping what scrolls out of view.

    `load_more` reaches its target by setting scrollTop to 0, and WhatsApp
    then unrenders the rows it has scrolled away from. Those rows stay in the
    DOM with empty text, and `extract_messages` drops empty rows -- so reading
    after a scroll to the top returns the OLDEST messages and silently omits
    the newest. Measured on a 45-message self-chat: `read(limit=60)` returned
    45 messages, none of which were the three most recent.

    That is the worst possible half to lose. Every command arrives at the
    bottom, so `read_after` could not find the draft it was told to start
    from, returned nothing by its own safety rule, and an `OK #XXX` sitting
    plainly in the chat was never seen.

    So: extract at the bottom first, then scroll up a window at a time and
    merge each view onto the front. Nothing that has been seen is lost when
    the next scroll unrenders it.
    """
    scroll_to_bottom(page)
    merged = extract_messages(page)

    # A step that adds nothing is not proof the history has ended. WhatsApp
    # only fetches older messages once the pane is scrolled to the very top,
    # and the fetch is asynchronous, so the first step or two after arriving
    # there legitimately return what we already have. Breaking on the first
    # of those capped every deep read at one screenful.
    stalled = 0
    last_top = None
    for _ in range(max_steps):
        if len(merged) >= minimum:
            break
        before = len(merged)
        top = scroll_up_one(page)
        if top is None:
            break
        page.wait_for_timeout(1200)
        merged = merge_older(extract_messages(page), merged)

        # Two different reasons a step can add nothing, and only one of them
        # means stop. While scrollTop is still falling we are crossing a page
        # we have already read; that is progress, not exhaustion. Counting it
        # as a stall capped every deep read at one screenful.
        grew = len(merged) > before
        travelling = last_top is None or top < last_top - 1
        last_top = top
        if grew or travelling:
            stalled = 0
        else:
            stalled += 1
            if stalled >= STALL_STEPS:
                break      # at the top, and nothing older is arriving

    # Leave the newest rendered. Later reads pass scroll=False and would
    # otherwise be looking at whatever this call happened to stop on.
    scroll_to_bottom(page)
    return merge_older(merged, extract_messages(page))


def capture_chat(page, name: str, expected_unread: int) -> ChatCapture:
    """Extract an already-opened chat. Never raises; records errors instead."""
    capture = ChatCapture(name=name, expected_unread=expected_unread)
    try:
        # Load a little beyond the unread count so context is available.
        #
        # `read_window`, not `load_more`: a deep capture scrolls to the top,
        # which unrenders the bottom, and the bottom is where the newest
        # messages are. GROUPSUM keeps a watermark on the last message it
        # summarised -- a recent one -- so losing the tail meant the mark fell
        # outside the captured window, every group reported a gap, and the
        # whole history was re-summarised as if new. Seen on 2026-09-06 across
        # all four monitored groups at once, immediately after the capture
        # depth was raised from 15 to 60: at 15 `load_more` never scrolled at
        # all, which is the only reason it had looked correct.
        capture.messages = read_window(page, minimum=expected_unread + 5)
    except Exception as exc:
        capture.error = f"parse failed: {exc}"
    try:
        pane = page.locator(selectors.CONVERSATION).first
        if pane.count():
            capture.raw_html = pane.evaluate("el => el.outerHTML") or ""
    except Exception as exc:
        capture.error = (capture.error + f" | html failed: {exc}").strip(" |")
    return capture
