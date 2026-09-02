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
                content_type="text/html", body=f"<html><body>{body}</body></html>"
            ),
        )
        page.goto("https://web.whatsapp.test/")
        return page

    yield _render
    context.close()


def test_qr_screen_counts_as_already_logged_out(render):
    assert log_out(render(QR_HTML)) is True


def test_unrendered_page_is_not_reported_as_unlinked(render):
    # The headless/blank case: no QR, no chat list. Must not return True.
    assert log_out(render(UNRENDERED_HTML)) is False


def test_logged_in_page_without_a_menu_fails_loudly(render):
    # Chat pane present but the menu selectors miss: report failure, not success.
    assert log_out(render('<div id="pane-side" style="height:200px">chats</div>')) is False
