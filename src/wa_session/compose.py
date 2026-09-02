"""Sending a WhatsApp message.

The failure that matters here is a MISDIRECTED SEND -- text delivered to the
wrong chat. It is irreversible and, in a group, public. So the recipient is
checked twice against two independent DOM signals (the conversation header and
the composer's own aria-label), re-checked after typing in case the chat
switched underneath us, and `dry_run` defaults to True so a send never happens
by omission.

Nothing here decides *who* to message: the caller passes an expected recipient
and this module refuses to proceed if the open chat is anyone else.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from . import selectors
from .interstitials import dismiss


class SendRefused(Exception):
    """Raised when a pre-send check fails. Never means 'maybe sent'."""


@dataclass(frozen=True)
class SendResult:
    ok: bool
    dry_run: bool
    recipient: str
    detail: str = ""


def header_recipient(page) -> str:
    try:
        node = page.locator(selectors.HEADER_TITLE).first
        if node.count():
            return (node.inner_text(timeout=2000) or "").strip()
    except Exception:
        pass
    return ""


def composer_recipient(page) -> str:
    """Recipient as the composer itself reports it, via its aria-label."""
    try:
        node = page.locator(selectors.COMPOSER).first
        if node.count():
            label = node.get_attribute("aria-label") or ""
            if label.startswith(selectors.COMPOSER_ARIA_PREFIX):
                return label[len(selectors.COMPOSER_ARIA_PREFIX):].strip()
    except Exception:
        pass
    return ""


def verify_recipient(page, expected: str) -> None:
    """Both signals must equal `expected` exactly, or nothing is sent."""
    if not expected:
        raise SendRefused("no expected recipient given")

    header = header_recipient(page)
    composer = composer_recipient(page)

    if not header and not composer:
        raise SendRefused("could not read the open chat's identity")
    # Each signal, when present, must match. A missing one is tolerated (the
    # markup changes); a CONFLICTING one never is.
    if header and header != expected:
        raise SendRefused(f"header says {header!r}, expected {expected!r}")
    if composer and composer != expected:
        raise SendRefused(f"composer says {composer!r}, expected {expected!r}")


def wait_for_chat_ready(page, expected: str = "", timeout_ms: int = 9000,
                        poll_ms: int = 100) -> str:
    """Block until a conversation pane is usable. Returns the header title.

    Replaces a flat sleep after clicking a chat row. The old code waited 2.5 to
    3.5 seconds whether the pane took 200ms or three, on every capture, draft
    and send -- and the browser work happens twice per productive tick, so it
    was paid twice.

    Waits for the header AND the composer, because every caller goes on to read
    one or both. Returns whatever it has at the deadline rather than raising:
    the callers already verify the recipient themselves, and that check is the
    one that must refuse.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    header = ""
    while True:
        header = header_recipient(page)
        if header and (not expected or header == expected):
            try:
                if page.locator(selectors.COMPOSER).first.count():
                    return header
            except Exception:
                pass
        if time.monotonic() >= deadline:
            return header
        page.wait_for_timeout(poll_ms)


def composer_text(page) -> str:
    try:
        node = page.locator(selectors.COMPOSER).first
        if node.count():
            return (node.inner_text(timeout=2000) or "").strip()
    except Exception:
        pass
    return ""


def clear_composer(page) -> bool:
    """Empty the composer, verifying it actually emptied.

    Leftover text is not cosmetic: a later stray Enter would send it. So this
    checks the result and escalates through fallbacks rather than assuming the
    first attempt worked.
    """
    box = page.locator(selectors.COMPOSER).first
    if not box.count():
        return True

    try:
        box.click(timeout=4000)
    except Exception:
        pass

    for attempt in ("keyboard", "fill"):
        try:
            if attempt == "keyboard":
                box.press("ControlOrMeta+A")
                box.press("Backspace")
            else:
                # Playwright drives contenteditable through the same input
                # events React listens to, so this survives a virtual DOM.
                box.fill("")
        except Exception:
            continue
        page.wait_for_timeout(350)
        if not composer_text(page):
            return True
    return not composer_text(page)


def _click_send(page) -> bool:
    for selector in selectors.SEND_BUTTON:
        try:
            node = page.locator(selector).first
            if node.count() and node.is_visible(timeout=1000):
                node.click(timeout=3000)
                return True
        except Exception:
            continue
    return False


def normalize_for_compare(text: str) -> str:
    """Content-preserving normalisation for the pre-send check.

    A contenteditable renders each newline as a doubled break in innerText, so
    a byte-exact match fails on formatting alone. Comparing non-empty, stripped
    lines still catches wrong, truncated, or injected content -- which is what
    the check is actually for -- without tripping over the DOM's line handling.
    """
    lines = [line.strip() for line in (text or "").split("\n")]
    return "\n".join(line for line in lines if line)


