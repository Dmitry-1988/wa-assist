"""A single-holder lock around the browser profile.

Chromium refuses to open a user-data-dir that another process already holds,
and a daemon tick firing while the interactive session is mid-command would do
exactly that. Whoever loses the race skips its turn rather than crashing.
"""

from __future__ import annotations

import errno
import fcntl
import os
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path


class Busy(Exception):
    """Another process holds the profile lock."""


_CONTENDED = (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK)


def holder(path: Path) -> str:
    """The pid recorded in a lock file, for a message worth reading."""
    try:
        return path.read_text(encoding="utf-8").strip() or "unknown"
    except OSError:
        return "unknown"


@contextmanager
def profile_lock(
    path: Path,
    blocking: bool = False,
    timeout_s: float = 0.0,
    poll_s: float = 1.0,
    on_wait: Callable[[str], None] | None = None,
):
    """Hold an exclusive lock for the duration of the block.

    A daemon tick takes the profile for tens of seconds at a time, so failing
    the instant it is busy is the wrong answer for an interactive command:
    `timeout_s` retries until the holder finishes. `on_wait` is called once,
    with the holding pid, if the first attempt fails -- a command that is about
    to sit there for half a minute should say why.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
    deadline = time.monotonic() + max(timeout_s, 0.0)
    announced = False
    while True:
        try:
            fcntl.flock(handle, flags)
            break
        except OSError as exc:
            if exc.errno not in _CONTENDED:
                os.close(handle)
                raise
            if time.monotonic() >= deadline:
                os.close(handle)
                raise Busy(f"profile is in use (lock: {path})") from exc
            if on_wait is not None and not announced:
                announced = True
                on_wait(holder(path))
            time.sleep(poll_s)
    try:
        os.ftruncate(handle, 0)
        os.write(handle, f"{os.getpid()}\n".encode())
        yield
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            os.close(handle)
