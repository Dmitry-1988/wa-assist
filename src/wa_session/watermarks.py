"""What each group has already been told to the user, so nothing repeats.

A digest is only worth reading if it is new. This started as one watermark per
chat -- the msg_id of the last message that made it into a POSTED digest --
and that was not enough, twice over:

* `advance` overwrote the mark with the last message of whatever was captured.
  When a capture lost its tail (see `messages.read_window`), the "last" message
  was an OLD one, so the mark moved BACKWARDS and every later digest
  re-reported everything after it. Quiet groups kept turning up in digests
  with nothing new to say.
* Even with ordering fixed, one mark cannot answer "has this message been sent
  to the user before?" without trusting that the capture window and the mark
  are in the same order and neither has been disturbed.

So the record is now a SET of message ids per chat: every id that has appeared
in a posted digest. A message that has been reported once is never new again,
whatever the capture does, and the set only ever grows -- there is no way to
rewind it. Nothing is added until the digest is actually posted, because a
digest the user never saw has not told them anything.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config

FILENAME = "digest_seen.json"

# Ids kept per chat. Comfortably more than any capture reaches back, and it
# bounds a file that would otherwise grow for the life of the install.
MAX_SEEN = 1000

# How many already-reported messages to hand the summariser as context. It may
# read them to make sense of a reply; it may not report them.
CONTEXT_MESSAGES = 5


def watermarks_path(config: Config) -> Path:
    return config.profile_dir.parent / ".wa-agent" / FILENAME


@dataclass
class ChatState:
    """What one chat has already had reported, and from where."""

    seen: set[str] = field(default_factory=set)
    # A legacy single-id watermark, from before this was a set. It means
    # "everything up to and including this id has been reported", but only the
    # id itself is known -- the rest is recovered from the next capture that
    # contains it.
    floor: str = ""


def read_reported(config: Config) -> dict[str, ChatState]:
    """Chat name -> ChatState. An unreadable file means "nothing seen"."""
    try:
        data = json.loads(watermarks_path(config).read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, ChatState] = {}
    for name, value in (data.get("chats") or {}).items():
        if not isinstance(name, str):
            continue
        if isinstance(value, str):
            # The old format: one id, meaning everything up to it.
            out[name] = ChatState(seen={value}, floor=value)
        elif isinstance(value, dict):
            seen = {i for i in (value.get("seen") or []) if isinstance(i, str)}
            floor = value.get("floor") or ""
            out[name] = ChatState(seen=seen,
                                  floor=floor if isinstance(floor, str) else "")
    return out


def write_reported(config: Config, state: dict[str, ChatState]) -> None:
    path = watermarks_path(config)
    payload = {
        name: {"seen": sorted(cs.seen)[-MAX_SEEN:],
               **({"floor": cs.floor} if cs.floor else {})}
        for name, cs in state.items()
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(json.dumps({"chats": payload}, ensure_ascii=False,
                                   indent=2), encoding="utf-8")
        path.chmod(0o600)
    except OSError:
        # Losing this repeats a digest. Never worth killing a tick over.
        pass


@dataclass(frozen=True)
class Window:
    """What to report, what to read for context, and what could not be reached."""

    messages: list[dict] = field(default_factory=list)
    context: list[dict] = field(default_factory=list)
    gap: bool = False
    reason: str = ""


def unseen(messages: list[dict], state: ChatState | None,
           cap: int | None = None,
           context_messages: int = CONTEXT_MESSAGES) -> Window:
    """The messages in `messages` that have never been reported.

    Anything already reported is filtered out no matter where it sits, so a
    capture that reaches further back than the last one cannot resurrect it.
    The last few reported messages before the first new one come back as
    `context`: a reply reads as nonsense without the message it answers, and
    the summariser is told to read them and not report them.

    `gap` means the capture and the record do not overlap at all -- every
    message in view is new AND something has been reported before -- so there
    may be messages between them that were never captured. The reachable ones
    are still returned; saying nothing arrived would be worse.
    """
    state = state or ChatState()
    seen = set(state.seen)

    # Recover a legacy watermark: everything at or before it was reported.
    if state.floor:
        for index, message in enumerate(messages):
            if message.get("msg_id") == state.floor:
                seen.update(m.get("msg_id") for m in messages[:index + 1]
                            if m.get("msg_id"))
                break

    # Anchor on the LAST reported message in view, not the first unreported
    # one. A capture that reaches further back than the previous one surfaces
    # messages older than anything ever reported; those are not news, they are
    # history the user scrolled past days ago. Only what follows the most
    # recent thing they were told can be new.
    last_reported = -1
    for index, message in enumerate(messages):
        if message.get("msg_id") in seen:
            last_reported = index

    if last_reported >= 0:
        fresh = [m for m in messages[last_reported + 1:]
                 if m.get("msg_id") not in seen]
        start = max(0, last_reported + 1 - context_messages)
        context = messages[start:last_reported + 1]
        if not fresh:
            return Window(messages=[], context=[])
        if cap is not None and len(fresh) > cap:
            return Window(messages=fresh[-cap:], context=[], gap=True,
                          reason=f"{len(fresh) - cap} older message(s) left out "
                                 f"to keep the digest to {cap}")
        return Window(messages=fresh, context=context)

    fresh = [m for m in messages if m.get("msg_id") not in seen]
    context = []
    if not fresh:
        return Window(messages=[], context=[])

    if seen:
        return Window(messages=_cap(fresh, cap), context=[], gap=True,
                      reason="nothing in view had been reported before; "
                             "anything between the last digest and these is "
                             "not covered")
    if cap is not None and len(fresh) > cap:
        return Window(messages=fresh[-cap:], context=[], gap=True,
                      reason=f"{len(fresh) - cap} older message(s) left out to "
                             f"keep the digest to {cap}")
    return Window(messages=fresh, context=context)


def _cap(messages: list[dict], cap: int | None) -> list[dict]:
    return messages if cap is None or len(messages) <= cap else messages[-cap:]


def advance(config: Config, chats: list[dict]) -> dict[str, ChatState]:
    """Record everything a POSTED digest reported, so it never repeats.

    Only `messages` counts. `context` was handed over to be read, not told, so
    marking it reported would hide it from a digest that genuinely needs it.
    """
    state = read_reported(config)
    for block in chats:
        name = block.get("chat")
        if not name:
            continue
        current = state.setdefault(name, ChatState())
        for message in block.get("messages") or []:
            msg_id = message.get("msg_id")
            if msg_id:
                current.seen.add(msg_id)
        # The legacy floor has served its purpose once real ids are recorded.
        current.floor = ""
        if len(current.seen) > MAX_SEEN:
            current.seen = set(sorted(current.seen)[-MAX_SEEN:])
    write_reported(config, state)
    return state
