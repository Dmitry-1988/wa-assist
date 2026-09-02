"""Best-effort unlink of this device through the WhatsApp Web UI.

Wiping the profile directory alone does NOT revoke the session: the linked
device stays registered on the account until it expires or is removed by hand,
and WhatsApp caps the number of linked devices. So rotation tries to log out
properly first, and says so loudly when it cannot.
"""

from __future__ import annotations

import re

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


def log_out(page, timeout_s: float = 30.0) -> bool:
    """Drive the WhatsApp Web logout flow. True if the device was unlinked.

    Returns False (never raises) when the UI has moved on and the selectors no
    longer match -- the caller wipes the profile either way and warns the user
    to remove the device from their phone.
    """
    state = detect(page)
    if state is PageState.AWAITING_QR:
        # Nothing linked in this profile; already logged out.
        return True
    if state is not PageState.LOGGED_IN:
        # Unknown state: the page may simply not have rendered. Never report a
        # successful unlink we cannot see, or the caller wipes the profile
        # believing the device was revoked when it was not.
        return False

    # An overlay would swallow the menu click and make this look like drift.
    dismiss(page)

    if not _click_first(page, selectors.MENU_BUTTONS):
        return False

    if not (_click_first(page, selectors.LOGOUT_ITEMS) or _click_by_text(page)):
        return False

    # A confirmation dialog usually follows; its button is also "Log out".
    _click_by_text(page, timeout_ms=2000)

    # Success is the QR screen coming back.
    deadline_ms = int(timeout_s * 1000)
    for selector in selectors.LOGGED_OUT:
        try:
            page.locator(selector).first.wait_for(
                state="visible", timeout=deadline_ms // len(selectors.LOGGED_OUT)
            )
            return True
        except Exception:
            continue
    return False
