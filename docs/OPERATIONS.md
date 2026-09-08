# Operating wa-assist

What the daemon does, how to read what it says, and what to do when it goes
wrong. If you are setting it up for the first time, start with the
[README](../README.md).

---

## One tick, in order

The daemon runs `wa-agent tick` every `StartInterval` seconds (120 in the
shipped template). Each tick has three phases,
and the split matters: an LLM run can take five minutes, and holding the
browser lock through it starves every other tick.

**1. Browser phase — holds the profile lock**
Opens WhatsApp Web, then in order: acts on decisions for pending drafts;
answers commands aimed at drafts that are no longer live; posts finished drafts
and digests; consumes a `GROUPSUM` request; lists unread allowlisted chats; and
captures any that need a reply into `queue/`.

**2. Drafting phase — lock released**
Runs Claude for each queued item. No WhatsApp access is needed, so nothing else
is blocked meanwhile. One lock *per queue item* stops the next tick starting a
second paid run for the same message.

**3. Post phase — retakes the lock**
Posts what was just drafted, rather than leaving it until the next tick.

Only phase 1 and 3 touch WhatsApp. If the session is gone, phase 1 reports
`blocked` and the tick stops there — there is no point paying for a draft that
cannot be posted.

---

## Reading `daemon.log`

One JSON object per tick, appended. `jq` helps:

```bash
jq -c 'select(.actions|length>0) | {at, actions}' .wa-agent/daemon.log | tail -20
```

### Normal progress

| key | meaning |
|---|---|
| `queued_for_drafting` | a chat was captured and handed to the drafter |
| `drafted` | a draft was posted to your self-chat, with its id |
| `sent` | an approved draft was delivered |
| `rejected` | you said `NO` |
| `edit_queued`, `revision` | you said `EDIT`; a redraft is queued |
| `summary_posted` | a group digest reached the self-chat |
| `groupsum_queued` | which chats and how many new messages each |
| `groupsum_unchanged` | groups with nothing new since the last digest |
| `groupsum_window_gap` | a digest could not cover everything; the note says so too |
| `groupsum: nothing new` | nothing anywhere; no paid run was made |
| `unmonitored_unread` | unread chats the allowlist does not cover |
| `rotation_warning` | a session-expiry warning was posted |
| `stale_command` | a command named a sent/withdrawn/expired draft |

### Things that need you

| key | what it means | what to do |
|---|---|---|
| `blocked: not logged in` | the session is gone | `uv run wa-login` and scan |
| `needs_attention: ambiguous` | a command was not an exact `OK`/`NO`/`EDIT` | retype it as a whole message |
| `context_unavailable` | `workspace-mcp` unreachable, OR its tools failed auth; **no draft is published** | if it says *a tool call failed*, re-authorise Google — see below |
| `not_queued` | the newest message is your own, or has no text (a photo) | nothing to answer; it re-queues when they write again |
| `stalled` | a reply failed to draft six times running | check MCP; the message is still queued |
| `edit_refused` | the 5-revision cap was reached | redraft in an interactive session |
| `sent: {ok: false}` | a pre-send check refused; the self-chat says why | usually an edited source message |
| `session_overdue_hours` | past the rotation policy | `uv run wa-login --reset` |

### Things that are usually fine

| key | meaning |
|---|---|
| `skipped: profile in use by another process` | you were running `wa-login` or a manual command |
| `skipped: already being drafted` | a previous tick's LLM run is still going |
| `post_deferred` | the profile was busy; it posts next tick |

### Failures worth investigating

`propose_failed`, `summary_post_failed`, `post_failed`, `poll_failed`,
`rejected_outbox`, `rejected_summary`, `stale_notice_failed`,
`rotation_warning_failed`, `edit_ack_failed`.

`propose_failed` naming a message id means a draft reached your self-chat but
was never armed — `OK` on it does nothing, and it is not in `pending`. The
queue item is kept and retried, so it resolves itself; the orphan text stays in
the chat and is only clutter. If it repeats every tick, the send is being
refused rather than lost: check `daemon.err.log`.

