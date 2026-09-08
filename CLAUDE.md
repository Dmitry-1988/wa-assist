# wa-assist — WhatsApp reply agent

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

## The other documents

- `README.md` — install and use it. `DISCLAIMER.md` — what a beta user is
  accepting (ban risk, read receipts, cost).
- `docs/OPERATIONS.md` — one tick in order, every `daemon.log` key, state
  files, known limitations, latency. **Read the `error` key first** when
  diagnosing: a tick that dies early logs empty `actions` and looks idle.
- `docs/GOOGLE_SETUP.md` — the OAuth client, and the four traps that cost a
  day each (Web application not Desktop; the exact redirect URI; publish the
  consent screen or Google expires the refresh token every 7 days; no logo).
- `docs/BACKENDS.md` — sketch only, nothing implemented, for running the model
  on something other than Claude.
- `SECURITY.md` — the boundary, and the disclosed privilege-escalation finding.

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
- `EDIT #XXX` retires the original **immediately** — it can never be approved
  afterwards — and issues the redraft under a NEW id. Ids are never reused: one
  id maps to one exact body for ever, which is what makes `OK #XXX`
  unambiguous when an old draft is still visible above it in the chat.
- The revision count lives on the Draft and is carried through
  `propose`, or `MAX_REVISIONS` is unreachable and an edit loop is unbounded.
- **A command never vanishes silently.** An EDIT is acknowledged the moment it
  is accepted, and a command naming a sent, withdrawn or expired draft gets a
  one-time reply saying so — those drafts are filtered out of `pending_drafts`,
  so without this they reach no code path at all and look like a dead daemon.
- `propose`/`deliver` refuse any chat not in `reply` mode.
- **A draft answers ONE message, and WhatsApp lets the sender edit it.** The
  edit replaces the text in place, so the user's chat shows only the new
  wording while the draft was written against the old — it reads exactly like
  the model inventing a question. Seen 2026-09-06: captured 20:39, edited
  20:39, draft posted 20:41 answering a question that no longer existed.
  `deliver` re-reads the chat and refuses unless `draft.quoted` is still there
  verbatim, and refuses too when it cannot check. A refused send is ANNOUNCED
  in the self-chat: an approval that produces nothing is the dead-daemon
  failure again.
- Recipient verified on two independent signals (header title + composer
  aria-label); any conflict refuses.
- Never infer availability from one calendar. Query all three (see memory).
- **A reply answers THEIR message, never ours.** `quoted` is the last captured
  message and nothing used to check the sender. A photo with no caption leaves
  no text row — `extract_messages` drops empty rows, which is how date
  separators are excluded — so the newest text was the user's own reply from
  hours earlier and the daemon drafted an answer to it. In a `reply` chat the
  other party IS the chat, so `sender != chat` means it came from us:
  `_nothing_to_answer` refuses, and the chat re-queues when they next write.
- **A connected MCP server is not a working one.** `mcp_health` reads the init
  handshake; it cannot know whether a CALL will succeed. workspace-mcp stays
  `connected` with dead Google OAuth and answers every call "Google
  Authentication Needed", so the run proceeds and the model writes a confident
  reply having checked nothing — and says so in its own sources, in a message
  the user is invited to approve. `drafter.tool_failure` scans tool results and
  kills the run: auth is global, so the first failure is enough.
- **The drafter reports facts; it never creates obligations.** No promises
  ("скину вечером"), no accepting or proposing plans, dates, bookings, spending
  or attendance, no agreeing to a request. A free calendar is not consent to
  fill it. If a full answer needs a decision, it states the facts and hands the
  decision back as one short question. The single allowed promise is "гляну и
  скажу", because the user already asked it to look.
- **Replies read like the user texting, not like a report.** One or two
  sentences, answer first, every part of the question answered — and NO
  provenance in the body: no naming a calendar or an email, no "I checked", no
  listing what was not found. That belongs in `sources`, which only the user
  sees at approval. "Не знаю" is said briefly, never as a survey of what was
  searched. Brevity must not drop half a two-part question.
