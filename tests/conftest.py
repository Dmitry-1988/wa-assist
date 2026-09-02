import json

import pytest

from wa_session.config import Config

# The account/calendar configuration the drafter refuses to run without. Real
# addresses live in the user's own gitignored .wa-agent/context.json; tests use
# placeholders so no personal data is committed.
EXAMPLE_CONTEXT = {
    "google_account": "you@example.com",
    "calendars": [
        "you@example.com",
        "family0000000000000000000@group.calendar.google.com",
        "en.usa#holiday@group.v.calendar.google.com",
    ],
    "never_use": ["old-account@example.com"],
    "notes": ["Query EVERY calendar id above."],
}


def write_context(config: Config, data: dict | None = None) -> None:
    """Give `config` a usable context.json."""
    path = config.profile_dir.parent / ".wa-agent" / "context.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(EXAMPLE_CONTEXT if data is None else data,
                               ensure_ascii=False, indent=2), encoding="utf-8")


def pytest_addoption(parser):
    parser.addoption(
        "--headed",
        action="store_true",
        default=False,
        help="run browser tests in a visible window instead of headless",
    )
    parser.addoption(
        "--slowmo",
        action="store",
        type=int,
        default=0,
        metavar="MS",
        help="delay each Playwright operation by MS milliseconds (implies --headed)",
    )


@pytest.fixture
def config(tmp_path) -> Config:
    return Config(
        profile_dir=tmp_path / "profile",
        state_dir=tmp_path / "state",
        rotate_after_hours=24.0,
    )


@pytest.fixture(scope="session")
def launch_kwargs(request) -> dict:
    """How browsers are launched for this run, from --headed / --slowmo."""
    slow_mo = request.config.getoption("--slowmo")
    headed = request.config.getoption("--headed") or slow_mo > 0
    return {"headless": not headed, "slow_mo": slow_mo}


@pytest.fixture(scope="session")
def playwright():
    """One Playwright instance for the suite.

    The sync API refuses to start a second instance on the same thread, so
    every browser test shares this one. No Playwright mocking anywhere.
    """
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as pw:
        yield pw


@pytest.fixture(scope="session")
def chromium(playwright, launch_kwargs):
    """A real Chromium, or skip if the browser binary is not installed."""
    try:
        browser = playwright.chromium.launch(**launch_kwargs)
    except Exception as exc:
        pytest.skip(f"Chromium unavailable: {exc}")
    yield browser
    browser.close()