# The composer needs time to settle before it can be read back, and a long
# body needs more of it. Measured: 3000 characters are stable well inside 2s.
def _settle_ms(text: str) -> int:
    return min(600 + len(text) // 3, 3000)


# A line beginning "- ", "* " or "+ " makes the composer build a real list and
# emit its own "•" marker, which lands where the line break was: a body reading
# "…\n- Tomorrow" is read back as "…•- Tomorrow". Same length, so it is not a
# truncation -- the newline is simply gone, and the pre-send check refuses.
# Verified against the live site 2026-09-01, where it blocked a group digest on
# every tick. "·", "–" and no marker at all round-trip untouched.
_LIST_MARKER = re.compile(r"^([ \t]*)([-*+])(\s+)", re.MULTILINE)


def neutralize_list_markers(text: str) -> str:
    """Swap leading -, * and + bullets for "·", which the composer leaves alone.

    Applied before BOTH typing and the read-back comparison, so the check still
    compares like with like. This is cosmetic -- one bullet character for
    another -- and it is what keeps a perfectly good message from being stuck
    behind a refusal for ever.

    A marker must be followed by whitespace, so "-5 degrees", "---" and the
    "*bold*" syntax are left alone.
    """
    return _LIST_MARKER.sub(r"\1·\3", text)


def describe_mismatch(expected: str, got: str, context: int = 45) -> str:
    """Where the composer first diverged, with text either side.

    The old message showed the first 60 characters of each, which for a long
    body is almost always identical prose -- it says a check failed without
    saying what failed, and a real corruption five ticks running could not be
    diagnosed from the log at all.
    """
    want = normalize_for_compare(expected)
    have = normalize_for_compare(got)
    if not have:
        return f"composer is empty; expected {len(want)} characters"
    index = next(
        (i for i in range(min(len(want), len(have))) if want[i] != have[i]),
        min(len(want), len(have)),
    )
    start = max(0, index - context)
    return (
        f"composer diverges at character {index} of {len(want)} "
        f"(composer has {len(have)}): "
        f"expected {want[start:index + context]!r}, "
        f"got {have[start:index + context]!r}"
    )


def type_text(box, page, text: str) -> None:
    """Enter `text` into the composer in a single insertion.

    One `insert_text` for the whole body, newlines included -- NOT a line at a
    time with Shift+Enter between. Two reasons, both verified against the live
    site on 2026-09-01:

    * The composer auto-continues lists. Press Shift+Enter after a line
      starting "1." and WhatsApp inserts "2. " itself, so a body that already
      contains its own "2." arrives as "2. 2.". Worse, once a list is running
      every later line gets numbered too: "• bullet" became "4. • bullet" and a
      plain closing line became "6. end". A 3000-character group digest failed
      its pre-send check on this five ticks in a row.
    * `insert_text` dispatches no key events at all, so Enter-to-send cannot
      fire mid-body. Typing the text would send one message per line.

    Enter still sends -- that has not changed. This code simply never presses
    it; the send is a deliberate click on the send control afterwards.

    Per-character typing is also still out: a long draft at 15ms/char exceeded
    Playwright's 30s action timeout, so every long message failed to post.
    """
    box.click(timeout=5000)
    page.keyboard.insert_text(text)
    page.wait_for_timeout(_settle_ms(text))


def send_message(
    page,
    expected_recipient: str,
    text: str,
    dry_run: bool = True,
    on_before_send=None,
) -> SendResult:
    """Type `text` into the open chat and (unless dry_run) send it.

    `dry_run` defaults to True on purpose: a caller that forgets the argument
    gets a rehearsal, not a delivered message.
    """
    if not text.strip():
        raise SendRefused("refusing to send empty text")

    # Before anything else, so the text that is typed is the text that is
    # verified. Sanitising only one side would defeat the check.
    text = neutralize_list_markers(text)

    dismiss(page)
    verify_recipient(page, expected_recipient)

    box = page.locator(selectors.COMPOSER).first
    if not box.count():
        raise SendRefused("composer not found")

    clear_composer(page)
    try:
        type_text(box, page, text.strip())
    except Exception:
        # Leaving a half-typed body in the composer means the next attempt
        # appends to it, and a stray Enter would send the mess.
        clear_composer(page)
        raise
    page.wait_for_timeout(600)

    typed = composer_text(page)
    if normalize_for_compare(typed) != normalize_for_compare(text):
        clear_composer(page)
        raise SendRefused(describe_mismatch(text, typed))

    # Re-verify: the chat list can move under us while typing.
    try:
        verify_recipient(page, expected_recipient)
    except SendRefused:
        clear_composer(page)
        raise

    if dry_run:
        clear_composer(page)
        return SendResult(True, True, expected_recipient, "dry run — nothing sent")

    # Journal HERE, not before the checks above: every refusal so far happened
    # with nothing sent, and recording an attempt for them marked the draft
    # permanently "sent", so an approved reply vanished with no notice.
    if on_before_send is not None:
        on_before_send()

    if not _click_send(page):
        clear_composer(page)
        raise SendRefused("send control not found")

    # A slow contenteditable can still hold the text a moment after the click.
    # A single fixed wait reported delivered messages as failures, inviting a
    # manual resend -- a duplicate. Poll instead.
    leftover = composer_text(page)
    for _ in range(12):
        if not leftover:
            break
        page.wait_for_timeout(250)
        leftover = composer_text(page)
    if leftover:
        return SendResult(
            False, False, expected_recipient,
            f"composer still holds text after send: {leftover[:40]!r}",
        )
    return SendResult(True, False, expected_recipient, "sent")
