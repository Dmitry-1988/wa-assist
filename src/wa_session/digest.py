"""Turning unread chats into something readable.

`Summarizer` is the seam: the local digest ships now, and an LLM-backed
summarizer can be dropped in later without touching extraction or rendering.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .unread import UnreadChat, sort_chats, total_unread, truncate


@runtime_checkable
class Summarizer(Protocol):
    """Produces one short line (or paragraph) describing a chat's unreads."""

    name: str

    def summarize(self, chat: UnreadChat) -> str: ...


class LocalDigest:
    """The default: echoes the preview WhatsApp already rendered.

    Deterministic, free, and nothing leaves the machine.
    """

    name = "local"

    def __init__(self, width: int = 72) -> None:
        self.width = width

    def summarize(self, chat: UnreadChat) -> str:
        preview = truncate(chat.preview, self.width)
        return preview or "(no preview available)"


def render(chats: list[UnreadChat], summarizer: Summarizer | None = None) -> str:
    """Render the digest as plain text."""
    summarizer = summarizer or LocalDigest()
    if not chats:
        return "No chats with unread messages."

    ordered = sort_chats(chats)
    header = (
        f"{len(ordered)} chat{'s' if len(ordered) != 1 else ''} with unread "
        f"messages ({total_unread(ordered)} total)"
    )
    lines = [header, ""]
    for chat in ordered:
        plural = "s" if chat.unread_count != 1 else ""
        meta = f"{chat.unread_count} unread{plural}"
        if chat.timestamp:
            meta += f" · {chat.timestamp}"
        # Name and body on separate lines: these chats are often RTL or
        # Cyrillic, and fixed-width columns render badly for both.
        lines.append(f"● {chat.name} — {meta}")
        lines.append(f"    {summarizer.summarize(chat)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
