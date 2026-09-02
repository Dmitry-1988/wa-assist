"""Giving notice before the session rotation deadline.

The daemon genuinely cannot rotate itself: linking a device needs a QR scanned
from the phone, and no amount of code changes that. What it *can* do is speak
while it still has a working session -- because the moment the session is gone,
the self-chat goes with it and WhatsApp stops being a channel at all.

That asymmetry sets the whole design here:

  * every warning fires BEFORE the deadline, through the self-chat, which is
    where the user already is;
  * after the deadline there is nothing left to say through WhatsApp, so the
    fallback is a macOS notification -- throttled, because a 300s daemon would
    otherwise post one twelve times an hour.

Warnings are recorded against `linked_at`, so a fresh scan silently clears the
whole history rather than leaving stale thresholds marked as announced.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Config
from .state import SessionState

# Hours of remaining life at which to speak up. Each fires at most once per
# session. 6h is "plan for it", 2h is "do it today", 0.5h is "do it now".
WARN_AT_HOURS = (6.0, 2.0, 0.5)

# A logged-out daemon notifies the desktop instead, but only this often.
DESKTOP_THROTTLE_S = 3600.0


def notices_path(config: Config) -> Path:
    return config.profile_dir.parent / ".wa-agent" / "rotation.json"


def _load(config: Config) -> dict:
    try:
        data = json.loads(notices_path(config).read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save(config: Config, data: dict) -> None:
    path = notices_path(config)
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        path.chmod(0o600)
    except OSError:
        # Losing the bookkeeping means a warning may repeat. That is strictly
        # better than a tick dying over it.
        pass


def _for_session(config: Config, state: SessionState) -> dict:
    """Bookkeeping for the CURRENT session; a relink starts from scratch."""
    data = _load(config)
    stamp = state.linked_at.isoformat()
    if data.get("linked_at") != stamp:
        # A new session clears the ANNOUNCED thresholds, but not the desktop
        # throttle: replacing the whole dict dropped it, so a logout notice
        # could repeat every tick right after a rotation.
        fresh = {"linked_at": stamp, "announced": []}
        if "desktop" in data:
            fresh["desktop"] = data["desktop"]
        return fresh
    return data


def hours_left(
    state: SessionState, rotate_after_hours: float, now: datetime | None = None
) -> float:
    """Hours until the rotation policy calls this session too old. May be < 0."""
    deadline = state.linked_at + timedelta(hours=rotate_after_hours)
    now = now or datetime.now(timezone.utc)
    return (deadline - now).total_seconds() / 3600.0


def due_warning(
    config: Config, state: SessionState, now: datetime | None = None
) -> float | None:
    """The threshold to announce now, or None if nothing new has been crossed.

    Returns the MOST URGENT crossed threshold, not the first: a daemon that has
    been down all day should say "30 minutes left", not walk the user up
    through every earlier warning three ticks in a row.
    """
    left = hours_left(state, config.rotate_after_hours, now)
    crossed = [t for t in WARN_AT_HOURS if left <= t]
    if not crossed:
        return None
    target = min(crossed)
    if str(target) in set(_for_session(config, state).get("announced", [])):
        return None
    return target


def mark_announced(config: Config, state: SessionState, threshold: float) -> None:
    """Record `threshold` and every less urgent one as said."""
    data = _for_session(config, state)
    announced = set(data.get("announced", []))
    announced.update(str(t) for t in WARN_AT_HOURS if t >= threshold)
    data["announced"] = sorted(announced)
    _save(config, data)


def render_warning(
    config: Config, state: SessionState, now: datetime | None = None
) -> str:
    """The self-chat text. Says what to run, because that is the whole point."""
    left = hours_left(state, config.rotate_after_hours, now)
    deadline = state.linked_at + timedelta(hours=config.rotate_after_hours)
    local = deadline.astimezone().strftime("%H:%M")
    if left <= 0:
        when = "The WhatsApp session is past its rotation deadline"
    elif left < 1:
        when = f"The WhatsApp session rotates in {int(left * 60)} minutes (at {local})"
    else:
        when = f"The WhatsApp session rotates in {left:.0f}h (at {local})"
    # --reset, not a bare `wa-login`: before the deadline a plain login is a
    # no-op on the clock (it will not even offer a QR), so naming it here would
    # send the user to a command that does nothing at exactly the moment this
    # warning is delivered.
    return (
        f"🔑 {when}.\n\n"
        "I cannot rotate it myself — it needs a QR scanned from your phone. "
        "Run this at a terminal, then scan:\n\n"
        "    uv run wa-login --reset\n\n"
        "Until you do, drafts and sends stop. This is the last channel I have "
        "to tell you: once the session lapses I cannot post here either."
    )


def notify_desktop(config: Config, key: str, title: str, message: str,
                   now: datetime | None = None) -> bool:
    """macOS notification, throttled per `key`. True if one was posted.

    The only route left once WhatsApp is unreachable. launchd runs this job in
    the Aqua session, so osascript can reach the notification centre.
    """
    now = now or datetime.now(timezone.utc)
    data = _load(config)
    sent = data.get("desktop") or {}
    last = sent.get(key)
    if last:
        try:
            if (now - datetime.fromisoformat(last)).total_seconds() < DESKTOP_THROTTLE_S:
                return False
        except ValueError:
            pass
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification {json.dumps(message)} with title {json.dumps(title)}'],
            capture_output=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    sent[key] = now.isoformat()
    data["desktop"] = sent
    _save(config, data)
    return True
