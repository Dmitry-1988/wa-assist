"""How far each group was summarised, so a digest never repeats itself.

`_collect_group_messages` always captured the last N messages in every
monitored group, so every GROUPSUM re-summarised the same window: ask twice in
an afternoon and the second digest restates the first, with one genuinely new
message buried inside it.

The mark is the `msg_id` of the last message that made it into a digest that
was actually POSTED. Advancing on capture instead would lose messages whenever
a post fails -- which is not hypothetical: a composer bug silently failed five
posts in a row on 2026-09-01. Nothing is marked seen until the user has seen it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config

FILENAME = "digest_seen.json"


def watermarks_path(config: Config) -> Path:
    return config.profile_dir.parent / ".wa-agent" / FILENAME


def read_watermarks(config: Config) -> dict[str, str]:
    """Chat name -> last summarised msg_id. Unreadable file means "none"."""
    try:
        data = json.loads(watermarks_path(config).read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.get("chats", {}).items()
            if isinstance(k, str) and isinstance(v, str)}


def write_watermarks(config: Config, marks: dict[str, str]) -> None:
    path = watermarks_path(config)
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(json.dumps({"chats": marks}, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        path.chmod(0o600)
    except OSError:
        # Losing the mark repeats a digest. Never worth killing a tick over.
        pass


@dataclass(frozen=True)
class Window:
    """What a digest actually covers, and what it could not reach.

    `gap` is the honest part. Everything else here is a best effort at the
    newest messages; `gap` says whether that effort came up short, so the
    daemon can put a warning in the digest instead of letting the user read a
    confident summary of a conversation it only partly saw.
    """

    messages: list[dict] = field(default_factory=list)
    gap: bool = False
    reason: str = ""


def window(messages: list[dict], last_id: str | None,
           cap: int | None = None) -> Window:
    """The messages after `last_id`, and whether anything fell outside them.

    Two ways a digest can end up incomplete, both of which used to happen in
    silence:

    * the mark is not in the captured rows at all. The chat produced more
      messages than the capture reaches, so the conversation has moved past
      the mark and whatever sat between them is not here. Everything captured
      is still returned -- it is genuinely unsummarised, and returning nothing
      would lose it AND report that nothing arrived -- but the shortfall is
      now stated.
    * more messages survived the mark than `cap` allows into one prompt. The
      newest `cap` are kept, because a digest of the oldest half of a backlog
      is worse than useless, and the truncation is reported.

    A missing mark can also mean the chat was cleared or that message deleted.
    That is indistinguishable from here and reports the same way; it resolves
    itself after one digest.
    """
    if not last_id:
        fresh, found = list(messages), True
    else:
        found = False
        fresh = list(messages)
        for index in range(len(messages) - 1, -1, -1):
            if messages[index].get("msg_id") == last_id:
                fresh, found = list(messages[index + 1:]), True
                break

    if not found:
        return Window(messages=_cap(fresh, cap), gap=True,
                      reason="the last summarised message is no longer in view; "
                             "anything between it and these is not covered")
    if cap is not None and len(fresh) > cap:
        return Window(messages=fresh[-cap:], gap=True,
                      reason=f"{len(fresh) - cap} older message(s) left out to "
                             f"keep the digest to {cap}")
    return Window(messages=fresh)


def _cap(messages: list[dict], cap: int | None) -> list[dict]:
    return messages if cap is None or len(messages) <= cap else messages[-cap:]


def since(messages: list[dict], last_id: str | None) -> list[dict]:
    """The messages after `last_id`. See `window` for what this cannot tell you."""
    return window(messages, last_id).messages


def last_id(messages: list[dict]) -> str:
    """The id to mark once these messages have been shown to the user."""
    for message in reversed(messages):
        found = message.get("msg_id")
        if found:
            return found
    return ""


def advance(config: Config, chats: list[dict]) -> dict[str, str]:
    """Record how far each chat in a POSTED digest was summarised."""
    marks = read_watermarks(config)
    for block in chats:
        name = block.get("chat")
        mark = last_id(block.get("messages") or [])
        if name and mark:
            marks[name] = mark
    write_watermarks(config, marks)
    return marks
