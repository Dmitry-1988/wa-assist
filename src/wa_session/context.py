"""Whose mail and calendars the drafter may read.

These values are the USER'S, not the program's. They lived as constants in
drafter.py and, separately, as a `.wa-agent/context.json` that nothing read --
the same three calendar ids written down twice, one copy authoritative by
accident. This module makes the file the single source and keeps real addresses
out of the repository.

Missing configuration is a hard failure, not a default. A drafter with no
calendars still has working Gmail tools and would answer availability questions
from thin air, which is the exact mistake the whole project is built to avoid.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config

FILENAME = "context.json"
EXAMPLE = "context.example.json"


class ContextError(Exception):
    """The account/calendar configuration is missing or unusable."""


@dataclass(frozen=True)
class Context:
    """The account to read, and every calendar that must be consulted."""

    google_account: str
    calendars: list[str] = field(default_factory=list)
    never_use: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def context_path(config: Config) -> Path:
    return config.profile_dir.parent / ".wa-agent" / FILENAME


def _calendar_ids(raw) -> list[str]:
    """Accept ["id", ...] or [{"id": ..., "name": ...}, ...]."""
    ids: list[str] = []
    for entry in raw or []:
        if isinstance(entry, str) and entry.strip():
            ids.append(entry.strip())
        elif isinstance(entry, dict):
            ident = entry.get("id")
            if isinstance(ident, str) and ident.strip():
                ids.append(ident.strip())
    return ids


def _strings(raw) -> list[str]:
    if isinstance(raw, str):
        return [raw]
    return [s for s in (raw or []) if isinstance(s, str) and s.strip()]


def load_context(config: Config) -> Context:
    """Read the account configuration. Raises ContextError if unusable."""
    path = context_path(config)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ContextError(
            f"no {FILENAME} at {path} — copy {EXAMPLE} there and fill it in"
        ) from None
    except (OSError, ValueError) as exc:
        raise ContextError(f"{path} is unreadable: {exc}") from None
    if not isinstance(data, dict):
        raise ContextError(f"{path} must contain a JSON object")

    account = data.get("google_account")
    if not isinstance(account, str) or not account.strip():
        raise ContextError(f'{path} needs a non-empty "google_account"')

    calendars = _calendar_ids(data.get("calendars"))
    if not calendars:
        # Availability answered from no calendar is a guess wearing a fact's
        # clothes -- the failure this project exists to prevent.
        raise ContextError(f'{path} lists no calendars under "calendars"')

    return Context(
        google_account=account.strip(),
        calendars=calendars,
        never_use=_strings(data.get("never_use")),
        notes=_strings(data.get("notes")) or _strings(data.get("note")),
    )
