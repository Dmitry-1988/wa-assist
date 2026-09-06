# wa-assist

A WhatsApp reply assistant that will not send anything without you saying so.

A launchd daemon drives WhatsApp Web through Playwright on a persistent
Chromium profile. When an allowlisted contact writes to you, a headless Claude
run drafts a reply using your Gmail and Calendar, and posts it into **your own
WhatsApp self-chat**. Nothing is delivered until you reply `OK #XXX` there.

```
Ann: what date did we drive up for Max's wedding? and did I ever pay you
     back for the parking?
        │
        ▼  daemon captures the chat, queues it
   Claude drafts, read-only Gmail + Calendar, no way to reach WhatsApp
        │  hands back text, and nothing else
        ▼  daemon posts it to YOUR self-chat
   🤖 DRAFT #47Q → Ann
   Sources:
     · Calendar "Max & Dana — wedding", Wed 3 June
     · Gmail 5 Jun: parking receipt, 8 shekels, discounted rate

   ── WILL SEND VERBATIM ──
   Wed 3 June. And yes, the 8 shekels came through that Friday.
   ────────────────────────

   OK #47Q  |  EDIT #47Q <changes>  |  NO #47Q
   expires 21:26
        │
        ▼  you type OK #47Q
   daemon sends it to Ann
```

Both halves of the question are answered and nothing else is. Where the answer
came from is in `Sources`, which you see when you approve it — the message
itself never says "I checked your calendar", because that is not how you text.

---

## Read this before you install it

**This automates WhatsApp Web, which is against WhatsApp's Terms of Service.**
Accounts have been banned for less. This is a personal experiment, not a
product; run it on an account you can afford to lose, and do not deploy it for
anyone who has not accepted that risk themselves.

**Reading a chat is irreversible and visible.** To draft a reply the daemon
must open the chat, which marks it read and sends read receipts to the sender —
*before* any draft exists. Rejecting the draft does not undo it, and the chat is
no longer unread, so nothing re-queues it. See `messages.py`.

**Every draft is a paid Claude run.** A message arriving in an allowlisted chat
costs an API call. So does each `GROUPSUM`.

**macOS only.** It depends on launchd and `osascript`.

---

## What it does and does not do

| | daemon (`wa-agent tick`) | drafter (`claude -p`) |
|---|---|---|
| WhatsApp | every action | **none** |
| Gmail / Calendar | none | read-only, via MCP |
| Filesystem | full | **none** |
| Composes text | never | yes |
| Chooses recipient | yes, from the queue item | **never** |

The drafter has no shell, no filesystem and no way to reach WhatsApp, so it
cannot post an approval or send its own draft. That is structural, not a rule
it is asked to follow — see [SECURITY.md](SECURITY.md), including a fixed
finding where it *could* write the code the daemon executes.

Chats are opt-in, per chat, in one of two modes:

- **`reply`** — drafts are written and, once you approve, sent.
- **`summarize`** — digest only. `propose` and `deliver` refuse any chat that is
  not in `reply` mode, so a group can never be replied to by accident.

---

## Setup