A `summary_post_failed` or `propose_failed` mentioning *acknowledged* means
WhatsApp never took the message. It rendered in the browser and stayed
`Pending`; had the daemon trusted that, the log would say delivered and your
phone would show nothing. The item is kept and retried.

`summary_post_failed` is the one to take seriously: it means a digest was
generated and could not be delivered. The queue item and its watermarks are
deliberately left untouched, so the next `GROUPSUM` retries it. Nothing is
marked seen until you actually have it.

---

## State files

Everything lives in `.wa-agent/`, mode `0700`, gitignored.

| file | what it holds | safe to delete? |
|---|---|---|
| `allowlist.json` | which chats are watched, and in which mode | no — you would lose your config |
| `context.json` | the Google account and calendars the drafter may read | no |
| `style.json` | house style injected into every prompt | yes, style reverts to default |
| `digest_seen.json` | every message id already reported, per group | yes, but the next digest repeats everything |
| `journal.jsonl` | append-only record of every draft, command, send | **no** — this is what stops a double send |
| `rotation.json` | which expiry warnings have been given | yes, warnings may repeat |
| `inbox.json` | last tick's view of what is waiting | yes |
| `queue/`, `outbox/` | work in flight | only when empty |
| `draft_XXX.json` | one file per draft ever posted | yes, once the id is retired or sent |
| `*.lock` | `flock` files | yes, if no process holds them |
| `daemon.log` | one JSON object per tick | yes, it only grows |

`.wa-profile/` is separate and is a **live WhatsApp credential**. Treat it like
an SSH key.

It must not sit in a synced folder. iCloud, Dropbox, OneDrive, Google Drive and
the `~/Library/CloudStorage` mounts are refused at startup — sync uploads a
live credential continuously, and defeats the file mode, FileVault and rotation
all at once. If you keep the checkout in one, set `WA_PROFILE_DIR` elsewhere.

Time Machine is worth checking too, since it is not refused, only worth
avoiding:

```bash
tmutil isexcluded .wa-profile        # [Included] means it gets backed up
sudo tmutil addexclusion .wa-profile
```

### Housekeeping

Nothing prunes `draft_*.json`, `run-*.lock` or `daemon.log` today, so they grow
without bound. They are small, but on a long-running install:

```bash
# stale per-item locks nothing holds
find .wa-agent -name 'run-*.lock' \
  -exec sh -c 'lsof "$1" >/dev/null 2>&1 || rm -f "$1"' _ {} \;

# drafts older than a week (the journal keeps the audit trail)
find .wa-agent -name 'draft_*.json' -mtime +7 -delete
```

Do **not** delete `journal.jsonl`. It is the only thing preventing a draft
being sent twice.

---

## The browser

Every browser step runs headless, so the daemon is invisible. The one
exception is `wa-login`, which must show you a QR.

This rests on one thing: WhatsApp Web gates on the User-Agent. Headless
Chromium advertises `HeadlessChrome/…`, and WhatsApp answers that with a
browser-support notice instead of the app — the page renders, it just is not
the page you wanted. `session.CHROME_UA` is sent on every launch, headed and
headless, so a login and a tick look like the same browser to Meta.

### If Meta changes the sniff

The symptom is specific and easy to misread: **every tick logs
`blocked: not logged in` while the session is genuinely fine.** `wa-login
--status` will report the same, because it runs headless too and sees the same
notice. A real logout looks identical from the outside.

To tell them apart, and to keep working meanwhile:

```bash
WA_HEADED=1 uv run wa-login --status
```

If that says `LOGGED IN`, the session is fine and the UA is the problem. Put
`WA_HEADED=1` in the plist's `EnvironmentVariables` and the daemon goes back to
a real minimised window — intrusive, but working — until `CHROME_UA` is
updated.

