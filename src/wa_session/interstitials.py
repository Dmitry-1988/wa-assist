"""Dismissing WhatsApp Web's occasional overlays.

WhatsApp shows a "What's new" dialog after updates. It is a role=dialog that
intercepts clicks, so it can silently break both state detection and the logout
flow -- and a logout that silently fails would make rotation wipe the profile
while reporting a successful unlink. Best-effort, never fatal.
"""

from __future__ import annotations

from . import selectors


def _click_if_visible(page, selector: str, timeout_ms: int = 1000) -> bool:
    try:
        locator = page.locator(selector).first
        if locator.count() and locator.is_visible(timeout=timeout_ms):
            locator.click(timeout=timeout_ms * 2)
            return True
    except Exception:
        pass
    return False


def present(page) -> bool:
    """True when a blocking overlay is on screen."""
    for selector in selectors.INTERSTITIAL_PRESENT:
        try:
            if page.locator(selector).first.is_visible(timeout=250):
                return True
        except Exception:
            continue
    return False


def dismiss(page, rounds: int = 3) -> int:
    """Close any overlays that are in the way. Returns how many were dismissed.

    Runs a few rounds because dismissing one can reveal another.
    """
    dismissed = 0
    for _ in range(rounds):
        if not present(page):
            break
        if not any(_click_if_visible(page, s) for s in selectors.INTERSTITIAL_DISMISS):
            break
        dismissed += 1
        page.wait_for_timeout(750)

    # The notifications strip is not a dialog but still covers list rows.
    for selector in selectors.BUTTERBAR_DISMISS:
        if _click_if_visible(page, selector):
            dismissed += 1
    return dismissed