- **Digests report, they do not infer.** A teacher wrote "Thursday and Friday
  are my days off"; the digest said "no kindergarten those days" — false, a
  substitute was covering. State the words and let the user draw the
  consequence. The digest also must not title itself: the daemon adds the
  header, and `_strip_own_title` removes a second one anyway.
- **Digests are short.** Under 120 words total, max 3 one-line bullets per
  chat, no restating a message in full — a digest slower to read than the
  messages has failed. (A 2-message digest once filled a phone screen.)
- **Digests are English.** The groups are Hebrew; the digest translates rather
  than transcribes and never mixes languages mid-sentence, quoting short Hebrew
  only where the exact wording carries the meaning. This is the DIGEST only —
  replies still go out in the language the other person wrote in.
- **Rendering is not sending; wait for WhatsApp's acknowledgement.** A message
  is in the composer's own chat the instant it is typed and sits at `Pending`
  until the server takes it — so reading the text back proves only that THIS
  browser drew it. The daemon closes the browser next, and a Pending message
  dies there: no error, `summary_posted` in the log, nothing on the phone.
  Two digests eleven minutes apart on 2026-09-07, identical markup and code
  path — 14:22 arrived, 14:33 did not. The status lives in an `aria-label` on
  the row (`Pending` → `Sent`/`Delivered`/`Read`); measured live, Pending at
  1.9s and Read at 2.4s. `selfchat.wait_for_delivery` polls it, and both
  `post_note` and `agent._post_and_locate` REFUSE anything not acknowledged —
  the item stays queued and retries. A duplicate is recoverable; a digest that
  never arrived while the log says it did is not.
- **A post is three separate checks, and skipping any of them loses messages.**
  `selfchat.post` RETURNS a `SendResult` and does not raise when a send is
  refused after the click, so (1) the result must be inspected. The message
  then renders before it transmits, so (2) it must be polled for until it
  appears — `propose` read ONCE, immediately, and a message still in flight
  looked like a failure: it raised, the caller closed the browser, and seven
  paid runs on 2026-09-06 left seven orphan drafts that no `OK` could reach.
  And appearing is not sending, so (3) the acknowledgement above must be
  waited for. Each check was added after the previous one was believed
  sufficient; assume a fourth is missing rather than that this is finished.
- **Read-backs compare fingerprints, not raw text.** WhatsApp renders emoji as
  `<img>` and `inner_text` drops them: a note posted as `📋 GROUP DIGEST 13:16`
  reads back as `GROUP DIGEST 13:16`. Matching raw text declared every digest
  undelivered and posted it again — ten duplicates in one afternoon. Covered by
  `test_readback_browser.py`, which renders emoji as `<img>` in a real DOM: the
  fakes elsewhere echo back what was posted, which is exactly what WhatsApp
  does not do.
- **A message reported once is never reported again.** `digest_seen.json` holds
  a SET of reported `msg_id`s per group, not a single watermark, and it only
  ever grows. One mark was not enough: `advance` assigned the last captured id,
  so a capture that had lost its tail moved the mark BACKWARDS and every later
  digest re-reported everything after it — quiet groups appearing over and over
  with nothing new to say. Ids are added **only after the digest posts**.
- **New means "after the last reported message in view", not "not yet
  reported".** A capture reaching further back than the previous one surfaces
  messages older than anything ever reported; those are history the user
  scrolled past days ago, not news. `unseen` anchors on the LAST reported
  message in the captured window.
- **`context` is read, never reported.** `unseen` returns up to
  `CONTEXT_MESSAGES` already-reported messages before the first new one,
  because a reply is nonsense without what it answers. The summariser is told
  to use them and say nothing about them, and `advance` does not mark them
  reported — that would hide them from a digest that genuinely needs them.
- `wa-agent digest-catchup` marks everything currently visible as reported. It
  exists because a rewound record cannot be repaired from the ids it kept.