Note that a minimised window is not an invisible one. It is created frontmost
and minimised a moment later, so every launch takes focus. At a 120s interval
and up to two launches per productive tick, that is roughly thirty focus
steals an hour, and on macOS it reshuffles Spaces. That is why it is the
fallback and not the default.

### Watching what it does

```bash
uv run wa-agent --visible unread     # open a real window for one command
uv run wa-agent --visible tick
```

---

## Known limitations

These are current behaviour, not bugs with a fix pending. They are listed so a
surprise is a recognised one.

### A digest repeating things you have already read

The record is a set of reported message ids per group, and it only ever grows,
so this should not happen. It did, from a single watermark that `advance`
assigned rather than advanced: a capture that had lost its tail wrote an OLD
id back as "the last thing reported", and every digest after it re-opened
everything that followed. Symptoms were quiet groups appearing in digest after
digest, and content the user had already read three times.

If a record is ever rewound again, it cannot be repaired from the ids it kept
— they are the wrong ones. Draw a line instead:

```bash
uv run wa-agent digest-catchup
```

That marks everything currently visible in the summarize groups as already
reported, so only messages arriving afterwards are digested. It opens those
chats, but they are already read, so it costs no new read receipts. Anything
recent that was genuinely never reported is given up in exchange — read it in
WhatsApp directly.

### A gap on every group at once is a bug, not a busy group

`groupsum_window_gap` naming **all** your groups in one digest does not mean
they all got busy. It means the capture lost its own tail and the watermark
fell outside it, so everything read was treated as new. The digest that
follows repeats things you have already been told.

That happened on 2026-09-06 and is fixed — `capture_chat` reads bottom-first
through `messages.read_window` — but the shape is worth recognising: one or
two groups gapping is plausible, all of them at once is not.

### A very busy group can still outrun its watermark — but it says so

A `summarize` chat is captured 60 rows deep, and up to 120 messages reach one
digest. Both are generous enough that a normal group never approaches them. A
group that does — hundreds of messages between two digests — pushes its own
watermark out of the captured window, and the messages between the mark and
the top of that window are not covered.

That case is now **reported rather than hidden**. The digest itself carries:

```
⚠️ INCOMPLETE — not everything was covered:
· <group> — the last summarised message is no longer in view; anything
  between it and these is not covered
```

and the tick logs `groupsum_window_gap` with the chat and the reason. Nothing
is lost from WhatsApp — open the chat to read the rest — and asking for
`GROUPSUM` more often prevents it recurring.

The watermark still advances when this happens. What *was* covered is covered,
and refusing to advance would re-summarise it for ever without recovering the
part already out of reach.

### Reading a chat spends its read receipt, at capture time

Before any draft exists. A `NO #XXX` cannot take it back, and because the chat
is no longer unread nothing re-queues it. Re-open the topic by sending a new
message; that makes the chat unread again and it queues normally.

### The daemon cannot rotate its own session

Linking needs a QR scanned from the phone. It warns in the self-chat at 24h, 6h
and 2h of remaining life, then falls back to a macOS notification once the
self-chat itself is gone with the session. The 14-day clock is this project's
policy, not WhatsApp's: past the deadline the daemon keeps working and only
warns, unless `WA_ENFORCE_ROTATION=1`.

It was 24h until 2026-09-07, set while a prompt-injected drafting run could
still write into the package the daemon executes. With that closed, a policy
demanding a QR scan every morning was buying very little for what it cost.

### Rotation does not revoke anything by itself

Worth being exact about, because the name oversells it and an earlier version
of this document did too.

| | at the deadline | is the session still usable by a copied profile? |
|---|---|---|
| default | warns, keeps working | **yes** |
| `WA_ENFORCE_ROTATION=1` | tick reports `blocked`, stops | **yes** |
| running `wa-login` | unlinks, wipes, re-links | no |

`log_out()` has exactly one caller — `wa-login`. The daemon never unlinks. So
`WA_ENFORCE_ROTATION` stops your agent while leaving the device linked and the
session valid: it costs availability and reduces exposure by nothing. It is a
"stop nagging me by stopping" switch, not a security control.

