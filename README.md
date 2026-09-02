# wa-session

Opens WhatsApp Web in a Chromium browser that remembers your login, so you scan
the QR code once instead of every run. The session is rotated automatically
every 24 hours.

This version **only logs in**. It does not read, extract, or send messages.

## Setup

Requires [uv](https://docs.astral.sh/uv/). Everything else is installed for you.

```bash
uv sync
uv run playwright install chromium
```

## Run

```bash
uv run wa-login
```

**First run** — a Chromium window opens on WhatsApp Web showing a QR code. On
your phone: WhatsApp → Settings → Linked Devices → Link a Device → scan it.
Once the chat list appears the session is saved. Close the window when done.

**Later runs, same day** — the window opens already logged in.

**Later runs, more than 24h since the last scan** — the session is rotated:
the tool unlinks this device through WhatsApp Web, deletes the profile, and
shows you a fresh QR code to scan.

```
$ uv run wa-login
  profile age: 27h — exceeds 24h rotation policy
  logging out via WhatsApp Web UI...
  device unlinked; wiping profile
  scan the QR code in the browser window
```

Always close the browser window rather than killing the process — Chromium
writes the profile to disk on a clean shutdown.

### Other commands

```bash
uv run wa-login --status   # where things live, and how old the session is
uv run wa-login --reset    # unlink and wipe now, then log in fresh
```

## Unread digest

```bash
uv run wa-unread
```

Lists chats with unread messages and a one-line summary of each:

```
2 chats with unread messages (17 total)

● אקווה פמילי — 12 unreads · 20:27
    ~Rama Atias: היי שבת שלום יש למישהו 2 גזרים לתת לי ?

● צהרון שונית/חופים — 5 unreads · 12:25
    ~שירה לברון שינוי מבפנים: אלה ימי החופש לפני התאריכים…
```

**It never opens a chat.** It reads the chat-list rows WhatsApp has already
rendered — name, unread badge, timestamp, and the preview snippet — after
switching to the built-in "Unread" filter tab. Opening a chat would mark it
read and send blue ticks to the sender, which is visible to them and cannot be
undone, so the tool does not do it. The cost is that summaries are limited to
the one-line preview.

`--limit N` caps how many rows are read; `--keep-open` leaves the window open.

### Swapping in a real summariser

`digest.Summarizer` is a protocol with one method. `LocalDigest` (the default)
echoes the preview. An LLM-backed summariser drops in without touching
extraction or rendering:

```python
class MySummarizer:
    name = "claude"

    def summarize(self, chat: UnreadChat) -> str:
        ...

render(chats, summarizer=MySummarizer())
```

Note that anything beyond `LocalDigest` sends your messages — and your
contacts' — to a third party.

### Configuration

| Variable | Default | Meaning |
|---|---|---|
| `WA_PROFILE_DIR` | `./.wa-profile` | Chromium user-data directory |
| `WA_STATE_DIR` | `./.wa-state` | Where the session timestamp is kept |
| `WA_ROTATE_AFTER_HOURS` | `24` | Rotation policy |

```bash
WA_ROTATE_AFTER_HOURS=8 uv run wa-login
```

## Tests

```bash
uv run pytest                    # everything
uv run pytest -m "not browser"   # fast, no browser launched
uv run pytest -m browser --headed --slowmo 3000   # watch them run
```

`--slowmo MS` delays each Playwright *action* and implies `--headed`. It does
not delay read-only queries such as visibility checks, so the detection probes
still run at full speed.

The browser-marked tests launch a real headless Chromium against in-memory HTML
fixtures — no network, and no mocking of Playwright itself. They skip
automatically if the Chromium binary is not installed.

## Reading unread chats (destructive)

```bash
uv run wa-read --yes
```

Opens every unread chat and exports its messages to `.wa-export/<timestamp>/`.

**This marks those chats read and sends read receipts to your contacts.** It is
irreversible and visible to them, and the unread state is gone afterwards —
which is why `--yes` is mandatory and why the run is built to need only one
attempt:

1. The unread snapshot is written **before** any chat is opened.
2. Each chat's parsed JSON **and** its raw pane HTML are written immediately
   after capture, so a parsing mistake can be fixed offline rather than needing
   a second run that no longer exists.
3. A failure on one chat is recorded and the run continues.

Output per run:

```
unread_snapshot.json        which chats were unread, and how many
chat_NN_<name>.json         parsed messages (sender, date, time, text, id)
chat_NN_<name>.html         raw conversation pane, for offline re-parsing
all_chats.json              everything combined
```

`.wa-export/` holds message content and is gitignored. Treat it like the
profile: it contains your contacts' messages, not just yours.

## Notes on WhatsApp Web

Verified against the live site on 2026-08-28:

- **It does not render under headless Chromium at all.** The page loads but
  stays empty. Every browser step here runs headed for that reason, including
  rotation's logout — a blank headless page made the unlink look successful
  while revoking nothing.
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

## Security

**`.wa-profile/` is a live credential.** It holds the WhatsApp Web session
token. Anyone who copies that directory to another machine gets your WhatsApp
account: full message history and the ability to send as you. It is closer to
an unlocked session than to a password hash. Treat it like an SSH private key.

- **Never commit it.** It is in `.gitignore`, but `git add -f`, an IDE "add
  all", or a `tar` of the project directory all bypass that. If it ever does
  get committed, rotate the credential (`uv run wa-login --reset`) — removing
  the file from a later commit is not enough, the token is still in the git
  objects.
- **Do not move this project into `~/Desktop` or `~/Documents`.** Both are
  syncing to iCloud Drive on this machine, so the session token would be
  uploaded to Apple. `~/PycharmProjects` is outside the sync tree — checked on
  2026-08-28. The same applies to Dropbox, Google Drive, and OneDrive folders.
- **Encryption at rest** comes from FileVault, which is on for this machine's
  data volume. The profile itself is plain SQLite and LevelDB; the directory is
  created `0700` so other local accounts cannot read it.
- **Cached message content accumulates.** Even though this tool never reads
  messages, Chromium caches page data — including message content — into the
  profile as you browse. Daily rotation wipes it.

### Revoking access

On your phone: WhatsApp → Settings → Linked Devices → tap this computer → Log
out. Then delete `.wa-profile/`, or run `uv run wa-login --reset`.

### A caveat about rotation

Deleting the profile directory alone does **not** revoke the session — it
orphans it, and the linked device stays registered on your account until it
expires. WhatsApp caps you at roughly four linked devices, so rotation logs out
through the WhatsApp Web UI first.

That logout depends on WhatsApp Web's markup, which changes without notice. If
the selectors stop matching, the tool wipes the profile anyway and prints:

```
  could not confirm logout through the UI; wiping profile anyway
  ACTION NEEDED: on your phone, open WhatsApp -> Linked Devices and
  remove any stale entry for this computer.
```

Do what it says, or stale devices will pile up against the cap.
