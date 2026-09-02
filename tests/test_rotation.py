"""Warnings must arrive while the session still works.

Once the session lapses the self-chat is gone, so a warning sent afterwards
reaches nobody. Everything here exists to make the notice land before that.
"""

from datetime import datetime, timedelta, timezone

import pytest

from wa_session.config import Config, load_config
from wa_session.rotation import (
    WARN_AT_HOURS,
    due_warning,
    hours_left,
    mark_announced,
    render_warning,
)
from wa_session.state import SessionState

LINKED = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def config(tmp_path) -> Config:
    (tmp_path / "p").mkdir()
    return Config(profile_dir=tmp_path / "p" / ".wa-profile",
                  state_dir=tmp_path / "p" / ".wa-state", rotate_after_hours=24.0)


@pytest.fixture
def state() -> SessionState:
    return SessionState(linked_at=LINKED)


def at(hours: float) -> datetime:
    return LINKED + timedelta(hours=hours)


def test_a_fresh_session_says_nothing(config, state):
    assert due_warning(config, state, at(1)) is None


def test_warning_fires_once_the_threshold_is_crossed(config, state):
    assert due_warning(config, state, at(18.5)) == 6.0


def test_each_threshold_is_announced_only_once(config, state):
    threshold = due_warning(config, state, at(18.5))
    mark_announced(config, state, threshold)
    assert due_warning(config, state, at(18.6)) is None


def test_a_late_daemon_says_the_urgent_thing_not_the_stale_one(config, state):
    """A daemon that was asleep all day must not walk the user up through
    every earlier warning one tick at a time."""
    assert due_warning(config, state, at(23.8)) == 0.5


def test_announcing_urgently_retires_the_gentler_warnings(config, state):
    mark_announced(config, state, 0.5)
    for hours in (18.5, 22.5, 23.9):
        assert due_warning(config, state, at(hours)) is None


def test_relinking_clears_the_history(config, state):
    mark_announced(config, state, 0.5)
    relinked = SessionState(linked_at=LINKED + timedelta(hours=24))
    assert due_warning(config, relinked, at(42)) == 6.0


def test_an_overdue_session_still_warns(config, state):
    assert due_warning(config, state, at(30)) == min(WARN_AT_HOURS)
    assert hours_left(state, 24.0, at(30)) == pytest.approx(-6.0)


def test_the_warning_says_what_to_run(config, state):
    text = render_warning(config, state, at(23.7))
    assert "cannot rotate it myself" in text
    # It must be explicit that this is the last chance to say anything.
    assert "cannot post here either" in text


def test_the_warning_names_reset_not_a_bare_login(config, state):
    """Every warning fires BEFORE the deadline, and before it a plain
    `wa-login` neither offers a QR nor moves the clock. Naming it would send
    the user to a command that does nothing at the moment they are told to
    run it."""
    text = render_warning(config, state, at(23.7))
    assert "uv run wa-login --reset" in text
    bare = [ln for ln in text.splitlines() if "wa-login" in ln]
    assert bare and all("--reset" in ln for ln in bare)


def test_warning_wording_switches_to_minutes_near_the_deadline(config, state):
    assert "minutes" in render_warning(config, state, at(23.75))
    assert "minutes" not in render_warning(config, state, at(18.5))


def test_enforcement_is_off_unless_asked_for():
    """The rotation clock is this project's policy, not WhatsApp's; enforcing
    it by default would stop a working agent."""
    assert load_config({}).enforce_rotation is False
    assert load_config({"WA_ENFORCE_ROTATION": "1"}).enforce_rotation is True
    assert load_config({"WA_ENFORCE_ROTATION": "true"}).enforce_rotation is True
    assert load_config({"WA_ENFORCE_ROTATION": "0"}).enforce_rotation is False
