"""Detection tests against a real Chromium rendering WhatsApp-shaped markup.

These drive the actual Playwright API rather than asserting on mock calls; the
only thing faked is the HTML, served from memory so the suite stays offline.
"""

import pytest

from wa_session.page_state import PageState, detect

pytestmark = pytest.mark.browser

LOGGED_IN_HTML = """
<div id="app">
  <div id="pane-side" style="height:200px">chat list container</div>
</div>
"""

QR_HTML = """
<div id="app">
  <div data-ref="AAAA1111" style="height:200px">
    <canvas aria-label="Scan me!" width="100" height="100"></canvas>
  </div>
</div>
"""

LOADING_HTML = '<div id="app"><div class="spinner">loading</div></div>'

HIDDEN_PANE_HTML = '<div id="pane-side" style="display:none">hidden</div>'


@pytest.fixture
def render(chromium):
    """Render a body fragment at a real https origin and hand back the page."""
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


def test_detects_logged_in(render):
    assert detect(render(LOGGED_IN_HTML)) is PageState.LOGGED_IN


def test_detects_qr_screen(render):
    assert detect(render(QR_HTML)) is PageState.AWAITING_QR


def test_unrecognised_markup_is_unknown(render):
    # A markup change must degrade to "unknown", never to a wrong verdict.
    assert detect(render(LOADING_HTML)) is PageState.UNKNOWN


def test_hidden_pane_does_not_count_as_logged_in(render):
    assert detect(render(HIDDEN_PANE_HTML)) is not PageState.LOGGED_IN


def test_logged_in_wins_over_stale_qr_node(render):
    # WhatsApp leaves the QR container in the DOM briefly after linking.
    page = render(f'<div data-ref="X" hidden></div>{LOGGED_IN_HTML}')
    assert detect(page) is PageState.LOGGED_IN
