"""One unattended cycle: send what you approved, notice what needs a draft.

Deliberately split by what needs judgement:

  * Sending an APPROVED draft is deterministic -- the text was fixed when you
    approved it, so a daemon can do it and you get replies out promptly.
  * Writing a draft needs context-gathering and judgement, so this only records
    that a message is waiting. An interactive session picks it up.

The daemon therefore never composes anything, and never sends anything you have
not already approved verbatim.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from .agent import (
    agent_dir,
    propose,
    read_chat,
    allowlist_path,
    deliver,
    journal_path,
    list_unread,
    load_pending,
    pending_drafts,
    poll,
    retire_draft,
)
from .allowlist import Allowlist
from .approval import Decision, Journal, is_groupsum
from .config import WHATSAPP_URL, Config, load_config
from .interstitials import dismiss
from .drafter import run_drafter, run_summarizer
from .lock import Busy, profile_lock
from .pipeline import (
    ContractError,
    outbox_dir,
    queue_dir,
    MAX_REVISIONS,
    QueueItem,
    clear_item,
    read_queue,
    take_submission,
    write_queue_item,
)
from .page_state import PageState, wait_for_state
from .rotation import (
    due_warning,
    hours_left,
    mark_announced,
    notify_desktop,
    render_warning,
)
from .session import first_page, persistent_context
from .state import read_state
from .watermarks import advance, read_watermarks, since

PAGE_READY_TIMEOUT_S = 30.0

# Ticks a queue item may fail for want of mail/calendar before the user is told.
# At a 300s interval this is roughly half an hour of quiet retrying, which
# covers a transient outage without letting a long one stay invisible.
CONTEXT_STALL_ATTEMPTS = 6


def inbox_path(config: Config) -> Path:
    return agent_dir(config) / "inbox.json"


def _record_inbox(config: Config, waiting: list[dict]) -> None:
    payload = {"updated_at": datetime.now(timezone.utc).isoformat(), "waiting": waiting}
    path = inbox_path(config)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    path.chmod(0o600)


def run_tick(config: Config | None = None) -> dict:
    """One cycle. Never raises; returns a summary for the log."""
    config = config or load_config()
    result: dict = {"at": datetime.now(timezone.utc).isoformat(), "actions": []}

    lock = agent_dir(config) / "profile.lock"
    try:
        # PHASE 1 -- everything that needs WhatsApp, under the profile lock.
        with profile_lock(lock):
            _browser_phase(config, result)
    except Busy:
        result["skipped"] = "profile in use by another process"
        return result
    except Exception as exc:  # a daemon must not die on one bad tick
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    if result.get("blocked"):
        return result

    # PHASE 2 -- the LLM runs, with the profile lock RELEASED. A drafting run
    # can take 300s; holding the browser lock through it starved every other
    # tick and made `wa-login` time out waiting. The comment below used to
    # claim this already happened -- it did not, because the whole tick ran
    # inside the lock one frame up.
    drafted_now = _drafting_phase(config, result)

    # PHASE 3 -- post what was just drafted, re-taking the lock. Previously
    # this ran while the outer lock was still held, and flock conflicts with
    # itself across two fds in one process, so it raised Busy every single
    # time: 0 fast-path posts in 14 attempts in production.
    if drafted_now:
        try:
            with profile_lock(lock):
                _post_phase(config, result)
        except Busy:
            result["actions"].append({"post_deferred": "profile busy; next tick"})
        except Exception as exc:
            result["actions"].append({"post_failed": f"{type(exc).__name__}: {exc}"})
    return result


def _handle_pending(page, config: Config, entry: dict, result: dict) -> None:
    """Act on one pending draft's decision. Raises on browser trouble."""
    draft_id = entry["draft_id"]
    command = poll(page, config, draft_id)
    if command.decision is Decision.APPROVE:
        sent = deliver(page, config, draft_id, live=True)
        result["actions"].append({"draft_id": draft_id, "sent": sent})
    elif command.decision is Decision.REJECT:
        retire_draft(config, draft_id, "rejected in self-chat")
        result["actions"].append({"draft_id": draft_id, "rejected": True})
    elif command.decision is Decision.EDIT:
        result["actions"].append(
            _queue_revision(config, entry, command.instructions)
        )
    elif command.decision is Decision.AMBIGUOUS:
        # Never guess at an unclear approval.
        result["actions"].append({
            "draft_id": draft_id,
            "needs_attention": "ambiguous",
            "instructions": command.instructions,
        })


