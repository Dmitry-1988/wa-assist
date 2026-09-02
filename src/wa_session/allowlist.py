"""Which chats the agent may draft replies for.

Starts empty and stays that way until you add a chat by hand. A chat that is
not listed is never drafted for and never sent to -- no heuristics, no
"probably fine", no model involvement in the decision.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

ALLOWLIST_FILENAME = "allowlist.json"


REPLY = "reply"
SUMMARIZE = "summarize"
MODES = (REPLY, SUMMARIZE)


@dataclass(frozen=True)
class Entry:
    """One allowlisted chat.

    `mode` is a capability, not a preference. A SUMMARIZE chat can be read and
    digested into your self-chat, but can never produce a sendable draft -- so
    adding a 40-person group for monitoring carries no risk of the agent one
    day replying into it.
    """

    name: str
    is_group: bool = False
    note: str = ""
    mode: str = REPLY

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {self.mode!r}")

    @property
    def can_reply(self) -> bool:
        return self.mode == REPLY

    def audience(self) -> str:
        return "GROUP — everyone sees your reply" if self.is_group else "1:1"


class Allowlist:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._entries: dict[str, Entry] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except ValueError:
            # A corrupt allowlist must not silently widen access: treat it as
            # empty, which blocks everything, rather than guessing.
            return
        for item in raw if isinstance(raw, list) else []:
            if isinstance(item, dict) and item.get("name"):
                mode = str(item.get("mode", REPLY))
                if mode not in MODES:
                    # An unrecognised mode must not fall back to the more
                    # capable one; skip the entry entirely.
                    continue
                entry = Entry(
                    name=item["name"],
                    is_group=bool(item.get("is_group", False)),
                    note=str(item.get("note", "")),
                    mode=mode,
                )
                self._entries[entry.name] = entry

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.write_text(
            json.dumps([asdict(e) for e in self._entries.values()],
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.path.chmod(0o600)

    def can_reply(self, chat_name: str) -> bool:
        """Only a REPLY-mode chat may ever be drafted for or sent to."""
        entry = self._entries.get(chat_name)
        return entry is not None and entry.can_reply

    def summarize_chats(self) -> list[Entry]:
        return [e for e in self.entries() if e.mode == SUMMARIZE]

    def allows(self, chat_name: str) -> bool:
        """Exact match only. No prefix, no fuzzy, no case folding.

        WhatsApp names are user-controlled: a contact can rename themselves to
        something that would fuzzy-match an allowlisted chat.
        """
        return chat_name in self._entries

    def get(self, chat_name: str) -> Entry | None:
        return self._entries.get(chat_name)

    def add(self, name: str, is_group: bool = False, note: str = "",
            mode: str = REPLY) -> Entry:
        entry = Entry(name=name, is_group=is_group, note=note, mode=mode)
        self._entries[name] = entry
        self.save()
        return entry

    def remove(self, name: str) -> bool:
        if name not in self._entries:
            return False
        del self._entries[name]
        self.save()
        return True

    def entries(self) -> list[Entry]:
        return sorted(self._entries.values(), key=lambda e: e.name)

    def __len__(self) -> int:
        return len(self._entries)


def filter_allowed(chats, allowlist: Allowlist):
    """Split chats into (allowed, skipped) by exact name match."""
    allowed, skipped = [], []
    for chat in chats:
        (allowed if allowlist.allows(chat.name) else skipped).append(chat)
    return allowed, skipped