- **A digest may be incomplete; it may never be incomplete in silence.**
  `watermarks.unseen` returns a `gap` flag, and the daemon states it in the
  note (`⚠️ INCOMPLETE`) and the log (`groupsum_window_gap`). Two ways it used
  to lose messages without a word: a 15-row capture that a busy group outran,
  so the mark fell outside the window and the whole window was called new;
  and `fresh[-40:]`, which dropped the oldest of a backlog and then advanced
  the mark **past** them, so no later digest could pick them up either. Capture
  is now `SUMMARY_CAPTURE_DEPTH` (60) and the cap is `SUMMARY_MAX_MESSAGES`
  (120), and hitting either is reported. The gap is the DAEMON's fact — the
  summariser is handed messages and cannot know what never arrived, so telling
  it would only invite a guess. The mark still advances on a gap: what was
  covered is covered.

## Commands

```
uv run wa-login                 # QR scan; rotates if past the 14-day policy
uv run wa-login --status        # checks WhatsApp itself (--quick = record only)
uv run wa-agent list|allow|deny # allowlist (--mode reply|summarize)
uv run wa-agent unread|chats|pending|drop|read|digest-catchup
uv run wa-agent propose|poll|send [--live]
uv run wa-agent tick            # one unattended cycle (the daemon runs this)
uv run pytest                   # 670 tests; -m "not browser" for the fast ones
```

Self-chat commands: `OK #XXX`, `NO #XXX`, `EDIT #XXX: …`, `GROUPSUM`.

## Hard-won facts — check before "fixing" these

- **Virtualisation applies to GROUP capture too, not just the self-chat.**
  `capture_chat` used `load_more`, so raising `SUMMARY_CAPTURE_DEPTH` from 15
  to 60 made every deep capture scroll to the top and drop the tail. GROUPSUM's
  watermark is a RECENT message, so it fell outside the window, all four groups
  reported a gap at once, and the entire history was re-summarised as new —
  a digest full of things the user had already read. At depth 15 `load_more`
  never scrolled at all, which is the only reason it had looked correct. Both
  paths now go through `messages.read_window`.
- **WhatsApp virtualises the message list, and it cuts BOTH ways.** Reopening
  a chat can render ONE row of nineteen, so `selfchat.read` must scroll — the
  daemon otherwise reads the self-chat, where every GROUPSUM and approval
  arrives, through a keyhole. But rows scrolled away from stay in the DOM with
  EMPTY TEXT, and `extract_messages` drops empty rows: `load_more` reaches its
  target with `scrollTop = 0`, so reading after it returned the OLDEST
  messages and silently dropped the NEWEST. Measured 2026-09-06 on a
  45-message self-chat: `read(limit=60)` returned 45 messages, none of them
  the three most recent, and it was non-monotonic (25 and 60 lost the newest,
  40 did not). Every command arrives at the bottom, so `read_after` could not
  find the marker it was told to start from, returned `[]` by its own safety
  rule, and an `OK #XXX` plainly in the chat was never seen — verified end to
  end. `_read_windows` now extracts at the BOTTOM first and merges each window
  onto the front as it scrolls up, then returns to the bottom.
- **Scrolling up is not the same as having arrived.** It took SIX steps to
  cross the loaded page before WhatsApp prepended any older history (25 rows →
  48). A stall counter that treated "this step added nothing" as "the history
  has ended" stopped after three and capped every deep read at one screenful.
  `_read_windows` only counts a stall once `scrollTop` has stopped falling.
- Scrolling costs ~6s, so a read is cached per page and invalidated by `post`;
  the delivery read-back passes `scroll=False` since it only wants the newest
  message — which is why `_read_windows` must leave the pane at the bottom.

