# wa-session — WhatsApp reply agent

Python + Playwright drives WhatsApp Web on a persistent Chromium profile. A
launchd daemon polls for messages, a headless Claude run drafts replies, and the
user approves each one in his own WhatsApp self-chat before anything is sent.

## Capability split — do not blur this

| | DAEMON (`wa-agent tick`) | DRAFTER (`claude -p`) |
|---|---|---|
| WhatsApp | owns every action | **no access at all** |
| Gmail / Calendar | none | read-only MCP |
| Composes text | never | yes |
| Chooses recipient | yes, from the queue item | **never** |

The drafter runs with **read-only Gmail/Calendar MCP and nothing else** — no
Bash, no Read, no Write, no Edit, no Agent. It has no filesystem at all: the
queue item is inlined into the prompt and the answer comes back as the run's
final message, which the DAEMON validates and writes. The summariser gets
**zero tools**.

That matters: `Write` was granted until 2026-09-02 and reached
`src/wa_session/`, the package the daemon imports and executes on its next
tick — a path from "a stranger messaged you" to code running as the user.
Withholding `Edit` never prevented it. Path-scoping cannot fix it either:
`Write(<dir>/**)` fails closed in this CLI even inside the scope.

The answer schema still **rejects** `chat`/`recipient`/`to`/`send`/`live`/`draft_id`.

## Invariants

- `--live` only immediately after a poll returned `approve` for that draft id.
- Approval is a whole-message `OK #XXX`. `OK #XXX but shorter` is AMBIGUOUS and
  must not send. Silence is never consent.
- `deliver` journals the send attempt immediately **before the click**, never
  before the pre-send checks — an earlier journal marked drafts permanently
  "sent" when `verify_recipient` refused, and they vanished silently.
- A tick has three phases: browser work **under** the profile lock, LLM runs
  **with it released**, then posting **re-taking** it. Running the LLM inside
  the lock starved every other tick and made the fast path unreachable.
- An unclear approval refuses to send but does **not** block later commands;
  a clean `OK #XXX` posted afterwards still wins.
- A drafting run whose `workspace-mcp` handshake is not `connected` is killed at
  the init message and returns `ok: False`. The queue item stays for a retry —
  a toolless run exits 0 and would otherwise publish a confident reply built on
  nothing. After `CONTEXT_STALL_ATTEMPTS` failures the self-chat says so.
- **A rejected draft is a decision, not a dropped message.** `NO #XXX` retires
  the draft and nothing re-queues the source — the chat is already read, so it
  will not resurface as unread. That is intended: the user re-opens the topic by
  sending a new message, which makes the chat unread and queues it normally.
  Do not "fix" this by re-queueing on reject.
- `propose`/`deliver` refuse any chat not in `reply` mode.
- Recipient verified on two independent signals (header title + composer
  aria-label); any conflict refuses.
- Never infer availability from one calendar. Query all three (see memory).
- **Replies read like the user texting, not like a report.** One or two
  sentences, answer first, every part of the question answered — and NO
  provenance in the body: no naming a calendar or an email, no "I checked", no
  listing what was not found. That belongs in `sources`, which only the user
  sees at approval. "Не знаю" is said briefly, never as a survey of what was
  searched. Brevity must not drop half a two-part question.
- **Digests are short.** Under 120 words total, max 3 one-line bullets per
  chat, no restating a message in full — a digest slower to read than the
  messages has failed. (A 2-message digest once filled a phone screen.)
- **Digests are English.** The groups are Hebrew; the digest translates rather
  than transcribes and never mixes languages mid-sentence, quoting short Hebrew
  only where the exact wording carries the meaning. This is the DIGEST only —
  replies still go out in the language the other person wrote in.
- **Every self-chat note goes through `tick.post_note`**, which READS THE
  MESSAGE BACK before returning. Two separate failures make that necessary:
  `selfchat.post` returns a SendResult and does not raise on a post-click
  refusal; and even a genuine "sent" is not yet transmitted when the call
  returns, so `_post_phase` closing the browser immediately dropped the
  message — `summary_posted` in the log, nothing in the chat. Drafts never hit
  this because `propose` already reads back to find `marker_id`.
- **A digest never repeats itself.** `watermarks.py` keeps the last summarised
  `msg_id` per group in `.wa-agent/digest_seen.json`; GROUPSUM summarises only
  what arrived after it, and posts "nothing new" rather than restating. The
  mark advances **only after the digest posts** — advancing at capture would
  have lost everything covered by the five posts that silently failed.

## Commands

