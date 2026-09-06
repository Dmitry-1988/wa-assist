"""Your "Message yourself" chat -- the approval channel.

Chosen because it involves no other person: opening it sends no read receipts,
and only you can post into it, so a message found here is authentically yours.

The chat's name is your own profile name, which varies, so it is read from the
page rather than hardcoded -- and then used as the expected recipient for the
send guards in `compose`.
"""

from __future__ import annotations

from . import selectors
from .compose import (SendResult, header_recipient, send_message,
                      wait_for_chat_ready)
from .interstitials import dismiss
from .messages import Message, extract_messages, load_more

SELF_CHAT_ROW = '[data-testid="message-yourself-row"]'

# Consecutive scroll steps that may add nothing before a read concludes it has
# reached the start of the conversation.
STALL_STEPS = 3


class SelfChatUnavailable(Exception):
    """The self-chat could not be opened. Never proceed past this."""


def open_self_chat(page, timeout_ms: int = 8000) -> str:
    """Open the self-chat and return its name, for use as the send target."""
    dismiss(page)
    row = page.locator(SELF_CHAT_ROW).first
    if not row.count():
        raise SelfChatUnavailable(
            "'Message yourself' row not found in the chat list"
        )
    row.click(timeout=timeout_ms)
    name = wait_for_chat_ready(page, timeout_ms=timeout_ms)
    dismiss(page)
    name = name or header_recipient(page)
    if not name:
        raise SelfChatUnavailable("opened a chat but could not read its title")
    return name


def post(page, text: str, dry_run: bool = False) -> SendResult:
    """Post into the self-chat.

    Unlike `compose.send_message`, dry_run defaults to False here: writing to
    your own notes reaches nobody else, and a draft that is never posted would
    silently break the approval loop.
    """
    name = open_self_chat(page)
    result = send_message(page, name, text, dry_run=dry_run)
    invalidate(page)      # the cached history no longer includes what we sent
    return result


_CACHE_ATTR = "_wa_selfchat_cache"


def invalidate(page) -> None:
    """Forget the cached read. Called after anything is posted."""
    try:
        setattr(page, _CACHE_ATTR, None)
    except Exception:
        pass


def _scroll_to_bottom(page) -> None:
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


def _scroll_up_one(page) -> float | None:
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


def _merge_older(window: list[Message], newer: list[Message]) -> list[Message]:
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


def _read_windows(page, minimum: int, max_steps: int = 20) -> list[Message]:
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
    _scroll_to_bottom(page)
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
        top = _scroll_up_one(page)
        if top is None:
            break
        page.wait_for_timeout(1200)
        merged = _merge_older(extract_messages(page), merged)

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
    _scroll_to_bottom(page)
    return _merge_older(merged, extract_messages(page))


def read(page, limit: int = 60, refresh: bool = False,
         scroll: bool = True) -> list[Message]:
    """Read the self-chat, oldest first, including the newest messages.

    WhatsApp virtualises the message list: reopening a chat can render a single
    row even when the conversation has dozens. Without scrolling, this returned
    1 message out of 19 -- and this is the function every tick uses to find
    your GROUPSUM, your approval and any command. A request landing outside
    that keyhole was silently never seen, which looked exactly like the daemon
    ignoring you.

    Scrolling alone was not enough, and traded one keyhole for another: see
    `_read_windows` for why reading after a scroll lost the newest messages
    instead of the oldest.
    """
    want = limit or 60
    if not refresh:
        cached = getattr(page, _CACHE_ATTR, None)
        # Reusable only if it was loaded at least as deep as this caller needs.
        if cached and cached[0] >= want:
            return cached[1][-limit:] if limit else cached[1]

    open_self_chat(page)
    if scroll:
        # Scrolling is what makes this correct, and it is not cheap, so a tick
        # pays for it once: several callers read the self-chat per cycle.
        messages = _read_windows(page, minimum=want)
    else:
        messages = extract_messages(page)
    if scroll:
        try:
            setattr(page, _CACHE_ATTR, (want, messages))
        except Exception:
            pass
    return messages[-limit:] if limit else messages


def read_after(page, marker_id: str, limit: int = 60) -> list[Message]:
    """Messages posted after the message with id `marker_id`.

    Used to find your approval, which must come AFTER the draft -- an earlier
    "OK" for a previous draft must never approve a later one.
    """
    messages = read(page, limit=limit)
    if not marker_id:
        return messages
    for index, message in enumerate(messages):
        if message.msg_id == marker_id:
            return messages[index + 1:]
    # Marker not visible (scrolled out): return nothing rather than risk
    # matching an approval that predates the draft.
    return []


def find_message_id(messages: list[Message], text_fragment: str) -> str:
    """Locate the id of the message containing `text_fragment` (e.g. a draft id)."""
    for message in reversed(messages):
        if text_fragment and text_fragment in message.text:
            return message.msg_id
    return ""
