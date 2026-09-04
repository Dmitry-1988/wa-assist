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


def read(page, limit: int = 60, refresh: bool = False,
         scroll: bool = True) -> list[Message]:
    """Read the self-chat, oldest first, scrolling until `limit` are loaded.

    WhatsApp virtualises the message list: reopening a chat can render a single
    row even when the conversation has dozens. Without scrolling, this returned
    1 message out of 19 -- and this is the function every tick uses to find
    your GROUPSUM, your approval and any command. A request landing outside
    that keyhole was silently never seen, which looked exactly like the daemon
    ignoring you.

    `read_chat` had always scrolled via `load_more`; the self-chat, the one
    place where every instruction arrives, did not.
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
        load_more(page, minimum=want)
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