def _drafting_phase(config: Config, result: dict) -> bool:
    """Run the LLM for each queued item. Needs no WhatsApp, so takes no lock."""
    drafted_now = False
    for item in read_queue(config):
        outbox = outbox_dir(config) / f"{item.queue_id}.json"
        if outbox.exists():
            continue
        # An LLM run can outlast the tick interval, so the next tick would
        # otherwise start a second run for the same item -- two paid runs
        # racing. One lock per item, held for the duration of the run.
        try:
            with profile_lock(agent_dir(config) / f"run-{item.queue_id}.lock"):
                outcome = _run_for(item, config, outbox)
        except Busy:
            result["actions"].append({"queue_id": item.queue_id,
                                      "skipped": "already being drafted"})
            continue
        result["actions"].append(outcome)
        if not outcome.get("ok", False):
            # ANY failure counts, not just an unreachable MCP: a timing-out or
            # crashing run would otherwise retry for ever, burning a paid run
            # every interval, with nothing ever shown to the user.
            _count_context_failure(config, item)
        drafted_now = drafted_now or outcome.get("ok", False)
    return drafted_now


def _post_phase(config: Config, result: dict) -> None:
    """Post finished drafts and digests without waiting for the next tick."""
    with persistent_context(config.profile_dir, headless=False,
                            quiet=True) as context:
        page = first_page(context)
        page.goto(WHATSAPP_URL, wait_until="domcontentloaded")
        if wait_for_state(page, PAGE_READY_TIMEOUT_S) is PageState.LOGGED_IN:
            dismiss(page)
            _post_ready_drafts(page, config, result)
            _post_ready_summaries(page, config, result)


def _browser_phase(config: Config, result: dict) -> dict:
    allow = Allowlist(allowlist_path(config))
    if len(allow) == 0:
        result["skipped"] = "allowlist is empty"
        return result

    pending = pending_drafts(config)

    with persistent_context(config.profile_dir, headless=False, quiet=True) as context:
        page = first_page(context)
        page.goto(WHATSAPP_URL, wait_until="domcontentloaded")
        if wait_for_state(page, PAGE_READY_TIMEOUT_S) is not PageState.LOGGED_IN:
            # Expected after the 24h rotation; say so plainly and stop. The
            # self-chat died with the session, so this is the one moment the
            # desktop is the only way left to reach the user.
            result["blocked"] = "not logged in — run `uv run wa-login` and scan"
            if notify_desktop(
                config, "logged_out", "wa-agent has stopped",
                "WhatsApp session is gone. Run: uv run wa-login",
            ):
                result["actions"].append({"desktop_notice": "logged_out"})
            return result
        dismiss(page)

        if _rotation_blocks(page, config, result):
            return result
        _report_stalled_items(page, config, result)

        for entry in pending:
            try:
                _handle_pending(page, config, entry, result)
            except Exception as exc:
                # A Playwright timeout or a missing self-chat row must cost one
                # draft, not the rest of the tick: posting, digests, queueing
                # and inbox recording all come after this loop.
                result["actions"].append(
                    {"draft_id": entry.get("draft_id"),
                     "poll_failed": f"{type(exc).__name__}: {exc}"})

        _post_ready_drafts(page, config, result)
        _post_ready_summaries(page, config, result)

        summary_requested = _take_groupsum_request(page, config, result)

        if summary_requested:
            _collect_group_messages(page, config, result)

        # Anything unread in a REPLY-mode chat that has no draft yet.
        drafted_for = {e["recipient"] for e in pending_drafts(config)}
        waiting = [
            {"chat": chat.name, "unread": chat.unread_count,
             "time": chat.timestamp, "preview": chat.preview}
            for chat in list_unread(page)
            if allow.can_reply(chat.name) and chat.name not in drafted_for
        ]
        _record_inbox(config, waiting)
        result["waiting_for_draft"] = waiting

        # Hand each waiting chat to the drafter, which has no way to send.
        queued = {item.chat for item in read_queue(config)}
        for chat in waiting:
            if chat["chat"] in queued:
                continue
            captured = read_chat(page, chat["chat"])
            if not captured.get("ok"):
                continue
            item = QueueItem(
                queue_id=_queue_id(chat["chat"]),
                chat=chat["chat"],
                messages=captured["messages"][-12:],
            )
            write_queue_item(config, item)
            result["actions"].append(
                {"queued_for_drafting": item.queue_id, "chat": item.chat}
            )
    return result

SUMMARY_PREFIX = "sum-"


