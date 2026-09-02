from datetime import datetime, timedelta, timezone

import pytest

from wa_session.state import (
    SessionState,
    clear_state,
    format_age,
    is_expired,
    read_state,
    wipe_profile,
    write_state,
)

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)


def test_round_trip(config):
    written = write_state(config, linked_at=NOW)
    assert read_state(config) == written


def test_missing_state_reads_as_none(config):
    assert read_state(config) is None


def test_corrupt_state_reads_as_none(config):
    config.state_dir.mkdir(parents=True)
    config.state_file.write_text("{not json")
    assert read_state(config) is None


def test_naive_timestamp_is_treated_as_utc():
    state = SessionState.from_json('{"version": 1, "linked_at": "2026-08-28T12:00:00"}')
    assert state.linked_at == NOW


def test_state_file_is_owner_only(config):
    write_state(config)
    assert config.state_file.stat().st_mode & 0o777 == 0o600


def test_clear_state_is_idempotent(config):
    write_state(config)
    clear_state(config)
    clear_state(config)
    assert read_state(config) is None


@pytest.mark.parametrize(
    ("hours", "expected"),
    [(0, False), (23.9, False), (24, True), (48, True)],
)
def test_expiry_boundary(hours, expected):
    state = SessionState(linked_at=NOW - timedelta(hours=hours))
    assert is_expired(state, rotate_after_hours=24.0, now=NOW) is expected


def test_absent_session_is_not_expired():
    # Nothing to rotate; the caller just prompts for a scan.
    assert is_expired(None, rotate_after_hours=24.0) is False


def test_custom_policy_is_honoured():
    state = SessionState(linked_at=NOW - timedelta(hours=2))
    assert is_expired(state, rotate_after_hours=1.0, now=NOW) is True
    assert is_expired(state, rotate_after_hours=6.0, now=NOW) is False


def test_wipe_profile_removes_tree(config):
    (config.profile_dir / "Default").mkdir(parents=True)
    (config.profile_dir / "Default" / "Cookies").write_text("secret")
    assert wipe_profile(config) is True
    assert not config.profile_dir.exists()


def test_wipe_profile_on_missing_dir(config):
    assert wipe_profile(config) is False


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (timedelta(minutes=12), "12m"),
        (timedelta(hours=27), "27h"),
        (timedelta(hours=3, minutes=30), "3h 30m"),
        (timedelta(days=3, hours=4), "3d 4h"),
        (timedelta(days=2), "2d"),
    ],
)
def test_format_age(delta, expected):
    assert format_age(delta) == expected
