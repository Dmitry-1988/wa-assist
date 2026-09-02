"""One-shot capture of unread chats.

Opening a chat destroys its unread boundary and sends read receipts, so this
runs exactly once per set of unreads. Every artefact is written to disk the
moment it exists -- the snapshot before any chat is opened, then each chat's
JSON and raw HTML as it is captured -- so a crash halfway through still leaves
everything gathered so far on disk.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from . import selectors
from .config import Config, ensure_private_dir
from .messages import ChatCapture, capture_chat
from .unread import UnreadChat, _text, apply_unread_filter, clear_filter, _badge_count

EXPORT_DIRNAME = ".wa-export"


def export_root(config: Config) -> Path:
    return config.profile_dir.parent / EXPORT_DIRNAME


def new_run_dir(config: Config) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    # Tighten the parent too: mkdir(parents=True) creates it with the umask,
    # which would leave message content group/other readable.
    ensure_private_dir(export_root(config))
    path = export_root(config) / stamp
    ensure_private_dir(path)
    return path


def _write(path: Path, payload) -> None:
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    path.chmod(0o600)


def snapshot_unread(page) -> list[UnreadChat]:
    """Record which chats are unread BEFORE anything is opened."""
    filtered = apply_unread_filter(page)
    chats: list[UnreadChat] = []
    try:
        rows = page.locator(selectors.CHAT_ROWS)
        for index in range(rows.count()):
            row = rows.nth(index)
            count = _badge_count(row)
            if count == 0 and not filtered:
                continue
            name = _text(row, selectors.ROW_TITLE)
            if name:
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


_YOU_SUFFIX = re.compile(r"\s*\(You\)\s*$", re.IGNORECASE)


def row_title_matches(row_title: str, target: str) -> bool:
    """Does this chat-list row denote `target`?

    The list renders the self-chat as "Name(You)" while the conversation header
    renders it as "Name". Tolerating that suffix here is safe because finding a
    row is only navigation -- `compose.verify_recipient` re-checks the opened
    chat against two independent signals before anything is sent, and refuses
    on any conflict. Widening the *finder* cannot widen what gets delivered.
    """
    if not row_title or not target:
        return False
    return row_title == target or _YOU_SUFFIX.sub("", row_title) == target


def find_row_by_name(page, name: str):
    """Locate a chat row by its title. Returns the locator, or None."""
    rows = page.locator(selectors.CHAT_ROWS)
    for index in range(rows.count()):
        row = rows.nth(index)
        if row_title_matches(_text(row, selectors.ROW_TITLE), name):
            return row
    return None


def open_chat(page, name: str) -> bool:
    """Open a chat by name. THIS MARKS IT READ and sends read receipts."""
    row = find_row_by_name(page, name)
    if row is None:
        return False
    try:
        row.click(timeout=5000)
        page.wait_for_timeout(3500)
        return page.locator(selectors.CONVERSATION).first.count() > 0
    except Exception:
        return False


def run_export(page, log=print) -> tuple[Path, list[ChatCapture]]:
    """Snapshot unreads, then open and capture each one. Writes as it goes."""
    from .config import load_config

    config = load_config()
    run_dir = new_run_dir(config)
    log(f"export dir: {run_dir}")

    unread = snapshot_unread(page)
    _write(run_dir / "unread_snapshot.json", [c.__dict__ for c in unread])
    log(f"snapshot saved: {len(unread)} chats, "
        f"{sum(c.unread_count for c in unread)} unread messages")

    captures: list[ChatCapture] = []
    for position, chat in enumerate(unread, start=1):
        log(f"[{position}/{len(unread)}] opening {chat.name!r} "
            f"({chat.unread_count} unread) — marks read, sends receipts")
        if not open_chat(page, chat.name):
            capture = ChatCapture(
                name=chat.name,
                expected_unread=chat.unread_count,
                error="could not open chat",
            )
        else:
            capture = capture_chat(page, chat.name, chat.unread_count)
        captures.append(capture)

        safe = "".join(ch if ch.isalnum() else "_" for ch in chat.name)[:60]
        _write(run_dir / f"chat_{position:02d}_{safe}.json", capture.as_dict())
        if capture.raw_html:
            _write(run_dir / f"chat_{position:02d}_{safe}.html", capture.raw_html)
        log(f"    captured {len(capture.messages)} messages"
            + (f" — {capture.error}" if capture.error else ""))

    _write(
        run_dir / "all_chats.json",
        {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "chats": [c.as_dict() for c in captures],
        },
    )
    return run_dir, captures
