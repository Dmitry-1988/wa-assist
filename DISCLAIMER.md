# Before you run this

Read this once, properly. It is short, and it is the part that cannot be
undone by uninstalling.

This is a personal experiment shared with friends, not a product. There is no
support, no warranty, and no guarantee it will not do something surprising
tomorrow. If any of the following is not acceptable to you, do not install it —
that is a completely reasonable conclusion.

---

## 1. Your WhatsApp account can be banned

This automates WhatsApp Web, which **violates WhatsApp's Terms of Service**.
Meta bans accounts for automation, and the ban is applied to the phone number,
not to a login. For most people that number *is* how their family reaches them.

There is no appeal process worth relying on, and no way to make this compliant:
the official WhatsApp Business API cannot read your personal chats or your
groups, so "do it properly instead" is not an available option.

**Run this only on an account you could genuinely afford to lose.**

## 2. It marks your friends' messages as read, before you have seen them

To draft a reply the daemon must open the chat, and opening a chat sends a read
receipt to whoever wrote to you. That happens at capture time — **before any
draft exists**, and before you decide whether you want one.

So the other person sees "read" while you may not have read anything. Rejecting
the draft does not take it back. If you rely on unread markers to keep track of
what you owe people, this will break that.

These are third parties. They did not agree to any of this, and they cannot
tell that a machine opened their message.

## 3. Setup needs a Google Cloud project of your own

About fifteen minutes in the Google Cloud Console, creating an OAuth client so
the drafter can read your mail and calendar:
[docs/GOOGLE_SETUP.md](docs/GOOGLE_SETUP.md). If that is more than you want to
do, this is the point to stop.

## 4. Your messages are sent to Anthropic

Every message in a chat you allowlist is sent to Claude to draft a reply, and
every message in a group you monitor is sent to Claude to summarise. Your Gmail
and Calendar are read too, over a read-only connection, to answer questions
from them.

That includes whatever your family and friends happen to write to you. Do not
allowlist a chat whose contents you would not put into a third-party service.

## 5. It costs money, per message

Each incoming message in an allowlisted chat is a paid Claude run, as is each
`GROUPSUM`. A busy group and a chatty week add up, and nothing here caps your
spend. Watch it for the first few days.

## 6. `.wa-profile/` is a live credential

It is a logged-in WhatsApp session. Anyone who copies that folder can read and
send as you, from their own machine, without your phone. Treat it exactly like
an SSH private key: do not put it in a synced folder (the code refuses the
common ones), do not put it in a backup, do not copy it to another machine.

## 7. It is not finished

The honest version: over a single day of use, this project produced repeated
digests, digests that omitted messages, approvals that were silently invisible
to the daemon, seven drafts that could never be approved, half an hour of ticks
that failed without saying so, and a draft that answered a question the sender
had since edited. Every one of those is fixed. The rate at which they were
*found* is the point.

You are an early user of something that has been stable for days, not years.
If it does something odd, assume it is a bug and say so.

---

## What it will not do

These are the parts that are designed not to fail, and are covered by tests:

- **Nothing is sent without you typing `OK #XXX` as a whole message.** Not on
  silence, not on "OK #XXX but shorter" — an unclear approval refuses.
- **The part that writes text cannot reach WhatsApp.** It has read-only Gmail
  and Calendar and nothing else: no filesystem, no shell, no way to send. It
  cannot approve its own draft or choose who to send to.
- **A group in `summarize` mode is never replied to**, only digested.
- **A draft is sent once.** An append-only journal records the attempt before
  the click, so a crash cannot produce a second send.

## If you want out

```bash
launchctl bootout gui/$(id -u)/<your-label>
rm ~/Library/LaunchAgents/<your-label>.plist
rm -rf .wa-profile .wa-state .wa-agent
```

Then, on your phone: **WhatsApp → Settings → Linked Devices → log out** of the
Chrome entry. That last step is the one that actually revokes access; deleting
the folder only removes your local copy.

---

MIT licensed, as-is, no warranty. See [LICENSE](LICENSE).
