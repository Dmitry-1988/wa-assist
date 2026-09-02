"""Launching the persistent Chromium context."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from playwright.sync_api import BrowserContext, Playwright, sync_playwright

from .config import ensure_private_dir

# Chromium's own automation banner and the default UA advertise a controlled
# browser; WhatsApp Web is happier without the banner.
LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]

VIEWPORT = {"width": 1280, "height": 900}


def _launch(playwright: Playwright, profile_dir: Path, headless: bool, slow_mo: int):
    return playwright.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir),
        headless=headless,
        slow_mo=slow_mo,
        args=LAUNCH_ARGS,
        viewport=VIEWPORT,
    )


def minimize(context) -> bool:
    """Send the browser window to the Dock, without hiding it from the page.

    WhatsApp Web refuses to render under headless Chromium -- verified on both
    a fresh and a logged-in profile: the document stays empty. And macOS clamps
    --window-position, so an off-screen window snaps back on screen. Minimising
    through CDP is the one approach that keeps the page fully rendered
    (document.hidden stays false) while leaving the user's screen alone.
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

    `launch_persistent_context` is what makes the login survive: unlike
    `launch()` + `new_context()`, it writes cookies, localStorage and IndexedDB
    into a real user-data directory on disk.

    Pass `playwright` to reuse a running instance -- the sync API refuses to
    start a second one on the same thread. `slow_mo` delays each operation by
    that many milliseconds, which makes a run watchable when debugging
    selectors against the live site.
    """
    ensure_private_dir(profile_dir)
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