- **WhatsApp Web renders headless fine — it gates on the User-Agent.**
  Headless Chromium says `HeadlessChrome/...` and WhatsApp answers with
  "WhatsApp works with Google Chrome 100+" instead of the app. That page was
  read as "does not render headless" and cost this project months of headed,
  CDP-minimised windows. Send `session.CHROME_UA` and old and new headless
  both work — verified 2026-09-06 on a live logged-in session: chat list,
  virtualised message list, emoji as `<img>`, both recipient signals, typing
  and click-to-send. The UA is passed headed too, so a login and a tick look
  like the same browser.
- `quiet=True` therefore means **headless**. `WA_HEADED=1` restores the old
  headed-and-minimised path for the day Meta changes the sniff. Minimising was
  never actually invisible: the window is created frontmost and minimised a
  moment later, so every launch stole focus — at 120s and up to two launches a
  tick, that reshuffled the user's Spaces ~30 times an hour.
- macOS clamps `--window-position`, so an off-screen window snaps back; that
  is why minimise, not move, was the old fallback.
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
- **`workspace-mcp` is PINNED** (`uvx workspace-mcp@1.26.0`) in the MCP
  registration. Unpinned, every drafting run resolves whatever is newest that
  minute — an untested third-party dependency in the daemon's critical path,
  whose breakage would look exactly like the Google auth failure of
  2026-09-08 and be diagnosed as slowly. Raise it deliberately, then re-verify
  Gmail and Calendar. The Claude Code CLI is the remaining unpinned dependency
  and cannot be pinned: `mcp_health` reads the shape of its `system/init`
  event and `tool_failure` the shape of its tool results, so a CLI change
  breaks the two gates that stop a reply being built on nothing.
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
every 120s. Runs headless and puts nothing on screen; the Aqua GUI session is
needed for `wa-login`, which must show a QR.

A recorded session can be **stale**: WhatsApp may drop the link while the local
record still reads "valid" — seen 2026-09-01, record 2h51m old and healthy while
the page showed a QR. The rotation clock cannot detect this (by its reckoning
21h remained), so `--status` opens the page and reports what is really there;
only `daemon.log` and the desktop notice catch it otherwise.

It cannot rotate its own session — linking needs a QR scanned from the phone —
so it gives notice instead, while it still has a channel to give it through:
the self-chat warns at 24h, 6h and 2h of remaining life (`rotation.py`, once
each, keyed to `linked_at` so a relink resets them). Once the session is
actually gone the self-chat is gone with it, so the only remaining fallback is
a throttled macOS notification.

The 14-day clock is **this project's policy, not WhatsApp's** — WhatsApp
expires a linked device on INACTIVITY (~14 days of the phone being offline),
not on a fixed lifetime. The daemon keeps working past the deadline and only
warns; set `WA_ENFORCE_ROTATION=1` to make it stop instead.

It was 24h while the drafter could still reach the filesystem — a prompt
injection could `Write` into `src/wa_session/` and copy the profile out. That
path is closed.

**Nothing here revokes a session.** `log_out()` has ONE caller, `wa-login`.
The daemon never unlinks, so passing the deadline changes nothing and
`WA_ENFORCE_ROTATION=1` only makes the tick stop — the device stays linked and
a copied profile keeps working. It costs availability and reduces exposure by
zero. This is a reminder interval, not a revocation interval; do not write
otherwise, as three docs did until 2026-09-07. Auto-unlink at the deadline
would make the name true and is not implemented.

`config.assert_not_synced` refuses a profile inside iCloud, Dropbox, OneDrive,
Google Drive or `~/Library/CloudStorage`. Sync defeats the file mode,
FileVault AND rotation at once, and it fails silently — everything works while
a copy of the session sits on someone else's servers. Checked in
`ensure_private_dir`, which every browser launch passes through.

**A plain `wa-login` before the deadline does not reset the clock** — it finds a
valid session, prints "within policy" and never offers a QR. Only `--reset` (or
a login *after* expiry) unlinks, wipes and restarts the clock. Rotation warnings
therefore name `wa-login --reset`; a bare `wa-login` would be a no-op at exactly
the moment they are sent.
