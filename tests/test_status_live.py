"""`--status` must report reality, not just its own bookkeeping.

On 2026-09-01 the record said "linked 2h 51m ago — valid" while WhatsApp Web
was sitting on a QR screen and every tick was blocked. The recorded timestamp
knows when a QR was last scanned; it cannot know that WhatsApp has since
dropped the link. Nothing in the rotation machinery notices either, because by
its reckoning the session still had 21 hours left.
"""

from datetime import datetime, timedelta, timezone

import pytest

from wa_session.cli import _status
from wa_session.config import Config
from wa_session.state import write_state


@pytest.fixture
def config(tmp_path) -> Config:
    (tmp_path / "p").mkdir()
    return Config(profile_dir=tmp_path / "p" / ".wa-profile",
                  state_dir=tmp_path / "p" / ".wa-state", rotate_after_hours=24.0)


@pytest.fixture
def fresh(config):
    write_state(config, datetime.now(timezone.utc) - timedelta(hours=2))
    return config


def probe(monkeypatch, value):
    monkeypatch.setattr("wa_session.cli.probe_live_state", lambda cfg, **kw: value)


def test_a_live_session_is_confirmed_as_real(fresh, monkeypatch, capsys):
    probe(monkeypatch, "logged_in")
    assert _status(fresh) == 0
    assert "LOGGED IN" in capsys.readouterr().out


def test_a_dropped_link_is_reported_even_though_the_clock_is_fine(
    fresh, monkeypatch, capsys
):
    """The exact failure: record valid, WhatsApp showing a QR."""
    probe(monkeypatch, "awaiting_qr")
    assert _status(fresh) == 1
    out = capsys.readouterr().out
    assert "NOT LOGGED IN" in out
    assert "STALE" in out
    assert "uv run wa-login" in out


def test_a_dropped_link_exits_nonzero_so_it_can_be_scripted(fresh, monkeypatch):
    probe(monkeypatch, "awaiting_qr")
    assert _status(fresh) == 1


def test_an_expired_record_is_not_also_called_stale(config, monkeypatch, capsys):
    """If the clock HAS run out, a QR is expected -- not a contradiction."""
    write_state(config, datetime.now(timezone.utc) - timedelta(hours=30))
    probe(monkeypatch, "awaiting_qr")
    _status(config)
    out = capsys.readouterr().out
    assert "NOT LOGGED IN" in out
    assert "STALE" not in out


def test_the_record_is_labelled_as_a_record(fresh, monkeypatch, capsys):
    """So 'valid' can never again be read as 'verified'."""
    probe(monkeypatch, "logged_in")
    _status(fresh)
    assert "(recorded)" in capsys.readouterr().out


def test_a_busy_profile_is_reported_not_guessed(fresh, monkeypatch, capsys):
    """The daemon holds the profile ~9% of the time; that is not a logout."""
    probe(monkeypatch, "busy")
    assert _status(fresh) == 0
    out = capsys.readouterr().out
    assert "not checked" in out
    assert "NOT LOGGED IN" not in out


def test_a_probe_error_is_surfaced_not_swallowed(fresh, monkeypatch, capsys):
    probe(monkeypatch, "error: TimeoutError: boom")
    assert _status(fresh) == 1
    assert "TimeoutError" in capsys.readouterr().out


def test_quick_skips_the_browser_entirely(fresh, monkeypatch, capsys):
    def explode(cfg, **kw):
        raise AssertionError("--quick must not open a browser")
    monkeypatch.setattr("wa_session.cli.probe_live_state", explode)
    assert _status(fresh, live=False) == 0
    assert "not checked (--quick)" in capsys.readouterr().out


def test_no_recorded_session_still_checks_reality(config, monkeypatch, capsys):
    probe(monkeypatch, "logged_in")
    _status(config)
    out = capsys.readouterr().out
    assert "none recorded" in out
    assert "LOGGED IN" in out