def _take_groupsum_request(page, config: Config, result: dict) -> bool:
    """Look for a standalone GROUPSUM in the self-chat and consume it once.

    Consuming matters: without it, the same message would re-trigger on every
    tick and reopen every monitored group -- spending read receipts on dozens of
    people repeatedly.
    """
    from .selfchat import read as read_selfchat

    journal = Journal(journal_path(config))
    try:
        messages = read_selfchat(page, limit=25)
    except Exception:
        return False
    for message in messages:
        if not is_groupsum(message.text):
            continue
        if journal.command_seen(message.msg_id):
            continue
        journal.record_command(message.msg_id, Decision.NONE, "GROUPSUM")
        result["actions"].append({"groupsum": "requested"})
        return True
    return False


def _collect_group_messages(page, config: Config, result: dict) -> str | None:
    """Open every SUMMARIZE chat and queue their messages. Returns queue id."""
    # Check BEFORE opening anything: reading these chats sends read receipts
    # and clears unread badges, so a redundant pass has a real cost.
    outstanding = [i for i in read_queue(config)
                   if i.queue_id.startswith(SUMMARY_PREFIX)]
    if outstanding:
        result["actions"].append({"groupsum": "a digest is already in progress",
                                  "queue_id": outstanding[0].queue_id})
        return None

    allow = Allowlist(allowlist_path(config))
    targets = allow.summarize_chats()
    if not targets:
        result["actions"].append({"groupsum": "no chats in summarize mode"})
        return None

    marks = read_watermarks(config)
    chats = []
    unchanged = []
    for entry in targets:
        captured = read_chat(page, entry.name)   # opens it; receipts are sent
        if not captured.get("ok"):
            result["actions"].append(
                {"groupsum_skipped": entry.name, "reason": captured.get("reason")}
            )
            continue
        # Only what has arrived since this chat was last summarised. Without
        # this every digest restates the previous one and buries the new part.
        fresh = since(captured["messages"], marks.get(entry.name))
        if not fresh:
            unchanged.append(entry.name)
            continue
        chats.append({"chat": entry.name, "messages": fresh[-40:]})

    if unchanged:
        result["actions"].append({"groupsum_unchanged": unchanged})

    if not chats:
        # Say so: silence after a GROUPSUM is indistinguishable from a failure.
        note = ("📋 Nothing new in the monitored groups since the last digest.\n"
                + "\n".join(f"· {e.name}" for e in targets))
        # "monitored" is doing a lot of work in that sentence. A chat the
        # allowlist does not cover is invisible here, so a newly joined group
        # sitting on nine unread reads as a broken digest. Name them.
        missing = _unmonitored_unread(page, allow)
        if missing:
            note += ("\n\nNOT monitored, and unread right now:\n"
                     + "\n".join(f"· {name} ({count})" for name, count in missing)
                     + "\n\nTo include one:\n"
                     '  uv run wa-agent allow "<name>" --group --mode summarize')
        try:
            post_note(page, note)
            result["actions"].append({"groupsum": "nothing new",
                                      "unmonitored_unread": [m[0] for m in missing]})
        except Exception as exc:
            result["actions"].append({"groupsum_note_failed": str(exc)})
        return None

    queue_id = f"{SUMMARY_PREFIX}{int(datetime.now(timezone.utc).timestamp())}"
    item = QueueItem(queue_id=queue_id, chat="__summary__", messages=chats)
    write_queue_item(config, item)
    result["actions"].append(
        {"groupsum_queued": queue_id,
         "chats": {c["chat"]: len(c["messages"]) for c in chats}}
    )
    return queue_id


def _unmonitored_unread(page, allow: Allowlist) -> list[tuple[str, int]]:
    """Unread chats the allowlist does not cover, newest badge first.

    Read from the chat LIST only -- no chat is opened, so this costs no read
    receipts. It exists so "nothing new in the monitored groups" cannot be
    mistaken for "nothing new anywhere".
    """
    try:
        chats = list_unread(page)
    except Exception:
        return []
    return [(c.name, c.unread_count) for c in chats if not allow.allows(c.name)]


def _run_for(item: QueueItem, config: Config, outbox) -> dict:
    """Dispatch one queue item to the right LLM run."""
    if outbox.exists():
        return {"queue_id": item.queue_id, "skipped": "already drafted"}
    if item.queue_id.startswith(SUMMARY_PREFIX):
        # Summaries get a strictly narrower run: no MCP and no tools at all, so
        # nothing is reachable while reading untrusted group chatter.
        return run_summarizer(config, item)
    return run_drafter(item, config)