```
uv run wa-login                 # QR scan; rotates if >24h old
uv run wa-login --status        # checks WhatsApp itself (--quick = record only)
uv run wa-agent list|allow|deny # allowlist (--mode reply|summarize)
uv run wa-agent unread|chats|pending
uv run wa-agent propose|poll|send [--live]
uv run wa-agent tick            # one unattended cycle (the daemon runs this)
uv run pytest                   # ~250 tests; -m "not browser" for the fast ones
```

Self-chat commands: `OK #XXX`, `NO #XXX`, `EDIT #XXX: …`, `GROUPSUM`.

## Hard-won facts — check before "fixing" these

- **WhatsApp Web does not render headless.** Verified on both fresh and
  logged-in profiles: empty document. Every browser step runs headed.
- Windows are **minimised via CDP** (`quiet=True`), not headless and not moved
  off-screen — macOS clamps `--window-position` back on screen.
- **Enter sends**, so nothing here ever presses it — the send is a click.
  A body goes in as ONE `keyboard.insert_text(text)`, newlines included:
  `insert_text` dispatches no key events, so Enter cannot fire mid-body, and
  per-character typing blew the 30s timeout on long drafts.
- **A line starting `- `, `* ` or `+ ` cannot be posted as-is.** The composer
  builds a real list and its own `•` marker replaces the LINE BREAK, so
  `…\n- Tomorrow` reads back as `…•- Tomorrow` — same length, so it looks like
  corruption rather than truncation. `compose.neutralize_list_markers` swaps
  them for `·` before typing AND before comparing. `·`, `–`, `---`, `-5` and
  `*bold*` are all safe and untouched.
- **Do NOT type line-by-line with Shift+Enter.** The composer auto-continues
  lists: Shift+Enter after a line starting `1.` makes WhatsApp insert `2. `
  itself, so a body carrying its own `2.` arrives as `2. 2.` — and once a list
  is running every later line is numbered too (`• bullet` → `4. • bullet`).
  This silently blocked a 3000-char GROUPSUM digest for five ticks.
- Nothing useful exists at DOMContentLoaded; poll with `wait_for_state`.
- All selectors live in `selectors.py`, verified against the live site.
- launchd caches the plist at bootstrap: editing it changes nothing until
  `bootout` + `bootstrap`. Resolve binaries in code instead.
- One `flock` per profile and one per queue item; an LLM run can outlast the
  tick interval, so without the latter two paid runs race. `wa-login` takes the
  same profile lock for its whole run (waits up to `LOCK_WAIT_S`, then refuses)
  — rotation calls `rmtree` on a directory a tick may be driving. `--status`
  takes no lock: it only reads a timestamp.
- **Opening a chat spends its read receipt**, at capture time — before any
  draft exists. Rejecting a draft cannot take that back, and the chat is no
  longer unread, so nothing re-queues it. See the warning atop `messages.py`.
- The drafter's MCP status is only visible with `--output-format stream-json
  --verbose`; the plain `json` result carries no `mcp_servers`. The init line is
  not reliably first — a `rate_limit_event` can precede it.

## State

`.wa-agent/` — allowlist, style.json (house style injected into prompts),
journal.jsonl, queue/, outbox/, daemon.log. All gitignored, all `0600`/`0700`.
`.wa-profile/` is a live WhatsApp credential — treat it like an SSH key.

## Daemon

`~/Library/LaunchAgents/<your-label>.plist` (set `WA_DAEMON_LABEL` to match),
every 300s (real gaps
300–466s). Needs the Aqua GUI session.

A recorded session can be **stale**: WhatsApp may drop the link while the local
record still reads "valid" — seen 2026-09-01, record 2h51m old and healthy while
the page showed a QR. The rotation clock cannot detect this (by its reckoning
21h remained), so `--status` opens the page and reports what is really there;
only `daemon.log` and the desktop notice catch it otherwise.

It cannot rotate its own session — linking needs a QR scanned from the phone —
so it gives notice instead, while it still has a channel to give it through:
the self-chat warns at 6h, 2h and 30m of remaining life (`rotation.py`, once
each, keyed to `linked_at` so a relink resets them). Once the session is
actually gone the self-chat is gone with it, so the only remaining fallback is
a throttled macOS notification.

The 24h clock is **this project's policy, not WhatsApp's** — a WhatsApp Web
session outlives it comfortably. The daemon therefore keeps working past the
deadline and only warns; set `WA_ENFORCE_ROTATION=1` to make it stop instead.

**A plain `wa-login` before the deadline does not reset the clock** — it finds a
valid session, prints "within policy" and never offers a QR. Only `--reset` (or
a login *after* expiry) unlinks, wipes and restarts the 24h. Rotation warnings
therefore name `wa-login --reset`; a bare `wa-login` would be a no-op at exactly
the moment they are sent.
