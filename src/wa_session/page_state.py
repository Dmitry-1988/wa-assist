"""Read-only detection of whether WhatsApp Web is showing a QR or a chat list.

This inspects only structural markers (does the QR container exist? does the
chat pane exist?). It never reads message content.
"""

from __future__ import annotations

import time
from enum import Enum

from . import selectors


class PageState(str, Enum):
    LOGGED_IN = "logged_in"
    AWAITING_QR = "awaiting_qr"
    UNKNOWN = "unknown"


def _any_visible(page, candidates: tuple[str, ...]) -> bool:
    for selector in candidates:
        try:
            if page.locator(selector).first.is_visible(timeout=250):
                return True
        except Exception:
            # A selector that errors (detached node, navigation mid-check) is
            # just a miss; try the next candidate.
            continue
    return False


def detect(page) -> PageState:
    """Classify the page as it stands right now, without waiting."""
    if _any_visible(page, selectors.LOGGED_IN):
        return PageState.LOGGED_IN
    if _any_visible(page, selectors.LOGGED_OUT):
        return PageState.AWAITING_QR
    return PageState.UNKNOWN


def wait_for_state(page, timeout_s: float, poll_s: float = 1.0) -> PageState:
    """Poll until the page resolves to a known state, or the timeout elapses."""
    deadline = time.monotonic() + timeout_s
    while True:
        state = detect(page)
        if state is not PageState.UNKNOWN:
            return state
        if time.monotonic() >= deadline:
            return PageState.UNKNOWN
        time.sleep(poll_s)


def wait_for_login(page, timeout_s: float, poll_s: float = 2.0) -> bool:
    """Block until the chat pane appears (the user finished scanning)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if detect(page) is PageState.LOGGED_IN:
            return True
        try:
            if page.is_closed():
                return False
        except Exception:
            return False
        time.sleep(poll_s)
    return False
