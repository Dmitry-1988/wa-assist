"""The file contract between the daemon and the drafting LLM.

Capability split:

  * The DAEMON owns every WhatsApp action -- reading chats, posting drafts to
    the self-chat, reading your approvals, sending. It composes nothing.
  * The DRAFTER (a headless Claude run) owns judgement -- reading context and
    writing text. It has no shell and never touches WhatsApp.

They exchange files, so the drafter's inability to send is structural rather
than a rule it is asked to follow.

The one asymmetry that matters: the drafter supplies BODY TEXT ONLY. Routing --
which chat a draft is for -- is carried by the queue item the daemon wrote, and
is never read back from the outbox. Otherwise an injected message could aim a
draft at a different recipient without ever needing the ability to send.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import Config, ensure_private_dir

MAX_REVISIONS = 5
MAX_BODY_CHARS = 4000


class ContractError(Exception):
    """An outbox file that cannot be trusted. Never partially applied."""


def queue_dir(config: Config) -> Path:
    return ensure_private_dir(config.profile_dir.parent / ".wa-agent" / "queue")


def outbox_dir(config: Config) -> Path:
    return ensure_private_dir(config.profile_dir.parent / ".wa-agent" / "outbox")


_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass
class QueueItem:
    """Daemon -> drafter. Everything the drafter is allowed to know."""

    queue_id: str
    chat: str                      # authoritative routing; drafter cannot change it
    messages: list[dict] = field(default_factory=list)
    edit_instructions: str = ""
    previous_body: str = ""
    revision: int = 0
    created_at: str = ""
    # Drafting runs that failed for want of context (mail/calendar unreachable).
    # The item stays queued and is retried; this is what makes a long outage
    # visible instead of silently swallowing the message.
    attempts: int = 0
    stalled_notified: bool = False

    def as_dict(self) -> dict:
        data = dict(self.__dict__)
        data["created_at"] = self.created_at or datetime.now(timezone.utc).isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> QueueItem:
        return cls(
            queue_id=data["queue_id"],
            chat=data["chat"],
            messages=data.get("messages", []),
            edit_instructions=data.get("edit_instructions", ""),
            previous_body=data.get("previous_body", ""),
            revision=int(data.get("revision", 0)),
            created_at=data.get("created_at", ""),
            attempts=int(data.get("attempts", 0)),
            stalled_notified=bool(data.get("stalled_notified", False)),
        )


def write_queue_item(config: Config, item: QueueItem) -> Path:
    if not _SAFE_ID.match(item.queue_id):
        raise ContractError(f"unsafe queue id: {item.queue_id!r}")
    path = queue_dir(config) / f"{item.queue_id}.json"
    path.write_text(json.dumps(item.as_dict(), ensure_ascii=False, indent=2),
                    encoding="utf-8")
    path.chmod(0o600)
    return path


def read_queue(config: Config) -> list[QueueItem]:
    items: list[QueueItem] = []
    for path in sorted(queue_dir(config).glob("*.json")):
        try:
            items.append(QueueItem.from_dict(json.loads(path.read_text(encoding="utf-8"))))
        except Exception:
            continue
    return items


@dataclass(frozen=True)
class DraftSubmission:
    """Drafter -> daemon. Body and evidence only. No routing, ever."""

    queue_id: str
    body: str
    sources: list[str]


def parse_submission(raw: str, expected_queue_id: str) -> DraftSubmission:
    """Validate an outbox file. Rejects anything that tries to steer routing.

    Rejecting rather than sanitising is deliberate: a file that tries to set a
    recipient is evidence something is wrong, and should stop the pipeline
    rather than be quietly cleaned up and used.
    """
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ContractError(f"outbox is not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise ContractError("outbox must be a JSON object")

    # Routing is the daemon's alone. Any attempt to supply it is a hard stop.
    for forbidden in ("chat", "recipient", "to", "send", "live", "draft_id"):
        if forbidden in data:
            raise ContractError(
                f"outbox may not contain {forbidden!r}: routing and sending are "
                "the daemon's, not the drafter's"
            )

    queue_id = data.get("queue_id", "")
    if queue_id != expected_queue_id:
        raise ContractError(
            f"outbox queue_id {queue_id!r} does not match {expected_queue_id!r}"
        )

    body = data.get("body", "")
    if not isinstance(body, str) or not body.strip():
        raise ContractError("outbox body must be a non-empty string")
    if len(body) > MAX_BODY_CHARS:
        raise ContractError(f"outbox body exceeds {MAX_BODY_CHARS} characters")

    sources = data.get("sources", [])
    if not isinstance(sources, list) or not all(isinstance(s, str) for s in sources):
        raise ContractError("outbox sources must be a list of strings")

    return DraftSubmission(queue_id=queue_id, body=body.strip(), sources=sources[:20])


def take_submission(config: Config, queue_id: str) -> DraftSubmission | None:
    """Read and validate the drafter's output for one queue item."""
    path = outbox_dir(config) / f"{queue_id}.json"
    if not path.exists():
        return None
    return parse_submission(path.read_text(encoding="utf-8"), queue_id)


def write_submission(config: Config, queue_id: str, submission: DraftSubmission) -> Path:
    """Persist a validated answer. Written by the DAEMON, never by the model.

    The drafter has no filesystem tools, so the only thing it can influence is
    the JSON it replies with -- which has already been through
    `parse_submission` before it reaches here.
    """
    path = outbox_dir(config) / f"{queue_id}.json"
    path.write_text(
        json.dumps({"queue_id": submission.queue_id, "body": submission.body,
                    "sources": submission.sources}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    path.chmod(0o600)
    return path


def clear_item(config: Config, queue_id: str) -> None:
    for path in (queue_dir(config) / f"{queue_id}.json",
                 outbox_dir(config) / f"{queue_id}.json"):
        path.unlink(missing_ok=True)
