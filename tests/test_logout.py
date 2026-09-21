"""log_out must never claim an unlink it could not observe.

Reporting success on a page it cannot read would make the caller wipe the
profile believing the device was revoked, leaving a live linked device behind.
"""

import pytest

from wa_session.logout import log_out

pytestmark = pytest.mark.browser

QR_HTML = '<div data-ref="AAAA1111" style="height:200px"><canvas aria-label="Scan me!"></canvas></div>'
UNRENDERED_HTML = '<div id="app"></div>'


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


def test_qr_screen_counts_as_already_logged_out(render):
    assert log_out(render(QR_HTML))


def test_unrendered_page_is_not_reported_as_unlinked(render):
    # The headless/blank case: no QR, no chat list. Must not return True.
    assert not log_out(render(UNRENDERED_HTML))


def test_logged_in_page_without_a_menu_fails_loudly(render):
    # Chat pane present but the menu selectors miss: report failure, not success.
    assert not log_out(render('<div id="pane-side" style="height:200px">chats</div>'))


# --- where it stopped, and whether Log out was actually pressed -----------

CHATS = '<div id="pane-side" style="height:200px">chats</div>'
MENU = '<div aria-label="Menu" onclick="document.getElementById(\'m\').style.display=\'block\'">menu</div>'


def _menu_with_logout(on_click: str) -> str:
    return (CHATS + MENU + '<div id="m" style="display:none">'
            f'<div role="menuitem" aria-label="Log out" onclick="{on_click}">'
            'Log out</div></div>')


def test_a_qr_that_arrives_late_is_still_confirmed(render):
    """The QR does not appear the instant Log out is pressed. The old code
    gave each of four selectors ONE `wait_for`; this polls the whole set."""
    # No nested quotes: HTML attribute values need none when they contain no
    # spaces. Escaping them through Python -> HTML attribute -> JS string is
    # how the first version of this test silently became a JS syntax error,
    # so the onclick never ran and log_out was blamed for it.
    page = render(_menu_with_logout(
        "setTimeout(function(){document.body.innerHTML="
        "'<div data-ref=AAAA1111 style=height:200px></div>';}, 1200)"))
    result = log_out(page, timeout_s=15)
    assert result, f"stopped at {result.step!r}"
    assert result.step == ""


def test_a_press_with_no_qr_reports_confirmation(render):
    """Log out WAS clicked and the QR never came. Distinct from not clicking:
    the unlink has probably happened and only the proof is missing."""
    result = log_out(render(_menu_with_logout("void 0")), timeout_s=1)
    assert not result
    assert result.step == "confirmation"
    assert result.clicked is True


def test_an_unreadable_page_reports_page_state(render):
    result = log_out(render(UNRENDERED_HTML))
    assert result.step == "page state"
    assert result.clicked is False


def test_a_missing_menu_reports_menu(render):
    result = log_out(render(CHATS))
    assert result.step == "menu"
    assert result.clicked is False, "nothing was pressed; the device is still linked"


def test_a_menu_without_a_logout_item_reports_it(render):
    result = log_out(render(CHATS + MENU), timeout_s=1)
    assert result.step == "logout item"
    assert result.clicked is False
