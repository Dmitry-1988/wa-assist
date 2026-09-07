"""Your "Message yourself" chat -- the approval channel.

Chosen because it involves no other person: opening it sends no read receipts,
and only you can post into it, so a message found here is authentically yours.

The chat's name is your own profile name, which varies, so it is read from the
page rather than hardcoded -- and then used as the expected recipient for the
send guards in `compose`.
"""

from __future__ import annotations

import re
import time

from . import selectors
from .compose import (SendResult, header_recipient, send_message,
                      wait_for_chat_ready)
from .interstitials import dismiss
from .messages import (Message, extract_messages, merge_older,
                       read_window, scroll_to_bottom, scroll_up_one)

SELF_CHAT_ROW = '[data-testid="message-yourself-row"]'


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


# WhatsApp puts the per-message delivery status in an aria-label on the row:
# "Pending" until the server has it, then "Sent" / "Delivered" / "Read".
# Measured on a live self-chat 2026-09-07: Pending at 1.9s, Read at 2.4s.
_ACKED = re.compile(r"read|deliver|sent", re.IGNORECASE)
_PENDING = re.compile(r"pending", re.IGNORECASE)

_STATUS_JS = """(msgId) => {
  const holder = document.querySelector('#main [data-id="' +
                 msgId.replace(/"/g, '\\"') + '"]');
  if (!holder) return 'absent';
  const row = holder.closest('[role="row"]') || holder;
  const labels = [...row.querySelectorAll('[aria-label]')]
    .map(n => (n.getAttribute('aria-label') || '').trim())
    .filter(s => /read|deliver|sent|pending/i.test(s));
  return labels.length ? labels.join(',') : 'none';
}"""


def delivery_state(page, msg_id: str) -> str:
    """'absent', 'none', or whatever status WhatsApp is showing for `msg_id`."""
    if not msg_id:
        return "absent"
    try:
        return str(page.evaluate(_STATUS_JS, msg_id) or "none")
    except Exception:
        return "none"


def wait_for_delivery(page, msg_id: str, timeout_s: float = 20.0) -> str:
    """Block until WhatsApp acknowledges `msg_id`. Returns the final state.

    Rendering is not sending. A message appears in the composer's own chat
    immediately and sits at "Pending" until the server takes it -- so reading
    the text back proves only that this browser drew it, which is exactly what
    `post_note` was doing when it declared success.

    The daemon then closes the browser, and a message still Pending at that
    moment is simply lost: no error anywhere, `summary_posted` in the log, and
    nothing on the user's phone. One digest survived this on 2026-09-07 and the
    next one, eleven minutes later, did not.

    Caller decides what a non-acknowledged result means; this only waits.
    """
    deadline = time.monotonic() + timeout_s
    state = delivery_state(page, msg_id)
    while True:
        if _ACKED.search(state):
            return state
        if time.monotonic() >= deadline:
            return state
        page.wait_for_timeout(400)
        state = delivery_state(page, msg_id)


_CACHE_ATTR = "_wa_selfchat_cache"


def invalidate(page) -> None:
    """Forget the cached read. Called after anything is posted."""
    try:
        setattr(page, _CACHE_ATTR, None)
    except Exception:
        pass


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
        messages = read_window(page, minimum=want)
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
