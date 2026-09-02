"""Chats with unread messages, read from the chat list only.

Privacy contract for this module: it reads what WhatsApp Web has already
rendered in the chat-list rows -- contact name, unread badge, timestamp and the
one-line preview snippet. It never opens a chat. Opening a chat would mark it
read and send read receipts to the sender, which is visible to them and cannot
be undone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# "3 unread messages", "1 unread message", or a bare badge like "5" / "99+".
_COUNT_IN_LABEL = re.compile(r"(\d+)\s*(?:\+)?\s*unread", re.IGNORECASE)
_BARE_COUNT = re.compile(r"^\s*(\d+)\s*\+?\s*$")


@dataclass(frozen=True)
class UnreadChat:
    """One chat-list row that has unread messages."""

    name: str
    unread_count: int
    timestamp: str = ""
    preview: str = ""

    def __post_init__(self) -> None:
        if self.unread_count < 0:
            raise ValueError("unread_count cannot be negative")


def parse_unread_count(raw: str | None) -> int:
    """Read an unread count from a badge label or badge text.

    Returns 0 when there is nothing countable, so a markup change degrades to
    "no unreads" rather than to a crash.
    """
    if not raw:
        return 0
    text = raw.strip()
    match = _COUNT_IN_LABEL.search(text)
    if match:
        return int(match.group(1))
    match = _BARE_COUNT.match(text)
    if match:
        return int(match.group(1))
    return 0


def truncate(text: str, limit: int = 72) -> str:
    """Shorten a preview for display, without splitting on a trailing space."""
    clean = " ".join((text or "").split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rstrip() + "…"


def total_unread(chats: list[UnreadChat]) -> int:
    return sum(chat.unread_count for chat in chats)


def sort_chats(chats: list[UnreadChat]) -> list[UnreadChat]:
    """Busiest first, then alphabetically so the order is stable."""
    return sorted(chats, key=lambda c: (-c.unread_count, c.name.casefold()))


# --- extraction ------------------------------------------------------------
# Everything below reads the chat-list rows WhatsApp has already rendered. It
# never opens a chat, so no read receipts are sent and nothing is marked read.

# The unread badge is nested inside the title and preview cells, so its text
# ("12 unread messages", "12") lands in both unless it is removed first.
_TEXT_JS = """
(el, excludes) => {
  const clone = el.cloneNode(true);
  for (const sel of excludes) clone.querySelectorAll(sel).forEach(n => n.remove());
  return (clone.textContent || '').trim();
}
"""

# Bounded on purpose: textContent has no newlines, so an open-ended pattern
# here would swallow the chat name that follows. No \b either -- the name
# often starts with a Hebrew or Cyrillic letter, which is a word character,
# so there is no boundary between "messages" and the name.
_BADGE_NOISE = re.compile(r"^\s*\d+\s*\+?\s*unread\s+messages?\s*", re.IGNORECASE)


def _clean(raw: str) -> str:
    return " ".join(_BADGE_NOISE.sub("", raw or "").split())


def _text(row, selector: str) -> str:
    from . import selectors

    # Icons contribute their internal names ("ic-notifications-off") to
    # textContent, so they are stripped alongside the unread badge.
    excludes = [
        selectors.ROW_UNREAD_BADGE,
        '[aria-label*="unread" i]',
        "[data-icon]",
        "svg",
    ]
    node = row.locator(selector).first
    if not node.count():
        return ""
    try:
        cleaned = _clean(node.evaluate(_TEXT_JS, excludes))
        if cleaned:
            return cleaned
    except Exception:
        pass
    # Fallback: innerText keeps line breaks, so the badge sits on its own line.
    try:
        lines = [ln.strip() for ln in (node.inner_text(timeout=1000) or "").splitlines()]
        keep = [ln for ln in lines if ln and not _BADGE_NOISE.match(ln)]
        return " ".join(" ".join(keep).split())
    except Exception:
        return ""


def _badge_count(row) -> int:
    """Unread count from the row's badge, or 0 when there is no badge."""
    from . import selectors

    try:
        badge = row.locator(selectors.ROW_UNREAD_BADGE).first
        if not badge.count():
            return 0
        label = badge.get_attribute("aria-label") or ""
        return parse_unread_count(label) or parse_unread_count(
            (badge.inner_text(timeout=1000) or "").strip()
        )
    except Exception:
        return 0


def apply_unread_filter(page) -> bool:
    """Switch the chat list to its 'Unread' tab.

    This is a local UI filter: it opens no chat and sends no read receipts. It
    also sidesteps list virtualisation, since only unread rows remain.
    """
    from . import selectors

    try:
        tab = page.locator(selectors.UNREAD_FILTER_TAB).first
        if tab.count() and tab.is_visible(timeout=1500):
            tab.click(timeout=3000)
            page.wait_for_timeout(1200)
            return True
    except Exception:
        pass
    return False


def clear_filter(page) -> None:
    """Put the chat list back on 'All' so the UI is left as we found it."""
    from . import selectors

    try:
        tab = page.locator(selectors.ALL_FILTER_TAB).first
        if tab.count():
            tab.click(timeout=3000)
            page.wait_for_timeout(500)
    except Exception:
        pass


def extract_unread(page, limit: int = 50) -> list[UnreadChat]:
    """Read chats with unread messages from the rendered chat list."""
    from . import selectors
    from .interstitials import dismiss

    dismiss(page)
    filtered = apply_unread_filter(page)

    chats: list[UnreadChat] = []
    try:
        rows = page.locator(selectors.CHAT_ROWS)
        for index in range(min(rows.count(), limit)):
            row = rows.nth(index)
            count = _badge_count(row)
            # Without the filter every row is present, so skip the read ones.
            if count == 0 and not filtered:
                continue
            name = _text(row, selectors.ROW_TITLE)
            if not name:
                continue
            chats.append(
                UnreadChat(
                    name=name,
                    unread_count=count,
                    timestamp=_text(row, selectors.ROW_DETAIL),
                    preview=_text(row, selectors.ROW_PREVIEW),
                )
            )
    finally:
        if filtered:
            clear_filter(page)
    return chats
