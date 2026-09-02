"""What a posted message looks like when it is read back out of the DOM.

Every fake in this suite echoed back exactly what was posted, emoji included.
WhatsApp does not: it renders emoji as <img> elements, and `inner_text` does not
include their alt text. So a note posted as "📋 GROUP DIGEST 13:16" reads back
as "GROUP DIGEST 13:16", the delivery check never matched, and the daemon posted
the same digest on every tick -- ten duplicates in one afternoon, 2026-09-02,
with a green suite the whole time.

These tests use a real browser and real DOM so the lossy round trip is exercised
rather than imagined.
"""

import pytest

from wa_session.messages import extract_messages
from wa_session.tick import _fingerprint

pytestmark = pytest.mark.browser

SENT = "📋 GROUP DIGEST 13:16\n\nאקווה פמילי\n· Neighbour asks about a technician"


def row(pre: str, inner: str) -> str:
    """One message row, shaped like WhatsApp's."""
    return (
        f'<div role="row"><div data-id="ABC123">'
        f'<div data-pre-plain-text="{pre}"></div>'
        f'<span class="selectable-text">{inner}</span>'
        f"</div></div>"
    )


def emoji_as_image(text: str) -> str:
    """Render emoji the way WhatsApp does: an <img> carrying only alt text."""
    out = []
    for ch in text:
        if ord(ch) > 0x2100:
            out.append(f'<img alt="{ch}" src="data:image/gif;base64,R0lGODlhAQABAAAAACw=">')
        else:
            out.append(ch)
    return "".join(out)


@pytest.fixture
def render(chromium):
    context = chromium.new_context()

    def _render(body: str):
        page = context.new_page()
        page.route(
            "https://web.whatsapp.test/**",
            lambda route: route.fulfill(
                content_type="text/html; charset=utf-8",
                body=f'<html><head><meta charset="utf-8"></head>'
                     f"<body>{body}</body></html>",
            ),
        )
        page.goto("https://web.whatsapp.test/")
        return page

    yield _render
    context.close()


def rendered(page):
    return extract_messages(page)[0].text


def test_the_dom_really_does_swallow_emoji(render):
    """The premise. If this ever fails the bug could not have happened and the
    fingerprinting below is unnecessary."""
    body = '<div id="main">' + row("[13:16, 9/2/2026] Me: ",
                                   emoji_as_image(SENT.replace("\n", " "))) + "</div>"
    text = rendered(render(body))
    assert "GROUP DIGEST 13:16" in text
    assert "📋" not in text, "emoji unexpectedly survived; this test is now moot"


def test_a_note_is_still_recognised_after_the_round_trip(render):
    """The actual regression: raw-text matching failed here, and every digest
    was declared undelivered and posted again."""
    body = '<div id="main">' + row("[13:16, 9/2/2026] Me: ",
                                   emoji_as_image(SENT.replace("\n", " "))) + "</div>"
    text = rendered(render(body))
    assert _fingerprint(SENT)[:60] in _fingerprint(text)


def test_hebrew_and_digits_survive_the_round_trip(render):
    """The fingerprint must not be so lossy that it matches anything: the
    distinguishing content has to make it through."""
    body = '<div id="main">' + row("[13:16, 9/2/2026] Me: ",
                                   emoji_as_image(SENT.replace("\n", " "))) + "</div>"
    text = rendered(render(body))
    assert "אקווה" in text
    assert "13:16" in text


def test_a_different_note_is_not_mistaken_for_this_one(render):
    """Delivery of note A must never be inferred from note B being present."""
    other = "⚠️ I cannot draft a reply to Ann: Gmail has been unreachable"
    body = '<div id="main">' + row("[13:20, 9/2/2026] Me: ",
                                   emoji_as_image(other)) + "</div>"
    text = rendered(render(body))
    assert _fingerprint(SENT)[:60] not in _fingerprint(text)


def test_the_old_raw_text_match_would_have_failed_here(render):
    """Proof this test is not vacuous: the exact comparison that shipped on
    2026-09-02 -- the note's first line against the read-back -- does not
    match, which is why every digest was posted again."""
    body = '<div id="main">' + row("[13:16, 9/2/2026] Me: ",
                                   emoji_as_image(SENT.replace("\n", " "))) + "</div>"
    text = rendered(render(body))
    old_needle = " ".join(SENT.strip().splitlines()[0].split())[:40]
    assert old_needle not in " ".join(text.split()), "the old check would have worked"
    assert _fingerprint(SENT)[:60] in _fingerprint(text), "the new check must work"