What actually bounds exposure is a person running `uv run wa-login --reset`.
The policy sends a reminder; it does not act. Read the interval as "how often
am I reminded", not "how long a leaked profile stays valid".

An auto-unlink at the deadline — the daemon driving the same logout flow — is
the variant that would make the name true. It is not implemented. Its cost is
availability, and not a small one: fired while you are away, the agent is off
until you are back with your phone, and it cannot tell you in the self-chat
because unlinking is what removes that channel.

Note that a plain `wa-login` **before** the deadline is a no-op — it finds a
valid session and prints "within policy". Only `--reset` (or a login after
expiry) unlinks, wipes and restarts the clock.

### Concurrency is one profile at a time

`wa-login` takes the profile lock for its whole run and waits up to 240s for
the daemon to finish a tick before refusing. Ticks that collide log
`skipped: profile in use by another process` and simply try again next
interval. This is why an interval below ~120s starts making interactive
commands wait.

---

## Troubleshooting

### No draft appeared for an incoming message

Work down this list; each step is visible in `daemon.log`.

1. **Is the chat allowlisted in `reply` mode?** `uv run wa-agent list`.
   `summarize` chats are never replied to, by design.
2. **Is the session alive?** `uv run wa-login --status` — this opens the page
   and checks WhatsApp itself. The local record can say "valid" while WhatsApp
   has dropped the link.
3. **Did the tick see it?** Look for `queued_for_drafting`.
4. **Did the drafter run?** `context_unavailable` means MCP was down and
   nothing was spent. `ok: true` followed by `drafted` means it worked.
5. **Was the chat already read?** Opening a chat clears its unread state. If
   you read it on your phone first, the daemon will not see it. Send another
   message to re-trigger.

### No digest after `GROUPSUM`

- `groupsum: nothing new` means exactly that — the monitored groups have said
  nothing since the last digest. The note lists which groups were checked and
  names any unread chat that is *not* monitored.
- A group you recently joined is invisible until you add it:
  `uv run wa-agent allow "<name>" --group --mode summarize`.
- `summary_post_failed` means it was generated but not delivered; it retries
  automatically on the next `GROUPSUM`.

### "It answered a question I never asked"

Almost certainly the message was **edited**. WhatsApp lets the sender change a
message after it arrives and replaces the text in place, so your chat shows
only the new wording — while the draft was written against what was there when
the daemon read it, minutes earlier. It looks like invention and is not.

`deliver` now re-reads the chat before sending and refuses if the message the
draft answers is no longer there word for word, saying so in the self-chat.
The draft is dead at that point: send a new message in that chat if you still
want a reply.

### My approval did nothing

- The whole message must be the command. `OK #ABC but shorter` is ambiguous
  and never sends.
- A Russian-layout `ОК` (Cyrillic О К) **is** accepted — it looks identical to
  the Latin one, so it is treated as an approval.
- If the draft was superseded, sent or expired, you now get a note saying so.
- Drafts expire two hours after posting.

Two fixed bugs produced exactly this symptom, both silent, and they are worth
recognising if anything like them returns:

- **The read could not see the newest messages.** WhatsApp virtualises the
  message list at both ends: rows scrolled away from stay in the DOM with
  empty text and were dropped as blanks. Scrolling to the top to load history
  therefore discarded the bottom — where every command arrives. `read_after`
  could not find the draft it was told to start from, returned nothing by its
  own safety rule, and an `OK #XXX` plainly in the chat did nothing at all.
  Reads now start at the bottom and merge each window as they scroll up.
- **The draft was never armed.** `propose` posted a draft and read it back
  once, immediately, before WhatsApp had transmitted it — so it raised, and
  the draft sat in the self-chat with nothing to approve. Those show as
  `propose_failed` in the log and, unlike a real draft, do not appear in
  `uv run wa-agent pending`.

