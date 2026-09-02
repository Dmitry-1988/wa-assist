import multiprocessing as mp

import pytest

from wa_session.lock import Busy, profile_lock


def _hold(path, started, release):
    with profile_lock(path):
        started.set()
        release.wait(timeout=10)


def test_second_holder_is_refused(tmp_path):
    lock = tmp_path / "lock"
    ctx = mp.get_context("spawn")
    started, release = ctx.Event(), ctx.Event()
    proc = ctx.Process(target=_hold, args=(lock, started, release))
    proc.start()
    try:
        assert started.wait(timeout=10), "helper never acquired the lock"
        with pytest.raises(Busy):
            with profile_lock(lock):
                pass
    finally:
        release.set()
        proc.join(timeout=10)


def test_lock_is_released_after_the_block(tmp_path):
    lock = tmp_path / "lock"
    with profile_lock(lock):
        pass
    with profile_lock(lock):   # must be reacquirable
        pass


def test_lock_released_even_on_exception(tmp_path):
    lock = tmp_path / "lock"
    with pytest.raises(RuntimeError):
        with profile_lock(lock):
            raise RuntimeError("boom")
    with profile_lock(lock):
        pass


def test_lock_file_is_owner_only(tmp_path):
    lock = tmp_path / "nested" / "lock"
    with profile_lock(lock):
        assert lock.stat().st_mode & 0o777 == 0o600
