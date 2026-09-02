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


def capture_chat(page, name: str, expected_unread: int) -> ChatCapture:
    """Extract an already-opened chat. Never raises; records errors instead."""
    capture = ChatCapture(name=name, expected_unread=expected_unread)
    try:
        # Load a little beyond the unread count so context is available.
        load_more(page, minimum=expected_unread + 5)
        capture.messages = extract_messages(page)
    except Exception as exc:
        capture.error = f"parse failed: {exc}"
    try:
        pane = page.locator(selectors.CONVERSATION).first
        if pane.count():
            capture.raw_html = pane.evaluate("el => el.outerHTML") or ""
    except Exception as exc:
        capture.error = (capture.error + f" | html failed: {exc}").strip(" |")
    return capture
