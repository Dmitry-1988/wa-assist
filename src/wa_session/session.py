"""Launching the persistent Chromium context."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import os

from playwright.sync_api import BrowserContext, Playwright, sync_playwright

from .config import ensure_private_dir

# Chromium's own automation banner and the default UA advertise a controlled
# browser; WhatsApp Web is happier without the banner.
LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]

VIEWPORT = {"width": 1280, "height": 900}

# THE reason this project spent months opening real windows.
#
# Headless Chromium advertises itself as "HeadlessChrome/..." and WhatsApp Web
# answers that with a browser-support notice -- "WhatsApp works with Google
# Chrome 100+" -- instead of the app. The page was read as "does not render
# headless", and every browser step was made headed and then minimised through
# CDP to keep it off the user's screen.
#
# It was never a rendering failure, only a User-Agent gate. Sending an ordinary
# Chrome UA renders the QR page, the chat list, the virtualised message list
# and the composer, in old and new headless alike -- verified against a live
# logged-in session on 2026-09-06, including typing and click-to-send.
#
# This is a sniff on Meta's side, so it can change. `WA_HEADED=1` restores the
# old headed-and-minimised behaviour without a code change.
CHROME_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
             "AppleWebKit/537.36 (KHTML, like Gecko) "
             "Chrome/140.0.0.0 Safari/537.36")


def forced_headed(env=None) -> bool:
    """WA_HEADED=1 pins every launch to a real window. The escape hatch for
    the day WhatsApp changes what it accepts."""
    env = os.environ if env is None else env
    return (env.get("WA_HEADED") or "").strip().lower() in {"1", "true", "yes", "on"}


def _launch(playwright: Playwright, profile_dir: Path, headless: bool, slow_mo: int):
    return playwright.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir),
        headless=headless,
        slow_mo=slow_mo,
        args=LAUNCH_ARGS,
        viewport=VIEWPORT,
        # Sent headed as well as headless: one UA everywhere means the session
        # a login creates is the session the daemon later reuses.
        user_agent=CHROME_UA,
    )


def minimize(context) -> bool:
    """Send the browser window to the Dock, without hiding it from the page.

    Only reached under WA_HEADED=1 now that `quiet` runs headless. Kept because
    it is the least-bad fallback if WhatsApp stops accepting `CHROME_UA`:
    macOS clamps --window-position, so an off-screen window snaps back, and
    minimising through CDP at least keeps the page rendered (document.hidden
    stays false) while leaving most of the screen alone.

    It is not invisible, though. The window is created frontmost and minimised
    a moment later, so every launch still takes focus -- which is why headless
    is the default and this is the fallback rather than the design.
    """
    try:
        page = context.pages[0] if context.pages else context.new_page()
        cdp = context.new_cdp_session(page)
        window_id = cdp.send("Browser.getWindowForTarget")["windowId"]
        cdp.send(
            "Browser.setWindowBounds",
            {"windowId": window_id, "bounds": {"windowState": "minimized"}},
        )
        return True
    except Exception:
        # Not fatal: a visible window still works, it is just intrusive.
        return False


@contextmanager
def persistent_context(
    profile_dir: Path,
    headless: bool = False,
    playwright: Playwright | None = None,
    slow_mo: int = 0,
    quiet: bool = False,
):
    """Yield a Chromium context whose cookies and storage live in `profile_dir`.

    `quiet=True` means "do not appear on the user's screen", and it is now
    satisfied by running headless -- see `CHROME_UA`. Before that it meant a
    real window minimised through CDP, which still stole focus on every launch:
    the daemon opens the browser up to twice a tick, so a 120s interval was
    reshuffling the user's Spaces around thirty times an hour.

    `launch_persistent_context` is what makes the login survive: unlike
    `launch()` + `new_context()`, it writes cookies, localStorage and IndexedDB
    into a real user-data directory on disk.

    Pass `playwright` to reuse a running instance -- the sync API refuses to
    start a second one on the same thread. `slow_mo` delays each operation by
    that many milliseconds, which makes a run watchable when debugging
    selectors against the live site.
    """
    ensure_private_dir(profile_dir)
    # A caller asking for quiet gets no window at all, unless the user has
    # pinned everything headed. `headless=True` from the caller still wins.
    headless = headless or (quiet and not forced_headed())

    if playwright is not None:
        context = _launch(playwright, profile_dir, headless, slow_mo)
        if quiet and not headless:
            minimize(context)
        try:
            yield context
        finally:
            _close_quietly(context)
        return

    with sync_playwright() as pw:
        context = _launch(pw, profile_dir, headless, slow_mo)
        if quiet and not headless:
            minimize(context)
        try:
            yield context
        finally:
            _close_quietly(context)


def first_page(context: BrowserContext):
    """The tab Chromium opens with, or a new one if it has none."""
    return context.pages[0] if context.pages else context.new_page()


def wait_until_closed(context: BrowserContext, poll_ms: int = 500) -> None:
    """Block until the user closes the browser window.

    Chromium flushes the profile to disk on a clean shutdown, so returning only
    once the window is gone is what keeps the session reusable.
    """
    while True:
        pages = context.pages
        if not pages:
            return
        try:
            # Sleeping through a page also pumps the driver connection, so a
            # window closed by the user is noticed on the next iteration.
            pages[0].wait_for_timeout(poll_ms)
        except Exception:
            # Page or context torn down: the window is closed.
            return


def _close_quietly(context: BrowserContext) -> None:
    try:
        context.close()
    except Exception:
        pass
