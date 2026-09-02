"""Draft approval: the control that decides whether anything is ever sent.

Deliberately dumb and deterministic. No model output reaches this module --
approval is a strict string match on a message you wrote in your own self-chat.
The failure mode that matters is a FALSE APPROVAL (sending something you did
not sanction), so every ambiguity resolves to "do not send".
"""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

DEFAULT_TTL_HOURS = 2.0

# Four characters total ("#" + 3), so the approval is quick to type on a
# phone. Short ids are safe here because approval requires the WHOLE message to
# be exactly "OK <id>" -- a stray mention of the same characters in your notes
# is classified AMBIGUOUS and never sends.
_ID_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no I/O/0/1
ID_PREFIX = "#"
_ID_LENGTH = 3


class Decision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    EDIT = "edit"
    AMBIGUOUS = "ambiguous"   # mentions the draft but is not a clean command
    NONE = "none"             # unrelated message


class Status(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    EDIT_REQUESTED = "edit_requested"
    REJECTED = "rejected"
    EXPIRED = "expired"
    SENT = "sent"
    FAILED = "failed"


def new_draft_id() -> str:
    return ID_PREFIX + "".join(secrets.choice(_ID_ALPHABET) for _ in range(_ID_LENGTH))


@dataclass
class Draft:
    draft_id: str
    recipient: str
    source_chat: str
    body: str
    quoted: str = ""
    sources: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    ttl_hours: float = DEFAULT_TTL_HOURS
    status: Status = Status.PENDING
    # How many EDITs produced this text. Without it the counter restarted at
    # zero on every revision and MAX_REVISIONS could never be reached, so an
    # edit loop was unbounded -- one paid drafting run per round, for ever.
    revision: int = 0

    @property
    def expires_at(self) -> datetime:
        return self.created_at + timedelta(hours=self.ttl_hours)

    def is_expired(self, now: datetime | None = None) -> bool:
        return (now or datetime.now(timezone.utc)) >= self.expires_at

    def as_dict(self) -> dict:
        data = asdict(self)
        data["created_at"] = self.created_at.isoformat()
        data["status"] = self.status.value
        return data


@dataclass(frozen=True)
class Command:
    decision: Decision
    draft_id: str = ""
    instructions: str = ""
    reason: str = ""


# A standalone instruction, not tied to any draft. Whole-message match for the
# same reason approvals are: "GROUPSUM later maybe" must not trigger a run that
# opens every group and spends read receipts on dozens of people.
_GROUPSUM = re.compile(r"^\s*groupsum\s*[.!]?\s*$", re.IGNORECASE)


def is_groupsum(text: str) -> bool:
    return bool(text) and bool(_GROUPSUM.match(text))


def _escaped(draft_id: str) -> str:
    return re.escape(draft_id)


# Cyrillic letters that are indistinguishable from Latin ones on screen. A
# Russian keyboard types "OK" as "ОК" (U+041E U+041A) -- the same glyphs, a
# different string -- so an approval that LOOKED exactly right was classified
# ambiguous and silently refused to send. Verified 2026-09-01.
#
# The mapping is 1:1, so offsets are unchanged and a match found in the
# normalised text can be sliced out of the ORIGINAL. That matters: EDIT
# instructions are Russian prose and must never be latinised.
_CONFUSABLES = str.maketrans({
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O",
    "Р": "P", "С": "C", "Т": "T", "У": "Y", "Х": "X", "Ѕ": "S", "І": "I",
    "Ј": "J", "Ԍ": "G",
    "а": "a", "в": "b", "е": "e", "к": "k", "м": "m", "н": "h", "о": "o",
    "р": "p", "с": "c", "т": "t", "у": "y", "х": "x", "ѕ": "s", "і": "i",
    "ј": "j",
})


def latinize(text: str) -> str:
    """Confusable Cyrillic to its Latin twin. Length and offsets preserved."""
    return text.translate(_CONFUSABLES)


def parse_command(text: str, draft_id: str) -> Command:
    """Classify one self-chat message against the draft awaiting approval.

    Approval requires the WHOLE message to be exactly "OK <id>". A message like
    "OK #WA-A7 but change the time" is NOT approval -- it is an edit request.
    Reading it as approval would send text you meant to revise.

    Commands are matched against a latinised copy, so "ОК" typed on a Russian
    layout is accepted; the text of an EDIT is taken from the original, so the
    instructions reach the drafter exactly as written.
    """
    if not text:
        return Command(Decision.NONE)

    body = text.strip()
    probe = latinize(body)
    ident = _escaped(draft_id)

    if not re.search(ident, probe, re.IGNORECASE):
        return Command(Decision.NONE)

    if re.fullmatch(rf"(?:ok|approve|send)\s+{ident}[.!]?", probe, re.IGNORECASE):
        return Command(Decision.APPROVE, draft_id)

    if re.fullmatch(rf"(?:no|cancel|drop|reject)\s+{ident}[.!]?", probe, re.IGNORECASE):
        return Command(Decision.REJECT, draft_id)

    edit = re.fullmatch(
        rf"edit\s+{ident}\s*[:\-]?\s*(?P<rest>.+)", probe, re.IGNORECASE | re.DOTALL
    )
    if edit:
        # Sliced from `body`, not `probe`: Russian instructions must survive.
        instructions = body[edit.start("rest"):edit.end("rest")].strip()
        if instructions:
            return Command(Decision.EDIT, draft_id, instructions=instructions)

    # Mentions the draft but is not a clean command. Never send on this: it is
    # usually an approval with a caveat attached ("OK ... but change X").
    return Command(
        Decision.AMBIGUOUS,
        draft_id,
        reason="message references the draft but is not an exact OK/NO/EDIT command",
    )


class Journal:
    """Append-only record of drafts and consumed commands.

    Written BEFORE a send is attempted, so a crash mid-send cannot lead to the
    same draft being sent twice on the next pass.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._seen_commands: set[str] = set()
        self._sent: set[str] = set()
        self._retired: set[str] = set()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("kind") == "command":
                self._seen_commands.add(entry.get("message_id", ""))
            elif entry.get("kind") == "send_attempt":
                self._sent.add(entry.get("draft_id", ""))
            elif entry.get("kind") == "retired":
                self._retired.add(entry.get("draft_id", ""))

    def _append(self, entry: dict) -> None:
        entry["at"] = datetime.now(timezone.utc).isoformat()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self.path.chmod(0o600)

    def command_seen(self, message_id: str) -> bool:
        return bool(message_id) and message_id in self._seen_commands

    def record_command(self, message_id: str, decision: Decision, draft_id: str) -> None:
        self._seen_commands.add(message_id)
        self._append({
            "kind": "command",
            "message_id": message_id,
            "decision": decision.value,
            "draft_id": draft_id,
        })

    def already_sent(self, draft_id: str) -> bool:
        return draft_id in self._sent

    def is_retired(self, draft_id: str) -> bool:
        return draft_id in self._retired

    def retire(self, draft_id: str, reason: str = "superseded") -> None:
        """Withdraw a draft so it can never be approved later.

        Superseded drafts stay visible in the self-chat, so without this an old
        one -- possibly a factually wrong one -- could still be approved by
        typing its id. Retiring is local and one-way.
        """
        self._retired.add(draft_id)
        self._append({"kind": "retired", "draft_id": draft_id, "reason": reason})

    def record_send_attempt(self, draft_id: str, recipient: str) -> None:
        """Call this BEFORE sending, never after."""
        self._sent.add(draft_id)
        self._append({
            "kind": "send_attempt",
            "draft_id": draft_id,
            "recipient": recipient,
        })

    def record_result(self, draft_id: str, ok: bool, detail: str = "") -> None:
        self._append({
            "kind": "send_result",
            "draft_id": draft_id,
            "ok": ok,
            "detail": detail,
        })

    def record_draft(self, draft: Draft) -> None:
        self._append({"kind": "draft", **draft.as_dict()})


def resolve(
    draft: Draft,
    messages: list[dict],
    journal: Journal,
    now: datetime | None = None,
    consume: bool = True,
) -> Command:
    """Find the decision for `draft` among self-chat messages posted after it.

    `messages` are dicts with at least `text` and `msg_id`, oldest first, and
    are expected to be only those newer than the draft.

    `consume=False` inspects without journalling, so merely *looking* at a
    decision does not spend it. Consumption belongs at the point of action.
    """
    now = now or datetime.now(timezone.utc)

    if journal.already_sent(draft.draft_id):
        return Command(Decision.NONE, draft.draft_id, reason="already sent")

    if journal.is_retired(draft.draft_id):
        return Command(Decision.REJECT, draft.draft_id, reason="withdrawn")

    if draft.is_expired(now):
        return Command(Decision.REJECT, draft.draft_id, reason="expired")

    # A definite decision anywhere wins over an earlier unclear one. Returning
    # the FIRST non-NONE command wedged a draft permanently: "OK #X but shorter"
    # is AMBIGUOUS, is never consumed (poll uses consume=False), and so was
    # re-matched on every tick -- a corrected "OK #X" posted afterwards could
    # never be reached, and the draft died at TTL. Refusing to send on an
    # unclear message is right; refusing to read the next one is not.
    unclear: Command | None = None
    for message in messages:
        message_id = message.get("msg_id", "")
        if journal.command_seen(message_id):
            continue
        command = parse_command(message.get("text", ""), draft.draft_id)
        if command.decision is Decision.NONE:
            continue
        if command.decision is Decision.AMBIGUOUS:
            if unclear is None:
                unclear = command
            continue
        if consume and message_id:
            journal.record_command(message_id, command.decision, draft.draft_id)
        return command

    if unclear is not None:
        return unclear
    return Command(Decision.NONE, draft.draft_id, reason="no decision yet")


def render_draft_message(draft: Draft, audience: str = "") -> str:
    """The text posted into your self-chat. Shows exactly what would be sent."""
    lines = [
        f"🤖 DRAFT {draft.draft_id} → {draft.recipient}"
        + (f"  ({audience})" if audience else ""),
    ]
    if draft.quoted:
        lines.append(f'Re: "{draft.quoted[:120]}"')
    if draft.sources:
        # One per line and individually trimmed: joined into a paragraph these
        # made the draft thousands of characters long, which broke posting and
        # buried the text you actually have to read.
        lines.append("Sources:")
        for source in draft.sources[:8]:
            lines.append(f"  · {source[:160]}")
        if len(draft.sources) > 8:
            lines.append(f"  · (+{len(draft.sources) - 8} more)")
    else:
        lines.append("Sources: none")
    lines += [
        "",
        "── WILL SEND VERBATIM ──",
        draft.body,
        "────────────────────────",
        "",
        f"OK {draft.draft_id}  |  EDIT {draft.draft_id} <changes>  |  NO {draft.draft_id}",
        f"expires {draft.expires_at.astimezone().strftime('%H:%M')}",
    ]
    return "\n".join(lines)
