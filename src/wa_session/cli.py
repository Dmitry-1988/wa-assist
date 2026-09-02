"""`wa-login` -- open WhatsApp Web on a persistent profile, rotating daily."""

from __future__ import annotations

import argparse
import os
import sys

from .agent import agent_dir
from .config import WHATSAPP_URL, Config, load_config
from .lock import Busy, profile_lock
from .logout import log_out
from .page_state import PageState, wait_for_login, wait_for_state
from .digest import render
from .export import run_export
from .interstitials import dismiss
from .session import first_page, persistent_context, wait_until_closed
from .unread import extract_unread
from .state import (
    clear_state,
    format_age,
    is_expired,
    read_state,
    wipe_profile,
    write_state,
)

# How long to leave the window open waiting for a human to scan the QR.
QR_SCAN_TIMEOUT_S = 300.0

# WhatsApp Web is a heavy SPA: nothing useful is in the DOM at DOMContentLoaded.
PAGE_READY_TIMEOUT_S = 30.0

# The launchd job's label. Yours may differ; only the message below uses it.
DAEMON_LABEL = os.environ.get("WA_DAEMON_LABEL", "com.example.wa-agent")

# How long to wait out a daemon tick before giving up on the profile. Measured
# over 193 ticks: median 22s, p90 34s, worst 166s -- so this covers the worst
# case with room, and a longer wait means something is actually stuck.
LOCK_WAIT_S = 240.0


def log(message: str) -> None:
    print(f"  {message}", flush=True)


# WhatsApp's login screen checkbox; unchecked, the session dies with the tab.
STAY_LOGGED_IN = "#auto-logout-toggle"


def _warn_if_not_staying_logged_in(page) -> None:
    """Warn if 'Stay logged in on this browser' is unticked.

    It ships ticked, so this only fires if it was turned off -- but with it
    off nothing persists and every run would demand a fresh scan.
    """
    try:
        checkbox = page.locator(STAY_LOGGED_IN).first
        if checkbox.count() and not checkbox.is_checked(timeout=1000):
            log("NOTE: 'Stay logged in on this browser' is unticked — tick it,")
            log("      or the session will not survive this run.")
    except Exception:
        pass


def _has_profile(config: Config) -> bool:
    return config.profile_dir.is_dir() and any(config.profile_dir.iterdir())


def _rotate(config: Config) -> None:
    """Unlink this device through the UI, then delete the profile."""
    if not _has_profile(config):
        # Nothing linked from this machine; skip the pointless network trip.
        log("no profile to unlink; clearing session record")
        wipe_profile(config)
        clear_state(config)
        return

    log("logging out via WhatsApp Web UI...")
    unlinked = False
    try:
        # Headed: WhatsApp Web does not render under headless Chromium, and a
        # blank page would make the unlink look successful when it did nothing.
        with persistent_context(config.profile_dir, headless=False) as context:
            page = first_page(context)
            page.goto(WHATSAPP_URL, wait_until="domcontentloaded")
            wait_for_state(page, PAGE_READY_TIMEOUT_S)
            unlinked = log_out(page)
    except Exception as exc:
        log(f"logout attempt failed: {exc}")

    if unlinked:
        log("device unlinked; wiping profile")
    else:
        log("could not confirm logout through the UI; wiping profile anyway")
        log("ACTION NEEDED: on your phone, open WhatsApp -> Linked Devices and")
        log("remove any stale entry for this computer.")

    wipe_profile(config)
    clear_state(config)


