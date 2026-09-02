"""`wa-login` and the daemon must never hold the profile at the same time.

Chromium corrupts a user-data-dir opened by two processes, and the rotation
path calls rmtree on that directory -- so a tick running at the wrong moment is
not a slow command, it is a destroyed profile. lock.py always described this
case; until now only tick.py actually took the lock.
"""

import multiprocessing
import time

import pytest

from wa_session.agent import agent_dir
from wa_session.cli import LOCK_WAIT_S, main
from wa_session.config import Config
from wa_session.lock import Busy, holder, profile_lock


@pytest.fixture
def config(tmp_path) -> Config:
    (tmp_path / "p").mkdir()
    return Config(profile_dir=tmp_path / "p" / ".wa-profile",
                  state_dir=tmp_path / "p" / ".wa-state", rotate_after_hours=24.0)


# --- the lock primitive ----------------------------------------------------

def test_a_contended_lock_still_fails_fast_by_default(tmp_path):
    path = tmp_path / "profile.lock"
    with profile_lock(path):
        with pytest.raises(Busy):
            with profile_lock(path):
                pass


def test_a_timeout_waits_instead_of_failing_at_once(tmp_path):
    """The daemon holds the profile for tens of seconds; an interactive
    command should sit through that, not refuse."""
    path = tmp_path / "profile.lock"
    started = time.monotonic()
    with profile_lock(path):
        with pytest.raises(Busy):
            with profile_lock(path, timeout_s=0.4, poll_s=0.1):
                pass
    assert time.monotonic() - started >= 0.4


def test_the_waiting_message_fires_once_and_names_the_holder(tmp_path):
    path = tmp_path / "profile.lock"
    seen: list[str] = []
    with profile_lock(path):
        recorded = holder(path)
        with pytest.raises(Busy):
            with profile_lock(path, timeout_s=0.3, poll_s=0.05,
                              on_wait=seen.append):
                pass
    assert seen == [recorded]


def test_the_lock_is_released_for_the_next_holder(tmp_path):
    path = tmp_path / "profile.lock"
    with profile_lock(path):
        pass
    with profile_lock(path, timeout_s=0.1):
        pass


def test_the_holder_pid_is_not_left_with_stale_trailing_bytes(tmp_path):
    path = tmp_path / "profile.lock"
    path.write_text("9999999999\n")
    with profile_lock(path):
        assert holder(path) == str(__import__("os").getpid())


# --- wa-login actually taking it ------------------------------------------

def _hold(path, seconds, ready):
    with profile_lock(path):
        ready.set()
        time.sleep(seconds)


def test_login_refuses_rather_than_racing_a_stuck_daemon(config, monkeypatch):
    """The failure must be a clean refusal with instructions, never a second
    Chromium on the same profile."""
    monkeypatch.setattr("wa_session.cli.LOCK_WAIT_S", 0.3)

    ran = []
    monkeypatch.setattr("wa_session.cli._run", lambda c: ran.append("run") or 0)
    monkeypatch.setattr("wa_session.cli.load_config", lambda: config)

    ctx = multiprocessing.get_context("fork")
    ready = ctx.Event()
    lock_path = agent_dir(config) / "profile.lock"
    other = ctx.Process(target=_hold, args=(lock_path, 3.0, ready))
    other.start()
    try:
        assert ready.wait(5)
        assert main([]) == 1
        assert ran == [], "must not touch the profile while it is held"
    finally:
        other.terminate()
        other.join()


def test_quick_status_works_while_the_daemon_holds_the_profile(config, monkeypatch):
    """--status --quick reads only the local record, so a running tick can
    never block it. Plain --status now opens the page to check reality (a
    recorded session can be stale), and therefore does wait for the profile --
    the "busy" branch is covered in test_status_live.py."""
    monkeypatch.setattr("wa_session.cli.load_config", lambda: config)

    def explode(cfg, **kw):
        raise AssertionError("--quick must never open a browser")
    monkeypatch.setattr("wa_session.cli.probe_live_state", explode)

    ctx = multiprocessing.get_context("fork")
    ready = ctx.Event()
    other = ctx.Process(target=_hold,
                        args=(agent_dir(config) / "profile.lock", 3.0, ready))
    other.start()
    try:
        assert ready.wait(5)
        assert main(["--status", "--quick"]) == 0
    finally:
        other.terminate()
        other.join()


def test_live_status_takes_the_same_lock_as_everything_else(config, monkeypatch):
    """Two Chromiums on one user-data-dir corrupt it -- a status check is no
    exception, so it goes through the profile lock like every other browser
    step."""
    monkeypatch.setattr("wa_session.cli.load_config", lambda: config)
    from wa_session.cli import probe_live_state

    ctx = multiprocessing.get_context("fork")
    ready = ctx.Event()
    other = ctx.Process(target=_hold,
                        args=(agent_dir(config) / "profile.lock", 3.0, ready))
    other.start()
    try:
        assert ready.wait(5)
        # Zero wait: the lock is held, so it must report busy rather than
        # launching a second browser on the same profile.
        assert probe_live_state(config, lock_wait_s=0) == "busy"
    finally:
        other.terminate()
        other.join()


def test_login_and_reset_share_one_lock_for_the_whole_command(config, monkeypatch):
    """Locking per step would leave a gap between the rmtree and the rescan
    for a tick to slip into."""
    monkeypatch.setattr("wa_session.cli.load_config", lambda: config)
    held: list[bool] = []

    def observe(cfg):
        try:
            with profile_lock(agent_dir(cfg) / "profile.lock"):
                held.append(False)
        except Busy:
            held.append(True)
        return 0

    monkeypatch.setattr("wa_session.cli._reset", observe)
    assert main(["--reset"]) == 0
    assert held == [True], "the command must already hold the profile lock"


def test_the_wait_covers_the_worst_observed_tick():
    """Measured over 193 real ticks: worst 166s. A shorter wait would give up
    on a daemon that was about to finish."""
    assert LOCK_WAIT_S >= 166
