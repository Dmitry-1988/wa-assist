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
from .messages import Message, extract_messages

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
    return send_message(page, name, text, dry_run=dry_run)


def read(page, limit: int = 60) -> list[Message]:
    """Read the self-chat's rendered messages, oldest first."""
    open_self_chat(page)
    messages = extract_messages(page)
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
