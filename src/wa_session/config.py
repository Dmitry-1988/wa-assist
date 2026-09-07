"""Where session data lives on disk, and the rotation policy."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

WHATSAPP_URL = "https://web.whatsapp.com"

DEFAULT_PROFILE_DIRNAME = ".wa-profile"
DEFAULT_STATE_DIRNAME = ".wa-state"
# 14 days. This was 24h, which was never WhatsApp's number -- WhatsApp expires
# a linked device on INACTIVITY (about 14 days of the phone being offline), not
# on a fixed lifetime, so a session left alone keeps working indefinitely.
#
# 24h was a defence-in-depth guess made while the drafter could still reach the
# filesystem: a prompt-injected run could write into src/wa_session/, which the
# daemon imports and executes, and from there copy the profile out. That path
# is closed -- the drafter has no filesystem and no shell at all -- and what
# rotation still buys is bounding the useful life of a copy taken by someone
# with brief access to an unlocked machine. Daily QR scans were a heavy price
# for that alone, and an ignored policy protects nothing: the warnings were
# simply becoming noise.
DEFAULT_ROTATE_AFTER_HOURS = 336.0

# The profile is a live credential; keep it owner-only.
PRIVATE_DIR_MODE = 0o700

# Directories whose whole point is to copy their contents to someone else's
# computer. A WhatsApp profile inside one is a live credential being uploaded,
# continuously, and nothing about the file mode or FileVault stops it.
SYNCED_ROOTS = (
    "Library/Mobile Documents",      # iCloud Drive
    "Library/CloudStorage",          # Google Drive, OneDrive, Dropbox, Box
    "Dropbox",
    "OneDrive",
    "Google Drive",
    "My Drive",
    "Yandex.Disk",
    "pCloud Drive",
)


class SyncedProfile(Exception):
    """The profile directory is inside a cloud-sync folder."""


def assert_not_synced(path: Path) -> None:
    """Refuse a profile directory that a sync client would upload.

    There is no legitimate reason to keep this in iCloud or Dropbox, and the
    failure is silent: everything works, and a copy of your WhatsApp session
    sits on someone else's servers. Rotation does not help -- the sync client
    uploads each new one too.
    """
    resolved = path.expanduser().resolve()
    parts = resolved.parts
    for root in SYNCED_ROOTS:
        needle = tuple(Path(root).parts)
        if any(parts[i:i + len(needle)] == needle
               for i in range(len(parts) - len(needle) + 1)):
            raise SyncedProfile(
                f"{resolved} is inside {root!r}, which syncs to the cloud. "
                "The WhatsApp profile is a live credential and must not leave "
                "this machine. Move the checkout, or set WA_PROFILE_DIR to a "
                "path outside any sync folder."
            )


@dataclass(frozen=True)
class Config:
    """Resolved paths and policy for one run."""

    profile_dir: Path
    state_dir: Path
    rotate_after_hours: float
    # Whether the DAEMON refuses to act on an over-age session. Off by default:
    # the rotation clock is this project's own policy, not WhatsApp's, and
    # WhatsApp Web keeps working well past it -- so enforcing it silently stops
    # a working agent. Warnings fire either way; this decides what happens when
    # they are ignored.
    enforce_rotation: bool = False

    @property
    def state_file(self) -> Path:
        return self.state_dir / "session.json"


def project_root() -> Path:
    """Repository root, derived from this file's location (src/wa_session/)."""
    return Path(__file__).resolve().parents[2]


def _read_hours(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than 0, got {value}")
    return value


def _read_flag(env: Mapping[str, str], name: str) -> bool:
    return (env.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Build a Config from `env` (defaults to the process environment).

    WA_PROFILE_DIR         override the Chromium user-data directory
    WA_STATE_DIR           override where the rotation timestamp is kept
    WA_ROTATE_AFTER_HOURS  override the 24h rotation policy
    WA_ENFORCE_ROTATION    1/true: the daemon stops once the session is over-age
    """
    env = os.environ if env is None else env
    root = project_root()
    profile = env.get("WA_PROFILE_DIR") or str(root / DEFAULT_PROFILE_DIRNAME)
    state = env.get("WA_STATE_DIR") or str(root / DEFAULT_STATE_DIRNAME)
    return Config(
        profile_dir=Path(profile).expanduser(),
        state_dir=Path(state).expanduser(),
        rotate_after_hours=_read_hours(
            env, "WA_ROTATE_AFTER_HOURS", DEFAULT_ROTATE_AFTER_HOURS
        ),
        enforce_rotation=_read_flag(env, "WA_ENFORCE_ROTATION"),
    )


def ensure_private_dir(path: Path) -> Path:
    """Create `path` (and parents) owner-only, tightening it if it already exists."""
    # Checked here because every browser launch goes through it, login included
    # -- there is no path that creates a profile without passing this point.
    assert_not_synced(path)
    path.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    # mkdir's mode is ignored when the directory already exists, and umask can
    # loosen it on creation, so set it explicitly either way.
    path.chmod(PRIVATE_DIR_MODE)
    return path
