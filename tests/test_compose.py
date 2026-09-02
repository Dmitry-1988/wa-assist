"""Pre-send guards. A misdirected send is irreversible and, in a group, public,
so every one of these must refuse rather than proceed on doubt.
"""

import pytest

from wa_session.compose import (
    SendRefused,
    composer_recipient,
    header_recipient,
    send_message,
    verify_recipient,
)

pytestmark = pytest.mark.browser

BOB = "Bob"


def chat_html(header: str | None, composer_for: str | None) -> str:
    parts = []
    if header is not None:
        parts.append(
            f'<div data-testid="conversation-info-header-chat-title">{header}</div>'
        )
    if composer_for is not None:
        parts.append(
            '<div data-testid="conversation-compose-box-input" contenteditable="true"'
            f' role="textbox" aria-label="Type a message to {composer_for}"></div>'
        )
    return f'<div id="main">{"".join(parts)}</div>'


@pytest.fixture
def render(chromium):
    context = chromium.new_context()

    def _render(body: str):
        page = context.new_page()
        page.route(
            "https://web.whatsapp.test/**",
            lambda route: route.fulfill(
                content_type="text/html; charset=utf-8", body=f"<html><body>{body}</body></html>"
            ),
        )
        page.goto("https://web.whatsapp.test/")
        return page

    yield _render
    context.close()


def test_reads_both_recipient_signals(render):
    page = render(chat_html(BOB, BOB))
    assert header_recipient(page) == BOB
    assert composer_recipient(page) == BOB


def test_matching_signals_pass(render):
    verify_recipient(render(chat_html(BOB, BOB)), BOB)  # no raise


def test_header_conflict_refuses(render):
    page = render(chat_html("Alice", BOB))
    with pytest.raises(SendRefused, match="header"):
        verify_recipient(page, BOB)


def test_composer_conflict_refuses(render):
    # The dangerous case: header stale, composer already switched to Alice.
    page = render(chat_html(BOB, "Alice"))
    with pytest.raises(SendRefused, match="composer"):
        verify_recipient(page, BOB)


def test_unreadable_identity_refuses(render):
    page = render('<div id="main"></div>')
    with pytest.raises(SendRefused, match="identity"):
        verify_recipient(page, BOB)


def test_one_missing_signal_is_tolerated(render):
    verify_recipient(render(chat_html(BOB, None)), BOB)
    verify_recipient(render(chat_html(None, BOB)), BOB)


def test_empty_expected_recipient_refuses(render):
    with pytest.raises(SendRefused):
        verify_recipient(render(chat_html(BOB, BOB)), "")


def test_empty_text_refuses_before_touching_the_page(render):
    with pytest.raises(SendRefused, match="empty"):
        send_message(render(chat_html(BOB, BOB)), BOB, "   ")


def test_dry_run_types_then_clears_and_reports_not_sent(render):
    page = render(chat_html(BOB, BOB))
    result = send_message(page, BOB, "hello there", dry_run=True)
    assert result.ok and result.dry_run
    assert "nothing sent" in result.detail
    # Composer must be left clean so no draft lingers.
    assert page.locator('[data-testid="conversation-compose-box-input"]').inner_text().strip() == ""


def test_dry_run_is_the_default(render):
    # A caller who forgets the flag gets a rehearsal, never a delivery.
    assert send_message(render(chat_html(BOB, BOB)), BOB, "hi").dry_run is True


def test_wrong_chat_refuses_before_typing(render):
    page = render(chat_html("Alice", "Alice"))
    with pytest.raises(SendRefused):
        send_message(page, BOB, "hi", dry_run=False)
    assert page.locator('[data-testid="conversation-compose-box-input"]').inner_text().strip() == ""


def test_missing_send_control_refuses_and_clears(render):
    # Composer present, no send button in the DOM: must not silently succeed.
    page = render(chat_html(BOB, BOB))
    with pytest.raises(SendRefused, match="send control"):
        send_message(page, BOB, "hello", dry_run=False)
    assert page.locator('[data-testid="conversation-compose-box-input"]').inner_text().strip() == ""


def test_multiline_text_does_not_send_on_each_newline(render):
    """WhatsApp sends on Enter, so a multi-line draft must never produce one.

    This was once achieved with Shift+Enter between lines. It no longer is: the
    body goes in as a single `insert_text`, which dispatches NO key events at
    all. That is a strictly stronger guarantee than a modified Enter -- there is
    no keystroke to get the modifier wrong on -- and it is also the only way to
    avoid the composer auto-continuing lists, which corrupted every digest
    containing a numbered list (see `type_text`).
    """
    page = render(chat_html(BOB, BOB))
    sent_keys = []
    page.expose_function("_recordKey", lambda k: sent_keys.append(k))
    page.evaluate("""() => {
      document.querySelector('[data-testid="conversation-compose-box-input"]')
        .addEventListener('keydown', e => {
          if (e.key === 'Enter') window._recordKey(e.shiftKey ? 'shift-enter' : 'enter');
        });
    }""")
    send_message(page, BOB, "line one\nline two\nline three", dry_run=True)
    assert "enter" not in sent_keys, f"a bare Enter would have sent early: {sent_keys}"
    assert sent_keys == [], f"no Enter of any kind should be dispatched: {sent_keys}"


def test_a_numbered_list_reaches_the_composer_without_extra_markers(render):
    """The digest corruption, as close as a local page can get it: the body
    must arrive with exactly the markers it was given, none added."""
    body = "Digest\n1. first\n2. second\n3. third"
    page = render(chat_html(BOB, BOB))
    result = send_message(page, BOB, body, dry_run=True)
    assert result.ok and result.dry_run


def test_multiline_body_reaches_the_composer_intact(render):
    page = render(chat_html(BOB, BOB))
    result = send_message(page, BOB, "first\nsecond", dry_run=True)
    assert result.ok and result.dry_run


@pytest.mark.parametrize(
    ("a", "b", "equal"),
    [
        ("one\ntwo", "one\n\ntwo", True),        # contenteditable doubles breaks
        ("one\ntwo", "one\n  two  ", True),      # per-line padding
        ("one\ntwo", "one\nthree", False),       # different content still caught
        ("hello", "hello world", False),         # injected text still caught
        ("שלום\nעולם", "שלום\n\nעולם", True),    # RTL unaffected
    ],
)
def test_normalize_tolerates_formatting_not_content(a, b, equal):
    from wa_session.compose import normalize_for_compare

    assert (normalize_for_compare(a) == normalize_for_compare(b)) is equal
