"""Draft -> approve -> send, with the orchestrator outside this process.

Claude Code gathers Gmail/Calendar context and writes the draft text; this
module owns only the WhatsApp side and the safety gates. Splitting it this way
keeps every irreversible action behind deterministic Python that a model cannot
talk its way past.

Sending requires ALL of:
  * the chat is on the allowlist
  * you posted an exact "OK <draft-id>" in your self-chat, after the draft
  * --live was passed (dry-run is the default everywhere)
  * the open chat's identity matches, on two independent signals
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .allowlist import Allowlist
from .interstitials import dismiss as dismiss_overlays
from .approval import (
    Command,
    Decision,
    Draft,
    Journal,
    Status,
    new_draft_id,
    render_draft_message,
    resolve,
)
from .compose import SendRefused, send_message, wait_for_chat_ready
from .config import Config, ensure_private_dir
from .export import find_row_by_name
from .selfchat import find_message_id, open_self_chat, post, read
from .unread import UnreadChat, apply_unread_filter, clear_filter, _badge_count, _text
from . import selectors

AGENT_DIRNAME = ".wa-agent"


def agent_dir(config: Config) -> Path:
    path = config.profile_dir.parent / AGENT_DIRNAME
    ensure_private_dir(path)
    return path


def allowlist_path(config: Config) -> Path:
    return agent_dir(config) / "allowlist.json"


def journal_path(config: Config) -> Path:
    return agent_dir(config) / "journal.jsonl"


def draft_path(config: Config, draft_id: str) -> Path:
    safe = draft_id.replace("#", "").replace("/", "_")
    return agent_dir(config) / f"draft_{safe}.json"


@dataclass
class PendingDraft:
    """A draft posted to the self-chat and awaiting your decision."""

    draft: Draft
    marker_id: str      # id of the self-chat message holding the draft

    def as_dict(self) -> dict:
        return {"draft": self.draft.as_dict(), "marker_id": self.marker_id}

    @classmethod
    def from_dict(cls, data: dict) -> PendingDraft:
        d = data["draft"]
        return cls(
            draft=Draft(
                draft_id=d["draft_id"],
                recipient=d["recipient"],
                source_chat=d["source_chat"],
                body=d["body"],
                quoted=d.get("quoted", ""),
                sources=d.get("sources", []),
                created_at=datetime.fromisoformat(d["created_at"]),
                ttl_hours=d.get("ttl_hours", 2.0),
                status=Status(d.get("status", "pending")),
                revision=int(d.get("revision", 0)),
            ),
            marker_id=data.get("marker_id", ""),
        )


def save_pending(config: Config, pending: PendingDraft) -> Path:
    path = draft_path(config, pending.draft.draft_id)
    path.write_text(json.dumps(pending.as_dict(), ensure_ascii=False, indent=2),
                    encoding="utf-8")
    path.chmod(0o600)
    return path


def load_pending(config: Config, draft_id: str) -> PendingDraft:
    return PendingDraft.from_dict(
        json.loads(draft_path(config, draft_id).read_text(encoding="utf-8"))
    )


def list_unread(page) -> list[UnreadChat]:
    """Unread chats, read from the list only. Opens nothing, sends no receipts."""
    filtered = apply_unread_filter(page)
    chats: list[UnreadChat] = []
    try:
        rows = page.locator(selectors.CHAT_ROWS)
        for index in range(rows.count()):
            row = rows.nth(index)
            count = _badge_count(row)
            if count == 0 and not filtered:
                continue
            name = _text(row, selectors.ROW_TITLE)
            if name:
                chats.append(UnreadChat(
                    name=name, unread_count=count,
                    timestamp=_text(row, selectors.ROW_DETAIL),
                    preview=_text(row, selectors.ROW_PREVIEW)))
    finally:
        if filtered:
            clear_filter(page)
    return chats


def list_chats(page, search: str = "", limit: int = 200) -> list[dict]:
    """Chat-list rows and their exact titles.

    Read-only: opens no chat, so no read receipts. Exists so an allowlist entry
    can be copied verbatim rather than retyped -- names often carry emoji or
    invisible characters that a hand-typed entry would never match.
    """
    from .export import row_title_matches  # noqa: F401  (kept for parity)

    rows = page.locator(selectors.CHAT_ROWS)
    needle = search.casefold()
    out: list[dict] = []
    for index in range(min(rows.count(), limit)):
        row = rows.nth(index)
        title = _text(row, selectors.ROW_TITLE)
        if not title:
            continue
        if needle and needle not in title.casefold():
            continue
        out.append({
            "name": title,
            "is_self_chat": bool(row.locator('[data-testid="message-yourself-row"]').count()),
            "unread": _badge_count(row),
        })
    return out


def pending_drafts(config: Config, chat: str = "") -> list[dict]:
    """Drafts that are still awaiting a decision.

    A polling loop must not re-draft for a message it already drafted for, or
    it would repost the same proposal every tick and bury the self-chat.
    """
    journal = Journal(journal_path(config))
    now = datetime.now(timezone.utc)
    out: list[dict] = []
    for path in sorted(agent_dir(config).glob("draft_*.json")):
        try:
            pending = PendingDraft.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            continue
        draft = pending.draft
        if chat and draft.recipient != chat:
            continue
        if journal.already_sent(draft.draft_id) or journal.is_retired(draft.draft_id):
            continue
        if draft.is_expired(now):
            continue
        out.append({
            "draft_id": draft.draft_id,
            "recipient": draft.recipient,
            "created_at": draft.created_at.isoformat(),
            "expires_at": draft.expires_at.isoformat(),
            "body": draft.body,
            "revision": draft.revision,
        })
    return out


def retire_draft(config: Config, draft_id: str, reason: str = "superseded") -> dict:
    journal = Journal(journal_path(config))
    journal.retire(draft_id, reason)
    return {"retired": draft_id, "reason": reason}


def read_chat(page, chat: str, depth: int = 10) -> dict:
    """Open ONE chat and read its messages.

    Opening marks it read and sends read receipts to that contact. Scoped to a
    single named chat on purpose: the bulk `wa-read` would open every unread
    conversation and spend receipts on people who are not part of this.

    `depth` is how far back to scroll. The default suits a reply, which only
    needs the question and a little context. A digest needs enough to still
    contain its watermark, or it cannot tell what it has already covered --
    see `_collect_group_messages`. Scrolling is the expensive part (~1.2s a
    step), which is why this is not simply always deep.
    """
    from .messages import capture_chat   # local: not imported at module level

    row = find_row_by_name(page, chat)
    if row is None:
        return {"ok": False, "reason": f"chat {chat!r} not found"}
    row.click(timeout=5000)
    wait_for_chat_ready(page, expected=chat)
    dismiss_overlays(page)
    capture = capture_chat(page, chat, expected_unread=depth)
    return {
        "ok": True,
        "chat": chat,
        "message_count": len(capture.messages),
        "messages": [m.as_dict() for m in capture.messages],
        "error": capture.error,
    }


def _fresh_draft_id(config: Config, attempts: int = 40) -> str:
    """A draft id no journal entry or draft file has ever used.

    Ids are 3 characters from a 32-character alphabet while the journal's
    `sent` and `retired` sets grow for ever (every EDIT retires one). A
    collision is silent and total: the new draft is filtered out of
    `pending_drafts`, `resolve` answers REJECT "withdrawn", and the user's
    OK does nothing at all.
    """
    journal = Journal(journal_path(config))
    for _ in range(attempts):
        candidate = new_draft_id()
        if (journal.already_sent(candidate) or journal.is_retired(candidate)
                or draft_path(config, candidate).exists()):
            continue
        return candidate
    raise RuntimeError("could not find an unused draft id")


def _post_and_locate(page, text: str, draft_id: str,
                     settle_s: float = 12.0) -> str:
    """Post a draft and wait for it to really be there. Returns its message id.

    A single read straight after `post` is not enough, and believing it was
    cost seven paid runs and seven orphan drafts in one afternoon:

    * `selfchat.post` RETURNS a SendResult; it does not raise when the send is
      refused after the click. Discarding it turned "never posted" into the
      misleading "posted ... but could not locate its message id".
    * A genuine "sent" is not yet transmitted when the call returns. Reading
      immediately finds nothing, `propose` raises, the caller closes the
      browser -- and the message either lands a moment later with no pending
      entry to arm it, or is dropped with the context.

    `post_note` already learned this for digests; its docstring claimed drafts
    were immune because propose "reads its message back", which was true only
    of the reading, not of the waiting. So: poll until the id appears.
    """
    outcome = post(page, text)
    if outcome is not None:
        if getattr(outcome, "dry_run", False):
            raise RuntimeError(f"draft {draft_id} was a dry run; nothing was posted")
        if not getattr(outcome, "ok", True):
            raise RuntimeError(
                f"draft {draft_id} was not posted: {getattr(outcome, 'detail', '')}"
            )

    deadline = time.monotonic() + settle_s
    while True:
        try:
            # scroll=False: only the newest messages matter, and a full
            # scrolling read costs ~6s of the settle budget it is spending.
            marker = find_message_id(read(page, limit=8, refresh=True,
                                          scroll=False), draft_id)
            if marker:
                return marker
        except Exception:
            pass
        if time.monotonic() >= deadline:
            return ""
        page.wait_for_timeout(500)


def propose(page, config: Config, chat: str, body: str,
            sources: list[str] | None = None, quoted: str = "",
            ttl_hours: float = 2.0, revision: int = 0) -> PendingDraft:
    """Post a draft into your self-chat and record it as pending.

    Refuses for a chat that is not allowlisted -- there is no point drafting
    something that could never be sent.
    """
    allow = Allowlist(allowlist_path(config))
    entry = allow.get(chat)
    if entry is None:
        raise SendRefused(f"{chat!r} is not on the allowlist")
    if not entry.can_reply:
        raise SendRefused(
            f"{chat!r} is monitored for summaries only; it cannot be replied to"
        )

    draft = Draft(
        draft_id=_fresh_draft_id(config), recipient=chat, source_chat=chat,
        body=body.strip(), quoted=quoted, sources=sources or [],
        ttl_hours=ttl_hours, revision=revision,
    )
    text = render_draft_message(draft, audience=entry.audience())
    marker = _post_and_locate(page, text, draft.draft_id)
    if not marker:
        # Without it, read_after returns EVERY visible message instead of only
        # those newer than the draft -- so an older command bearing the same id
        # would count as consent. Fail loudly rather than post an unguarded
        # draft; the caller records propose_failed and retries next tick.
        raise RuntimeError(
            f"posted draft {draft.draft_id} but could not locate its message id; "
            "refusing to arm an approval that cannot be bounded"
        )
    pending = PendingDraft(draft=draft, marker_id=marker)

    journal = Journal(journal_path(config))
    journal.record_draft(draft)
    save_pending(config, pending)
    return pending


def poll(page, config: Config, draft_id: str, consume: bool = False) -> Command:
    """Look for your decision in the self-chat, after the draft's own message.

    Read-only by default: checking the decision must not spend it, or a later
    `deliver` would find the approval already consumed and refuse.
    """
    pending = load_pending(config, draft_id)
    journal = Journal(journal_path(config))
    open_self_chat(page)
    from .selfchat import read_after

    messages = read_after(page, pending.marker_id)
    return resolve(
        pending.draft,
        [{"text": m.text, "msg_id": m.msg_id} for m in messages],
        journal,
        consume=consume,
    )


# How far back to look for the message a draft answers. Drafts expire in two
# hours, so it is normally among the newest few.
SOURCE_LOOKBACK = 40


def _source_message_changed(page, quoted: str) -> str:
    """Reason the draft must not be sent, or "" if the source still stands.

    A draft answers one specific message. If that message has been edited or
    deleted since it was captured, the reply no longer answers anything -- and
    the user cannot tell, because their chat shows only the NEW text while the
    draft was written against the old.

    Refusing when the message cannot be found is deliberate. It costs a
    re-request; sending a confident answer to a question that has changed
    underneath it costs rather more.
    """
    if not quoted:
        return ""
    try:
        from .messages import read_window

        recent = read_window(page, minimum=SOURCE_LOOKBACK)
    except Exception as exc:
        return f"could not re-read the chat to confirm the message: {exc}"
    if any((message.text or "") == quoted for message in recent):
        return ""
    return ("the message this answers is no longer in the chat as it was "
            f"captured -- edited or deleted since. It read: {quoted[:120]!r}")


def deliver(page, config: Config, draft_id: str, live: bool = False) -> dict:
    """Send an approved draft. Dry-run unless `live` is explicitly True."""
    pending = load_pending(config, draft_id)
    draft = pending.draft
    journal = Journal(journal_path(config))

    allow = Allowlist(allowlist_path(config))
    if not allow.can_reply(draft.recipient):
        return {"ok": False,
                "reason": f"{draft.recipient!r} is not a reply-mode allowlisted chat"}

    # Spend the approval only on a real send. A rehearsal must leave it
    # intact, or a dry run would silently disarm the subsequent live one.
    command = poll(page, config, draft_id, consume=live)
    if command.decision is not Decision.APPROVE:
        return {"ok": False, "reason": f"not approved (decision={command.decision.value})",
                "detail": command.reason or command.instructions}

    if journal.already_sent(draft.draft_id):
        return {"ok": False, "reason": "already sent"}

    row = find_row_by_name(page, draft.recipient)
    if row is None:
        return {"ok": False, "reason": f"chat {draft.recipient!r} not found"}
    row.click(timeout=5000)
    wait_for_chat_ready(page, expected=draft.recipient)

    stale = _source_message_changed(page, draft.quoted)
    if stale:
        # WhatsApp lets the sender EDIT a message after it arrives, and the
        # edit replaces the text in place. On 2026-09-06 a message was
        # captured, drafted against, and edited to something unrelated inside
        # the same minute: the draft answered "where did we last light a
        # bonfire" while the chat now read "when is your next psychiatrist
        # appointment". Nothing checked, and an OK would have sent it.
        return {"ok": False, "reason": stale}

    # Journal immediately BEFORE the click, never earlier: a crash mid-send
    # must not look unsent, but a pre-send refusal must not look sent either.
    def _journal_attempt() -> None:
        journal.record_send_attempt(draft.draft_id, draft.recipient)

    try:
        result = send_message(page, draft.recipient, draft.body,
                              dry_run=not live,
                              on_before_send=_journal_attempt if live else None)
    except SendRefused as exc:
        journal.record_result(draft.draft_id, False, str(exc))
        return {"ok": False, "reason": f"refused: {exc}"}

    journal.record_result(draft.draft_id, result.ok, result.detail)
    return {"ok": result.ok, "dry_run": result.dry_run,
            "recipient": result.recipient, "detail": result.detail}
