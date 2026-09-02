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
| `groupsum: nothing new` | nothing anywhere; no paid run was made |
| `unmonitored_unread` | unread chats the allowlist does not cover |
| `rotation_warning` | a session-expiry warning was posted |
| `stale_command` | a command named a sent/withdrawn/expired draft |

### Things that need you

| key | what it means | what to do |
|---|---|---|
| `blocked: not logged in` | the session is gone | `uv run wa-login` and scan |
| `needs_attention: ambiguous` | a command was not an exact `OK`/`NO`/`EDIT` | retype it as a whole message |
| `context_unavailable` | `workspace-mcp` was not connected; **no tokens were spent** | usually transient; if it persists see below |
| `stalled` | a reply could not be drafted for many attempts | check MCP; the message is still queued |
| `edit_refused` | the 5-revision cap was reached | redraft in an interactive session |
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
| `digest_seen.json` | last summarised message per group | yes, but the next digest repeats itself |
| `journal.jsonl` | append-only record of every draft, command, send | **no** — this is what stops a double send |
| `rotation.json` | which expiry warnings have been given | yes, warnings may repeat |
| `inbox.json` | last tick's view of what is waiting | yes |
| `queue/`, `outbox/` | work in flight | only when empty |
| `draft_XXX.json` | one file per draft ever posted | yes, once the id is retired or sent |
| `*.lock` | `flock` files | yes, if no process holds them |
| `daemon.log` | one JSON object per tick | yes, it only grows |

`.wa-profile/` is separate and is a **live WhatsApp credential**. Treat it like
an SSH key.

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

### My approval did nothing

- The whole message must be the command. `OK #ABC but shorter` is ambiguous
  and never sends.
- A Russian-layout `ОК` (Cyrillic О К) **is** accepted — it looks identical to
  the Latin one, so it is treated as an approval.
- If the draft was superseded, sent or expired, you now get a note saying so.
- Drafts expire two hours after posting.

### `workspace-mcp` keeps failing to connect

The drafting run is killed at the handshake, before any tokens are spent, and
the message stays queued. Check the server starts by hand:

```bash
uvx workspace-mcp --read-only --tools gmail calendar --help
```

Google OAuth tokens live in `~/.google_workspace_mcp/credentials/`. Deleting
them forces a fresh consent flow.

### The daemon is running but doing nothing

```bash
launchctl print gui/$(id -u)/<your-label> | grep -E 'state|runs|last exit'
tail -5 .wa-agent/daemon.err.log
```

`runs` should climb every 300s. If `last exit code` is non-zero, the stderr log
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