def _fingerprint(text: str) -> str:
    """Letters, digits and single spaces only, casefolded.

    WhatsApp renders emoji as <img> elements, and `inner_text` does not include
    their alt text -- a note posted as "📋 GROUP DIGEST 13:16" reads back as
    "GROUP DIGEST 13:16". Matching on the raw text therefore never succeeded,
    so `post_note` declared every digest undelivered and the next tick posted it
    again: ten identical digests in one afternoon. Comparing on what actually
    survives the round trip is the fix; punctuation goes too, since "·" and "—"
    are no more guaranteed than the emoji.
    """
    kept = (ch if (ch.isalnum() or ch.isspace()) else " " for ch in text)
    return " ".join("".join(kept).split()).casefold()


def post_note(page, text: str, settle_s: float = 12.0) -> str:
    """Post a note into the self-chat and CONFIRM it is really there.

    Two failures made this necessary, both seen in production:

    * `selfchat.post` RETURNS a SendResult and does not raise when a send is
      refused after the click. Ignoring it marked a digest delivered while
      nothing was posted.
    * Even on a genuine "sent", the message is not necessarily transmitted when
      the call returns. `_post_phase` closes the browser as soon as posting is
      done, and a context torn down that quickly dropped the message on the
      floor -- `summary_posted` in the log, nothing in the chat. Drafts never
      hit this because `propose` reads its message back to find `marker_id`,
      which both proves delivery and holds the page open.

    So the note is read back until it appears. Returns its message id.
    """
    from .selfchat import post as post_selfchat, read as read_selfchat

    outcome = post_selfchat(page, text)
    if outcome is not None:
        if getattr(outcome, "dry_run", False):
            raise RuntimeError("note was a dry run; nothing was posted")
        if not getattr(outcome, "ok", True):
            raise RuntimeError(f"note not posted: {getattr(outcome, 'detail', '')}")

    needle = _fingerprint(text)[:60]
    deadline = time.monotonic() + settle_s
    while True:
        try:
            for message in reversed(read_selfchat(page, limit=8)):
                if needle and needle in _fingerprint(message.text or ""):
                    return message.msg_id
        except Exception:
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "note was reported sent but never appeared in the self-chat "
                f"within {settle_s:g}s -- treating it as not delivered"
            )
        page.wait_for_timeout(500)


def _post_ready_summaries(page, config: Config, result: dict) -> int:
    """Post finished group digests into the self-chat as plain notes.

    A digest is not a draft: it has no recipient and needs no approval, because
    it goes nowhere but your own notes. Routing it through the draft machinery
    would put an approvable "OK" on something that must never be sendable.
    """
    posted = 0
    for item in read_queue(config):
        if not item.queue_id.startswith(SUMMARY_PREFIX):
            continue
        try:
            submission = take_submission(config, item.queue_id)
        except ContractError as exc:
            result["actions"].append(
                {"queue_id": item.queue_id, "rejected_summary": str(exc)}
            )
            clear_item(config, item.queue_id)
            continue
        if submission is None:
            continue
        stamp = datetime.now(timezone.utc).astimezone().strftime("%H:%M")
        note = (f"📋 GROUP DIGEST {stamp}\n\n{submission.body}\n\n"
                + "\n".join(f"· {src}" for src in submission.sources))
        try:
            post_note(page, note)
        except Exception as exc:
            # Leave the item queued and the watermarks untouched: a digest that
            # did not arrive must be retried, never marked delivered.
            result["actions"].append(
                {"queue_id": item.queue_id, "summary_post_failed": str(exc)}
            )
            continue
        # Only now is it safe to mark these messages seen: the user has them.
        # Advancing at capture time would have lost everything covered by the
        # five posts that silently failed on 2026-09-01.
        advance(config, item.messages)
        clear_item(config, item.queue_id)
        result["actions"].append({"summary_posted": item.queue_id})
        posted += 1
    return posted


