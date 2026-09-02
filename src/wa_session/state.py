"""Tracks when the current WhatsApp session was established, for rotation."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Config, ensure_private_dir

STATE_VERSION = 1


@dataclass(frozen=True)
class SessionState:
    """When the QR was last scanned. `None` file on disk means "no session"."""

    linked_at: datetime

    def age(self, now: datetime | None = None) -> timedelta:
        return (now or _utcnow()) - self.linked_at

    def to_json(self) -> str:
        return json.dumps(
            {"version": STATE_VERSION, "linked_at": self.linked_at.isoformat()},
            indent=2,
        )

    @classmethod
    def from_json(cls, raw: str) -> SessionState:
        data = json.loads(raw)
        linked_at = datetime.fromisoformat(data["linked_at"])
        if linked_at.tzinfo is None:
            linked_at = linked_at.replace(tzinfo=timezone.utc)
        return cls(linked_at=linked_at)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def read_state(config: Config) -> SessionState | None:
    """Load the recorded session, or None if absent or unreadable.

    A corrupt state file is treated as "no session": that forces a rotation,
    which is the safe direction to fail in.
    """
    try:
        raw = config.state_file.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return None
    try:
        return SessionState.from_json(raw)
    except (ValueError, KeyError, TypeError):
        return None


def write_state(config: Config, linked_at: datetime | None = None) -> SessionState:
    """Record that a session was just established."""
    state = SessionState(linked_at=linked_at or _utcnow())
    ensure_private_dir(config.state_dir)
    config.state_file.write_text(state.to_json(), encoding="utf-8")
    config.state_file.chmod(0o600)
    return state


def clear_state(config: Config) -> None:
    config.state_file.unlink(missing_ok=True)


def is_expired(
    state: SessionState | None, rotate_after_hours: float, now: datetime | None = None
) -> bool:
    """True when the session is old enough that the rotation policy applies.

    No recorded session is not "expired" — there is nothing to rotate; the
    caller will simply prompt for a QR scan.
    """
    if state is None:
        return False
    return state.age(now) >= timedelta(hours=rotate_after_hours)


def wipe_profile(config: Config) -> bool:
    """Delete the Chromium profile directory. Returns True if it existed."""
    if not config.profile_dir.exists():
        return False
    shutil.rmtree(config.profile_dir)
    return True


def format_age(delta: timedelta) -> str:
    """Render a duration as `3d 4h`, `27h`, or `12m` for log output."""
    total_minutes = int(delta.total_seconds() // 60)
    if total_minutes < 60:
        return f"{max(total_minutes, 0)}m"
    hours, minutes = divmod(total_minutes, 60)
    if hours < 48:
        return f"{hours}h" if minutes == 0 else f"{hours}h {minutes}m"
    days, rem_hours = divmod(hours, 24)
    return f"{days}d" if rem_hours == 0 else f"{days}d {rem_hours}h"