`pending` is the authority on what is actually approvable. A draft in the
self-chat that is not listed there is inert, whatever it looks like.

### `workspace-mcp` keeps failing to connect

The drafting run is killed at the handshake, before any tokens are spent, and
the message stays queued. Check the server starts by hand:

```bash
uvx workspace-mcp --read-only --tools gmail calendar --help
```

Google OAuth tokens live in `~/.google_workspace_mcp/credentials/`. Deleting
them forces a fresh consent flow. The setup, and the four ways it goes wrong,
are in **[GOOGLE_SETUP.md](GOOGLE_SETUP.md)**.

**A connected server can still be unusable.** `status=connected` only means the
process answered; if the Google token has expired or been revoked, every call
comes back `Google Authentication Needed` while the handshake looks perfect.
That used to produce a draft built on nothing. The run is now killed on the
first such tool result and logged as
`context_unavailable: a tool call failed: authentication needed`.

To fix it, re-run the consent flow:

```bash
rm -rf ~/.google_workspace_mcp/credentials
```

then start any interactive Claude session in this directory and call a
calendar tool once — it will print an authorisation URL to open. Until that is
done, replies stay queued and nothing is drafted, which is the intended
behaviour.

### The daemon is running but doing nothing

**Check the `error` key first.** A tick that dies early still logs an entry
with an empty `actions` list, so a daemon that is failing every cycle looks
identical to an idle one:

```bash
jq -c 'select(.error) | {at, error}' .wa-agent/daemon.log | tail -5
```

Do not filter the log down to `{at, actions}` while diagnosing — that is
exactly what hides this. One such failure ran for half an hour looking like a
quiet daemon while every GROUPSUM went unanswered.

```bash
launchctl print gui/$(id -u)/<your-label> | grep -E 'state|runs|last exit'
tail -5 .wa-agent/daemon.err.log
```

`runs` should climb every `StartInterval` seconds (120 by default). If `last exit code` is non-zero, the stderr log
has the traceback. Remember launchd caches the plist: after editing it you must
`bootout` then `bootstrap`.

---

## Latency, and the interval

Measured over 500 ticks on one machine:

| stage | cost |
|---|---|
| waiting to be noticed | 0 to one interval, **half of it on average** |
| browser open + WhatsApp load | ~6s, twice per productive tick |
| opening a chat | ~1.4s |
| the drafting run itself | 20 to 60s |

These were measured with a headed browser, before the switch to headless, and
have not been re-measured since.

The interval dominates everything else, so it is the only knob worth turning
first. An idle tick costs about 20 seconds, which sets the price:

| `StartInterval` | average wait before a message is noticed | share of time with a browser open |
|---|---|---|
| 300 | 150s | ~8% |
| 180 | 90s | ~14% |
| **120** (default) | **60s** | **~20%** |
| 60 | 30s | ~40% |

Below about 120 the daemon holds the profile lock so much that interactive
commands and `wa-login` start waiting behind it. Change it in the plist, then
`bootout` and `bootstrap` — launchd caches the old value otherwise.

A draft that lands in the same tick that noticed the message takes roughly
60-90 seconds end to end; one that just misses a tick takes an interval longer.

---

## Updating

```bash
git pull
uv sync                     # dependencies may have changed
uv run pytest -m "not browser"
launchctl bootout   gui/$(id -u)/<your-label>
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<your-label>.plist
```

The daemon imports from the working tree, so code changes take effect on the
next tick without a reload — but a plist change does not, and a dependency
change needs `uv sync`.

---

## Uninstall

```bash
launchctl bootout gui/$(id -u)/<your-label>
rm ~/Library/LaunchAgents/<your-label>.plist
```

Then, on your phone: **WhatsApp → Settings → Linked Devices → log out** of the
Chrome entry. Finally:

```bash
rm -rf .wa-profile .wa-state .wa-agent
```

Unlinking on the phone is the step that actually revokes access. Deleting the
profile only removes your local copy of the credential.
