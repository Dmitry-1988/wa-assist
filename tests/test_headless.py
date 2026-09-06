"""The daemon must not put a window on the user's screen.

For months every browser step ran headed and was minimised through CDP,
because WhatsApp Web answered headless Chromium with "WhatsApp works with
Google Chrome 100+" and that was read as "it does not render headless".

It renders fine. Headless Chromium advertises `HeadlessChrome/...` and
WhatsApp gates on the User-Agent; an ordinary Chrome UA gets the real app, in
old and new headless alike. Verified against a live logged-in session on
2026-09-06: chat list, virtualised message list, emoji as <img>, both
recipient signals, typing and click-to-send.

The minimised window was never actually invisible either -- it is created
frontmost and minimised a moment later, so each launch stole focus. At a 120s
interval and up to two launches a tick that was reshuffling the user's macOS
Spaces about thirty times an hour.
"""

import pytest

from wa_session import session


@pytest.fixture
def launches(monkeypatch):
    """Record what would reach Playwright, without starting a browser."""
    seen = {}

    class FakeContext:
        pages = []

        def close(self):
            pass

    def fake_launch(playwright, profile_dir, headless, slow_mo):
        seen["headless"] = headless
        return FakeContext()

    monkeypatch.setattr(session, "_launch", fake_launch)
    monkeypatch.setattr(session, "minimize",
                        lambda ctx: seen.__setitem__("minimized", True))
    return seen


def _open(tmp_path, **kw):
    with session.persistent_context(tmp_path / "profile", playwright=object(), **kw):
        pass


# --- the UA is the whole fix ---------------------------------------------

def test_a_real_chrome_ua_is_sent():
    """Without this WhatsApp serves a browser-support notice, not the app."""
    assert "Chrome/" in session.CHROME_UA
    assert "Headless" not in session.CHROME_UA


def test_the_ua_reaches_playwright(monkeypatch, tmp_path):
    """It must be passed at launch; the gate is checked before any script runs."""
    captured = {}

    class FakePW:
        class chromium:
            @staticmethod
            def launch_persistent_context(**kw):
                captured.update(kw)
                return type("C", (), {"pages": [], "close": lambda self: None})()

    session._launch(FakePW, tmp_path, headless=True, slow_mo=0)
    assert captured["user_agent"] == session.CHROME_UA


def test_the_ua_is_sent_headed_too(tmp_path):
    """One UA everywhere: the session a login creates is the one the daemon
    reuses, so they must not look like two different browsers."""
    captured = {}

    class FakePW:
        class chromium:
            @staticmethod
            def launch_persistent_context(**kw):
                captured.update(kw)
                return type("C", (), {"pages": [], "close": lambda self: None})()

    session._launch(FakePW, tmp_path, headless=False, slow_mo=0)
    assert captured["user_agent"] == session.CHROME_UA


# --- quiet means invisible, not minimised ---------------------------------

def test_quiet_runs_headless_and_opens_no_window(launches, tmp_path, monkeypatch):
    monkeypatch.delenv("WA_HEADED", raising=False)
    _open(tmp_path, quiet=True)
    assert launches["headless"] is True
    assert "minimized" not in launches, "a quiet run must not create a window"


def test_a_visible_run_stays_visible(launches, tmp_path, monkeypatch):
    """`wa-agent --visible` and the login flow still want a real window."""
    monkeypatch.delenv("WA_HEADED", raising=False)
    _open(tmp_path, quiet=False)
    assert launches["headless"] is False
    assert "minimized" not in launches


# --- the escape hatch ------------------------------------------------------

def test_wa_headed_restores_the_minimised_window(launches, tmp_path, monkeypatch):
    """The UA gate is Meta's to change. When it does, this is the way back
    without editing code."""
    monkeypatch.setenv("WA_HEADED", "1")
    _open(tmp_path, quiet=True)
    assert launches["headless"] is False
    assert launches.get("minimized") is True


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_the_hatch_accepts_the_usual_spellings(value, monkeypatch):
    monkeypatch.setenv("WA_HEADED", value)
    assert session.forced_headed() is True


@pytest.mark.parametrize("value", ["", "0", "no", "off", "  "])
def test_anything_else_leaves_it_headless(value, monkeypatch):
    monkeypatch.setenv("WA_HEADED", value)
    assert session.forced_headed() is False


def test_an_explicit_headless_request_still_wins(launches, tmp_path, monkeypatch):
    """WA_HEADED is about not surprising the user with a window; it must not
    force one on a caller that asked for none."""
    monkeypatch.setenv("WA_HEADED", "1")
    _open(tmp_path, headless=True, quiet=True)
    assert launches["headless"] is True


# --- the daemon in particular ---------------------------------------------

def test_neither_tick_phase_asks_for_a_window():
    """Both daemon phases go through `quiet=True`; passing headless=False
    alongside it used to read as 'this must be headed' and is gone."""
    import inspect

    from wa_session import tick

    src = inspect.getsource(tick)
    assert "persistent_context(config.profile_dir, quiet=True)" in src
    assert "headless=False" not in src


def test_only_the_login_flow_ever_asks_for_a_window():
    """The daemon must never surprise the user with one, and neither should a
    read-only inspection command. `wa-login` is the sole exception: a QR you
    cannot see is not much use."""
    import inspect

    from wa_session import agent_cli, cli, tick

    for module in (tick, agent_cli):
        src = inspect.getsource(module)
        assert "headless=False" not in src, f"{module.__name__} asks for a window"

    src = inspect.getsource(cli)
    # Only the two login paths (_rotate's logout and _run's QR) stay headed.
    assert src.count("headless=False") == 2