Requires [uv](https://docs.astral.sh/uv/) and the
[Claude Code CLI](https://docs.claude.com/en/docs/claude-code).

### 1. Install

```bash
uv sync
uv run playwright install chromium
```

### 2. Link WhatsApp

```bash
uv run wa-login
```

A Chromium window opens on WhatsApp Web with a QR code. On your phone:
**WhatsApp → Settings → Linked Devices → Link a device**. Leave *"Stay logged in
on this browser"* ticked or nothing persists. Close the window when the chat
list appears.

```bash
uv run wa-login --status     # checks WhatsApp itself, not just the local record
```

### 3. Give the drafter Gmail and Calendar

The drafter reads your mail and calendar through
[workspace-mcp](https://github.com/taylorwilsdon/google_workspace_mcp), which
needs a Google OAuth client (Desktop app) with the Gmail and Calendar APIs
enabled. Register the server with Claude Code **for this project directory**:

```bash
claude mcp add workspace-mcp --scope project \
  -e GOOGLE_OAUTH_CLIENT_ID=... \
  -e GOOGLE_OAUTH_CLIENT_SECRET=... \
  -e WORKSPACE_MCP_PORT=8000 \
  -e GOOGLE_OAUTH_REDIRECT_URI=http://localhost:8000/oauth2callback \
  -- uvx workspace-mcp --read-only --tools gmail calendar
```

`--read-only` matters: the drafter is granted only `search_gmail_messages`,
`get_events` and similar. It cannot send mail or edit your calendar.

A drafting run whose MCP handshake is not `connected` is **killed before it
spends a token** and the message stays queued — otherwise a five-minute outage
becomes a confident reply built on nothing.

### 4. Tell it whose calendars to read

```bash
cp context.example.json .wa-agent/context.json   # then edit
```

Without it the drafter refuses to run. That is deliberate: with Gmail working
but no calendars, it would answer availability questions from thin air.

### 5. Allow some chats

```bash
uv run wa-agent chats --search "Ann"          # exact names, to copy
uv run wa-agent allow "Ann" --mode reply
uv run wa-agent allow "Building" --group --mode summarize
uv run wa-agent list
```

### 6. Run the daemon

```bash
cp examples/com.example.wa-agent.plist ~/Library/LaunchAgents/
# edit WorkingDirectory and the uv path inside it, then:
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.wa-agent.plist
launchctl print gui/$(id -u)/com.example.wa-agent | grep state
```

The daemon runs Chromium headless, so nothing appears on your screen. launchd
caches the plist at bootstrap, so after editing it you must `bootout` and
`bootstrap` again. `wa-login` stays headed — you have to see the QR to scan
it — so linking still needs the Aqua GUI session.

---

## Using it

Everything happens in your self-chat ("Message yourself").

| you type | effect |
|---|---|
| `OK #XXX` | send it |
| `NO #XXX` | discard it — terminal, nothing re-queues |
| `EDIT #XXX: shorter, drop the prices` | redraft under a **new id** (max 5 revisions) |
| `GROUPSUM` | digest the `summarize` groups |

`EDIT` retires the original immediately: it can never be approved afterwards,
and the redraft arrives under a new id. Ids are never reused, so `OK #XXX`
always means one exact body even when an older draft is still visible above it.
You get an acknowledgement the moment an edit is accepted, and a note if you
command a draft that is already sent, withdrawn or expired.

Drafts expire two hours after posting.

The whole message must be the command. **`OK #XXX but shorter` is ambiguous and
never sends** — a caveat is not consent, and silence never is either. A Russian
keyboard's `ОК` (Cyrillic О К) is accepted; it looks identical on screen.

A digest covers only what has arrived since the last one posted, and says
"nothing new" rather than restating itself. If it could not reach everything —
a group that produced more between digests than one capture holds — it says
that too, naming the group, rather than reading as a complete account.

### Command line

```bash
uv run wa-login [--status|--quick|--reset]
uv run wa-agent list|allow|deny|chats|unread|pending|drop|read
uv run wa-agent tick                  # one cycle by hand
uv run pytest                         # 532 tests
uv run pytest -m "not browser"        # the fast subset
```

### Configuration

| variable | default | meaning |
|---|---|---|
| `WA_PROFILE_DIR` | `./.wa-profile` | Chromium user-data directory |
| `WA_STATE_DIR` | `./.wa-state` | where the rotation timestamp lives |
| `WA_ROTATE_AFTER_HOURS` | `24` | session rotation policy |
| `WA_ENFORCE_ROTATION` | off | stop the daemon once the session is over-age |
| `WA_DAEMON_LABEL` | `com.example.wa-agent` | your launchd label, for messages |
| `WA_HEADED` | off | force a real (minimised) window instead of headless |

Files in `.wa-agent/`: `allowlist.json`, `context.json`, `style.json` (house
style injected into every prompt), `digest_seen.json`, `rotation.json`,
`journal.jsonl`, `queue/`, `outbox/`, `daemon.log`. All gitignored,
`0700`/`0600`. What each holds, and which are safe to delete, is in
**[docs/OPERATIONS.md](docs/OPERATIONS.md)**.

Do not delete `journal.jsonl`: it is the only thing preventing a draft being
sent twice.

### When something looks wrong

**[docs/OPERATIONS.md](docs/OPERATIONS.md)** covers what each tick does, how to
read `daemon.log`, and what to do when a draft or digest does not appear. The
short version:

```bash
uv run wa-login --status          # asks WhatsApp, not just the local record
jq -c 'select(.actions|length>0)' .wa-agent/daemon.log | tail -20
tail -5 .wa-agent/daemon.err.log
```

The most common cause of "nothing happened" is a chat that was already read on
your phone — the daemon only sees unread chats — or a group that was never
added to the allowlist.

## Updating

```bash
git pull && uv sync
uv run pytest -m "not browser"
launchctl bootout   gui/$(id -u)/<your-label>
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<your-label>.plist
```

Code changes take effect on the next tick without a reload; plist changes do
not, and dependency changes need `uv sync`.

## Uninstall

```bash
launchctl bootout gui/$(id -u)/<your-label>
rm ~/Library/LaunchAgents/<your-label>.plist
rm -rf .wa-profile .wa-state .wa-agent
```

Then log the browser out from **phone → Linked Devices**. That is the step that
actually revokes access — deleting the profile only removes your local copy.

---

## Security and privacy

`.wa-profile/` is a **live WhatsApp credential** — anyone holding it can read
and send as you. Treat it like an SSH key. It never leaves your machine and is
gitignored.

The session is rotated every 24h by policy. The daemon cannot rotate itself
(linking needs a QR from your phone), so it warns in the self-chat at 6h, 2h and
30m — while it still has a channel to warn through. Once the session lapses the
self-chat is gone too, and the only fallback is a macOS notification.

To revoke everything: **phone → Linked Devices → log out**, then
`rm -rf .wa-profile .wa-state`.

See [SECURITY.md](SECURITY.md) for the boundary model and the disclosed
privilege-escalation finding.

## Notes on WhatsApp Web

Verified against the live site, most recently on 2026-09-05:

- **The message list is virtualised, at both ends.** Reopening a chat can
  render one row out of nineteen, so reading a conversation means scrolling.
  But rows scrolled away from stay in the DOM with empty text, so scrolling to
  the top to load history silently drops the newest messages — which is where
  every command arrives. Reads therefore start at the bottom and merge each
  window as they scroll up. Scrolling costs about 6s, so reads are cached per
  page.
- **It gates on the User-Agent, not on headless.** Headless Chromium
  advertises `HeadlessChrome/...`, and WhatsApp answers with a
  "WhatsApp works with Google Chrome 100+" notice rather than the app. For
  months that was read here as "it does not render headless", and every
  browser step ran headed and was minimised through CDP. Sending an ordinary
  Chrome UA (`session.CHROME_UA`) renders everything, headless included.
  Set `WA_HEADED=1` if Meta ever changes the sniff.
- Nothing useful exists at `DOMContentLoaded`; the app needs a few seconds.
  `wait_for_state` polls instead of reading the DOM immediately.
- WhatsApp shows a "What's new" dialog after updates that swallows clicks.
  `interstitials.dismiss()` runs before detection and before logout.
- Logout lives under the chat-list header **Menu**, not a settings rail:
  `[aria-label="Menu"]` → `[role="menuitem"][aria-label="Log out"]`.
- The "Stay logged in on this browser" checkbox ships ticked. Unticked, no
  session survives; `wa-login` warns if it is off.

All selectors live in `selectors.py`. When WhatsApp changes its markup, that is
the only file that should need editing.
---

## Licence

MIT — see [LICENSE](LICENSE).
