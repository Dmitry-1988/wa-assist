"""Best-effort unlink of this device through the WhatsApp Web UI.

Wiping the profile directory alone does NOT revoke the session: the linked
device stays registered on the account until it expires or is removed by hand,
and WhatsApp caps the number of linked devices. So rotation tries to log out
properly first, and says so loudly when it cannot.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from . import selectors
from .interstitials import dismiss
from .page_state import PageState, detect

_LOGOUT_RE = re.compile(selectors.LOGOUT_TEXT, re.IGNORECASE)


def _click_first(page, candidates: tuple[str, ...], timeout_ms: int = 3000) -> bool:
    for selector in candidates:
        try:
            locator = page.locator(selector).first
            locator.click(timeout=timeout_ms)
            return True
        except Exception:
            continue
    return False


def _click_by_text(page, timeout_ms: int = 3000) -> bool:
    """Click a 'Log out' control found by its accessible name or text."""
    getters = (
        lambda: page.get_by_role("button", name=_LOGOUT_RE).first,
        lambda: page.get_by_role("menuitem", name=_LOGOUT_RE).first,
        lambda: page.get_by_text(_LOGOUT_RE).first,
    )
    for get in getters:
        try:
            get().click(timeout=timeout_ms)
            return True
        except Exception:
            continue
    return False


@dataclass(frozen=True)
class LogoutResult:
    """Whether the device was unlinked, and where the attempt stopped if not.

    Truthy when confirmed, so `if log_out(page):` still reads naturally. The
    step matters because the two kinds of failure are not equally serious:
    stopping at `menu` or `logout item` means the click never happened and the
    device is certainly still linked, while stopping at `confirmation` means
    it was clicked and only the proof is missing.
    """

    ok: bool
    step: str = ""

    def __bool__(self) -> bool:
        return self.ok

    @property
    def clicked(self) -> bool:
        """Did we actually press Log out before giving up?"""
        return self.ok or self.step == "confirmation"


def _wait_for_qr(page, timeout_s: float, poll_s: float = 0.5) -> bool:
    """Poll until the page really is showing a QR.

    The old version gave each of the four LOGGED_OUT selectors a single
    `wait_for`, budgeted as timeout/4. Logging out reloads the page, and a
    `wait_for` that spans a navigation raises instead of waiting -- so all four
    could fail in quick succession, long before the 30s budget was spent, and
    report a failed unlink that had in fact succeeded. Seen 2026-09-21: the
    user was told to remove a stale device and found none to remove, because
    the logout had worked.

    `detect` is the right primitive and was already proven against the live
    site -- it is what spots the QR during login. It gives each selector 250ms
    and treats an error as a miss, so sweeping it repeatedly tries every
    candidate dozens of times across the same budget.

    Note this waits for AWAITING_QR specifically, not merely for a known
    state: `wait_for_state` returns the instant the page resolves, and the
    chat list is still up for a moment after the click, so it would answer
    LOGGED_IN and look like failure.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            if detect(page) is PageState.AWAITING_QR:
                return True
        except Exception:
            pass          # detached node mid-reload is a miss, not an answer
        if time.monotonic() >= deadline:
            return False
        try:
            page.wait_for_timeout(int(poll_s * 1000))
        except Exception:
            time.sleep(poll_s)


def log_out(page, timeout_s: float = 30.0) -> LogoutResult:
    """Drive the WhatsApp Web logout flow.

    Never raises. A falsy result means the unlink could not be CONFIRMED,
    which is not the same as knowing it failed -- see `LogoutResult.clicked`.
    The caller wipes the profile either way.
    """
    state = detect(page)
    if state is PageState.AWAITING_QR:
        # Nothing linked in this profile; already logged out.
        return LogoutResult(True)
    if state is not PageState.LOGGED_IN:
        # Unknown state: the page may simply not have rendered. Never report a
        # successful unlink we cannot see, or the caller wipes the profile
        # believing the device was revoked when it was not.
        return LogoutResult(False, "page state")

    # An overlay would swallow the menu click and make this look like drift.
    dismiss(page)

    if not _click_first(page, selectors.MENU_BUTTONS):
        return LogoutResult(False, "menu")

    if not (_click_first(page, selectors.LOGOUT_ITEMS) or _click_by_text(page)):
        return LogoutResult(False, "logout item")

    # A confirmation dialog usually follows; its button is also "Log out".
    _click_by_text(page, timeout_ms=2000)

    if _wait_for_qr(page, timeout_s):
        return LogoutResult(True)
    return LogoutResult(False, "confirmation")