def _run(config: Config) -> int:
    state = read_state(config)

    if state is not None:
        age = format_age(state.age())
        if is_expired(state, config.rotate_after_hours):
            log(f"profile age: {age} — exceeds {config.rotate_after_hours:g}h rotation policy")
            _rotate(config)
        else:
            # Deliberately does NOT restart the clock: a rotation policy that
            # renews whenever you glance at the session is not a policy. Say so,
            # or an early login looks like it rotated and the deadline arrives
            # anyway.
            log(f"profile age: {age} — within policy")
            log("this does NOT reset the rotation clock; the deadline is unchanged")
            log("to rotate now, quit and run: uv run wa-login --reset")

    with persistent_context(config.profile_dir, headless=False) as context:
        page = first_page(context)
        page.goto(WHATSAPP_URL, wait_until="domcontentloaded")

        match wait_for_state(page, PAGE_READY_TIMEOUT_S):
            case PageState.LOGGED_IN:
                log("logged in — session restored")
                dismiss(page)
                if read_state(config) is None:
                    # Profile predates this tool, or state was lost. Start the
                    # rotation clock now rather than rotating blindly.
                    write_state(config)
            case PageState.AWAITING_QR:
                log("scan the QR code in the browser window")
                _warn_if_not_staying_logged_in(page)
                if wait_for_login(page, QR_SCAN_TIMEOUT_S):
                    write_state(config)
                    log("linked — session saved")
                else:
                    log("no login detected before timeout; nothing was saved")
            case _:
                log("page state unknown (WhatsApp Web markup may have changed)")

        log("leave this window open; close it when you are done")
        wait_until_closed(context)

    return 0


def _reset(config: Config) -> int:
    log("forcing rotation now")
    _rotate(config)
    return _run(config)


def probe_live_state(config: Config, lock_wait_s: float = 45.0) -> str:
    """What WhatsApp Web actually shows for this profile right now.

    The recorded timestamp only knows when a QR was last scanned. It cannot
    know that WhatsApp has since dropped the link -- which is exactly what
    happened on 2026-09-01: the record read "linked 2h 51m ago — valid" while
    the page was sitting on a QR screen and every tick was blocked. A status
    command that can only report its own bookkeeping is not a status command.

    Returns "logged_in", "awaiting_qr", "unknown", "busy", or "error: ...".
    """
    try:
        with profile_lock(agent_dir(config) / "profile.lock",
                          timeout_s=lock_wait_s):
            # Headed but minimised: WhatsApp Web does not render headless, and
            # a blank page would read as "not logged in" and be a lie of its own.
            with persistent_context(config.profile_dir, headless=False,
                                    quiet=True) as context:
                page = first_page(context)
                page.goto(WHATSAPP_URL, wait_until="domcontentloaded")
                return wait_for_state(page, PAGE_READY_TIMEOUT_S).value
    except Busy:
        return "busy"
    except Exception as exc:
        return f"error: {type(exc).__name__}: {exc}"


def _status(config: Config, live: bool = True) -> int:
    log(f"profile: {config.profile_dir}")
    log(f"state:   {config.state_file}")
    log(f"policy:  rotate after {config.rotate_after_hours:g}h")
    state = read_state(config)
    recorded_ok = False
    if state is None:
        log("session: none recorded (next run prompts for a QR scan)")
    else:
        expired = is_expired(state, config.rotate_after_hours)
        recorded_ok = not expired
        verdict = "EXPIRED" if expired else "valid"
        log(f"session: linked {format_age(state.age())} ago — {verdict} (recorded)")

    if not live:
        log("live:    not checked (--quick)")
        return 0

    log("live:    checking WhatsApp Web itself…")
    actual = probe_live_state(config)

    if actual == "logged_in":
        log("live:    LOGGED IN — the recorded session is real")
        return 0
    if actual == "awaiting_qr":
        log("live:    NOT LOGGED IN — WhatsApp Web is showing a QR")
        if recorded_ok:
            # The failure mode this whole check exists for.
            log("         the recorded session above is STALE: WhatsApp has")
            log("         dropped this link even though the clock has not run out.")
        log("         run `uv run wa-login` and scan to restore it")
        return 1
    if actual == "busy":
        log("live:    not checked — the daemon is using the profile; try again")
        return 0
    log(f"live:    {actual}")
    return 1