def _post_ready_drafts(page, config: Config, result: dict) -> int:
    """Post any drafts the drafter has finished. Returns how many."""
    before = len(result["actions"])
    for item in read_queue(config):
        if item.queue_id.startswith(SUMMARY_PREFIX):
            continue          # summaries are posted as notes, not drafts
        try:
            submission = take_submission(config, item.queue_id)
        except ContractError as exc:
            # A file that tried to steer routing, or is malformed. Stop on
            # it loudly rather than sanitise and continue.
            result["actions"].append(
                {"queue_id": item.queue_id, "rejected_outbox": str(exc)}
            )
            clear_item(config, item.queue_id)
            continue
        if submission is None:
            continue
        try:
            # Recipient comes from the QUEUE ITEM, never from the outbox.
            pending_draft = propose(
                page, config, item.chat, submission.body,
                sources=submission.sources,
                quoted=(item.messages[-1]["text"] if item.messages else ""),
            )
        except Exception as exc:
            result["actions"].append(
                {"queue_id": item.queue_id, "propose_failed": str(exc)}
            )
            continue
        clear_item(config, item.queue_id)
        result["actions"].append({
            "queue_id": item.queue_id,
            "drafted": pending_draft.draft.draft_id,
            "recipient": item.chat,
        })
    return len(result["actions"]) - before


def _count_context_failure(config: Config, item: QueueItem) -> None:
    """Keep the item queued, but count the failure.

    The message stays in the queue precisely so a passing outage costs nothing
    but a delay. The counter exists so a long one does not sit here silently.
    """
    item.attempts = int(item.attempts) + 1
    try:
        write_queue_item(config, item)
    except ContractError:
        pass


def _report_stalled_items(page, config: Config, result: dict) -> None:
    """Say once, in the self-chat, that a reply cannot be drafted.

    Retrying forever with nothing shown is the failure mode this avoids: the
    incoming message has already been marked read, so from the other side it
    looks answered-and-ignored while the queue quietly spins.
    """
    for item in read_queue(config):
        if item.queue_id.startswith(SUMMARY_PREFIX):
            continue
        if item.stalled_notified or item.attempts < CONTEXT_STALL_ATTEMPTS:
            continue
        note = (
            f"⚠️ I cannot draft a reply to {item.chat}: Gmail and Calendar have "
            f"been unreachable for {item.attempts} attempts, and I will not "
            f"answer from memory.\n\nThe message is still queued and I will "
            f"draft it as soon as access returns. Nothing has been sent."
        )
        try:
            post_note(page, note)
        except Exception as exc:
            result["actions"].append({"stall_notice_failed": str(exc)})
            continue
        item.stalled_notified = True
        write_queue_item(config, item)
        result["actions"].append({"stalled": item.queue_id, "chat": item.chat})


def _rotation_blocks(page, config: Config, result: dict) -> bool:
    """Warn about the rotation deadline; report whether the tick must stop.

    Warnings go out while the session still works, because afterwards there is
    no self-chat to warn through. Stopping is opt-in (WA_ENFORCE_ROTATION):
    the clock is this project's own policy, and WhatsApp Web keeps working past
    it, so enforcing by default would strand a healthy agent.
    """
    state = read_state(config)
    if state is None:
        return False

    threshold = due_warning(config, state)
    if threshold is not None:
        try:
            post_note(page, render_warning(config, state))
        except Exception as exc:
            result["actions"].append({"rotation_warning_failed": str(exc)})
        else:
            mark_announced(config, state, threshold)
            result["actions"].append({"rotation_warning": f"{threshold:g}h left"})

    left = hours_left(state, config.rotate_after_hours)
    if left > 0:
        return False
    result["session_overdue_hours"] = round(-left, 2)
    if not config.enforce_rotation:
        return False
    result["blocked"] = (
        "session past the rotation policy — run `uv run wa-login` "
        "(set WA_ENFORCE_ROTATION=0 to keep running instead)"
    )
    return True


def _queue_id(chat: str) -> str:
    digest = hashlib.sha256(chat.encode("utf-8")).hexdigest()[:10]
    return f"{digest}-{int(datetime.now(timezone.utc).timestamp())}"


def _queue_revision(config: Config, entry: dict, instructions: str) -> dict:
    """Turn an EDIT into a fresh drafting job, with a bound on revisions."""
    draft_id = entry["draft_id"]
    revision = int(entry.get("revision", 0)) + 1
    retire_draft(config, draft_id, f"superseded by EDIT (revision {revision})")
    if revision > MAX_REVISIONS:
        return {"draft_id": draft_id, "edit_refused":
                f"revision cap {MAX_REVISIONS} reached; ask in a session instead"}
    item = QueueItem(
        queue_id=_queue_id(entry["recipient"]),
        chat=entry["recipient"],
        edit_instructions=instructions,
        previous_body=entry.get("body", ""),
        revision=revision,
    )
    write_queue_item(config, item)
    return {"draft_id": draft_id, "edit_queued": item.queue_id, "revision": revision}
