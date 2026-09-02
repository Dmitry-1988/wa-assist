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


def since(messages: list[dict], last_id: str | None) -> list[dict]:
    """The messages after `last_id`.

    A mark that is not in the captured window means the conversation has moved
    past it entirely, so everything captured is new. Returning nothing there
    would silently drop whatever scrolled by in between.
    """
    if not last_id:
        return list(messages)
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("msg_id") == last_id:
            return list(messages[index + 1:])
    return list(messages)


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