def unread_main(argv: list[str] | None = None) -> int:
    """`wa-unread` -- digest of chats with unread messages."""
    parser = argparse.ArgumentParser(
        prog="wa-unread",
        description="Summarise chats with unread messages. Never opens a chat, "
        "so no read receipts are sent.",
    )
    parser.add_argument("--limit", type=int, default=50, help="max chats to read")
    parser.add_argument(
        "--keep-open", action="store_true", help="leave the browser window open"
    )
    args = parser.parse_args(argv)

    try:
        config = load_config()
    except ValueError as exc:
        print(f"wa-unread: {exc}", file=sys.stderr)
        return 2

    with persistent_context(config.profile_dir, headless=False) as context:
        page = first_page(context)
        page.goto(WHATSAPP_URL, wait_until="domcontentloaded")
        state = wait_for_state(page, PAGE_READY_TIMEOUT_S)
        if state is not PageState.LOGGED_IN:
            log("not logged in — run `uv run wa-login` first")
            return 1
        chats = extract_unread(page, limit=args.limit)
        print()
        print(render(chats))
        if args.keep_open:
            log("leave this window open; close it when you are done")
            wait_until_closed(context)
    return 0


def read_main(argv: list[str] | None = None) -> int:
    """`wa-read` -- open every unread chat and capture its messages.

    Destructive on purpose: opening a chat marks it read and sends read
    receipts to the sender. Requires --yes so it cannot happen by accident.
    """
    parser = argparse.ArgumentParser(
        prog="wa-read",
        description="Open unread chats and export their messages. THIS MARKS "
        "THEM READ and sends read receipts to your contacts.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="required: confirms you accept read receipts being sent",
    )
    args = parser.parse_args(argv)

    if not args.yes:
        print(
            "wa-read: this opens every unread chat, which marks it read and\n"
            "         sends read receipts to those contacts. It cannot be undone,\n"
            "         and the unread state is gone afterwards.\n"
            "         Re-run with --yes if that is what you want.",
            file=sys.stderr,
        )
        return 2

    try:
        config = load_config()
    except ValueError as exc:
        print(f"wa-read: {exc}", file=sys.stderr)
        return 2

    with persistent_context(config.profile_dir, headless=False) as context:
        page = first_page(context)
        page.goto(WHATSAPP_URL, wait_until="domcontentloaded")
        if wait_for_state(page, PAGE_READY_TIMEOUT_S) is not PageState.LOGGED_IN:
            log("not logged in — run `uv run wa-login` first")
            return 1
        dismiss(page)
        run_dir, captures = run_export(page, log=log)
        total = sum(len(c.messages) for c in captures)
        log(f"done: {len(captures)} chats, {total} messages -> {run_dir}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wa-login",
        description="Open WhatsApp Web on a persistent Chromium profile.",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="unlink and wipe the profile now, then start a fresh login",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="print session state, checking WhatsApp Web itself, then exit",
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="with --status: read the local record only, do not open a browser",
    )
    return parser


def _announce_wait(pid: str) -> None:
    log(f"the daemon is using the profile (pid {pid}); waiting for it to finish…")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config()
    except ValueError as exc:
        print(f"wa-login: {exc}", file=sys.stderr)
        return 2

    # Reads a timestamp; touches no browser. Must work while a tick runs.
    if args.status:
        return _status(config, live=not args.quick)

    # One lock for the WHOLE command, not per step. Chromium corrupts a
    # user-data-dir opened twice, and rotation calls rmtree on that directory --
    # which a tick may be driving at the time. Taking it once also means the
    # lock is still held while the login window sits open, so ticks skip their
    # turn instead of racing the window the user is looking at.
    try:
        with profile_lock(agent_dir(config) / "profile.lock",
                          timeout_s=LOCK_WAIT_S, on_wait=_announce_wait):
            return _reset(config) if args.reset else _run(config)
    except Busy:
        log("the daemon still holds the profile after "
            f"{LOCK_WAIT_S:.0f}s — it may be stuck.")
        log("stop it and try again:")
        log(f"  launchctl bootout gui/$(id -u)/{DAEMON_LABEL}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
