"""`wa-agent` -- the primitives Claude Code drives.

Every subcommand prints JSON on stdout so the orchestrator can consume it.
Sending is dry-run unless --live is passed.
"""

from __future__ import annotations

import argparse
import json
import sys

from .agent import (
    agent_dir,
    retire_draft,
    pending_drafts,
    list_chats,
    read_chat,
    allowlist_path,
    deliver,
    list_unread,
    poll,
    propose,
)
from .allowlist import Allowlist
from .compose import SendRefused
from .config import WHATSAPP_URL, load_config
from .interstitials import dismiss
from .page_state import PageState, wait_for_state
from .session import first_page, persistent_context

PAGE_READY_TIMEOUT_S = 30.0


def emit(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


# Automated ticks run minimised so they do not steal focus. Login and
# debugging pass visible=True, because you need to see the QR / the page.
QUIET = True


def _with_page(fn, quiet: bool = True):
    config = load_config()
    with persistent_context(config.profile_dir, headless=False, quiet=quiet) as context:
        page = first_page(context)
        page.goto(WHATSAPP_URL, wait_until="domcontentloaded")
        if wait_for_state(page, PAGE_READY_TIMEOUT_S) is not PageState.LOGGED_IN:
            emit({"ok": False, "reason": "not logged in — run `uv run wa-login`"})
            return 1
        dismiss(page)
        return fn(page, config)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wa-agent",
        description="WhatsApp reply agent primitives. Sending is dry-run by default.",
    )
    parser.add_argument("--visible", action="store_true",
                        help="show the browser window (default: minimised)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="show the allowlist")
    p_allow = sub.add_parser("allow", help="add a chat to the allowlist")
    p_allow.add_argument("chat")
    p_allow.add_argument("--group", action="store_true", help="mark as a group chat")
    p_allow.add_argument("--note", default="")
    p_allow.add_argument("--mode", choices=("reply", "summarize"), default="reply",
                         help="reply: drafts and sends. summarize: digest only, "
                              "can never be replied to")
    p_deny = sub.add_parser("deny", help="remove a chat from the allowlist")
    p_deny.add_argument("chat")

    sub.add_parser("unread", help="unread chats (reads the list only)")

    p_chats = sub.add_parser("chats", help="list chat names, to copy into the allowlist")
    p_chats.add_argument("--search", default="", help="filter by substring")

    sub.add_parser("tick", help="one unattended cycle (used by the launchd daemon)")

    p_pend = sub.add_parser("pending", help="drafts still awaiting your decision")
    p_pend.add_argument("--chat", default="")

    p_drop = sub.add_parser("drop", help="withdraw a draft so it can never be approved")
    p_drop.add_argument("--draft-id", required=True)
    p_drop.add_argument("--reason", default="superseded")

    p_read = sub.add_parser("read", help="open ONE chat and read it (sends read receipts)")
    p_read.add_argument("--chat", required=True)
    p_read.add_argument("--yes", action="store_true",
                        help="required: confirms read receipts will be sent")

    p_prop = sub.add_parser("propose", help="post a draft to your self-chat")
    p_prop.add_argument("--chat", required=True)
    p_prop.add_argument("--body-file", required=True,
                        help="file holding the exact text to send")
    p_prop.add_argument("--source", action="append", default=[],
                        help="evidence used, e.g. 'calendar:1' (repeatable)")
    p_prop.add_argument("--quoted", default="")
    p_prop.add_argument("--ttl-hours", type=float, default=2.0)

    p_poll = sub.add_parser("poll", help="check your self-chat for a decision")
    p_poll.add_argument("--draft-id", required=True)

    p_send = sub.add_parser("send", help="deliver an approved draft")
    p_send.add_argument("--draft-id", required=True)
    p_send.add_argument("--live", action="store_true",
                        help="actually send; omit for a rehearsal")

    args = parser.parse_args(argv)
    config = load_config()

    def _with_page_q(fn):
        return _with_page(fn, quiet=not args.visible)

    if args.cmd in {"list", "allow", "deny"}:
        allow = Allowlist(allowlist_path(config))
        if args.cmd == "allow":
            entry = allow.add(args.chat, is_group=args.group, note=args.note, mode=args.mode)
            emit({"added": entry.name, "mode": entry.mode,
                  "audience": entry.audience()})
        elif args.cmd == "deny":
            emit({"removed": args.chat, "was_present": allow.remove(args.chat)})
        else:
            emit({"dir": str(agent_dir(config)),
                  "allowlist": [{"name": e.name, "mode": e.mode,
                                 "audience": e.audience(), "note": e.note}
                                for e in allow.entries()]})
        return 0

    if args.cmd == "unread":
        return _with_page_q(lambda page, cfg: (
            emit([{"name": c.name, "unread": c.unread_count,
                   "time": c.timestamp, "preview": c.preview}
                  for c in list_unread(page)]) or 0))

    if args.cmd == "chats":
        return _with_page_q(lambda page, cfg: (
            emit(list_chats(page, search=args.search)) or 0))

    if args.cmd == "tick":
        from .tick import run_tick

        outcome = run_tick(config)
        emit(outcome)
        return 0 if not outcome.get("error") else 1

    if args.cmd == "pending":
        emit(pending_drafts(config, chat=args.chat))
        return 0

    if args.cmd == "drop":
        emit(retire_draft(config, args.draft_id, args.reason))
        return 0

    if args.cmd == "read":
        if not args.yes:
            emit({"ok": False, "reason": "opening a chat sends read receipts; pass --yes"})
            return 2
        return _with_page_q(lambda page, cfg: (emit(read_chat(page, args.chat)) or 0))

    if args.cmd == "propose":
        body = open(args.body_file, encoding="utf-8").read()

        def _run(page, cfg):
            try:
                pending = propose(page, cfg, args.chat, body,
                                  sources=args.source, quoted=args.quoted,
                                  ttl_hours=args.ttl_hours)
            except SendRefused as exc:
                emit({"ok": False, "reason": str(exc)})
                return 1
            emit({"ok": True, "draft_id": pending.draft.draft_id,
                  "marker_id": pending.marker_id,
                  "recipient": pending.draft.recipient,
                  "expires_at": pending.draft.expires_at.isoformat()})
            return 0

        return _with_page_q(_run)

    if args.cmd == "poll":
        def _run(page, cfg):
            cmd = poll(page, cfg, args.draft_id)
            emit({"decision": cmd.decision.value, "draft_id": cmd.draft_id,
                  "instructions": cmd.instructions, "reason": cmd.reason})
            return 0
        return _with_page_q(_run)

    if args.cmd == "send":
        def _run(page, cfg):
            result = deliver(page, cfg, args.draft_id, live=args.live)
            emit(result)
            return 0 if result.get("ok") else 1
        return _with_page_q(_run)

    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
