# Security

This project runs an unattended LLM against message content written by people
you have never met. The interesting part is not that it works; it is where the
boundaries are, and what happened when one of them turned out to be imaginary.

## The design

| | daemon (`wa-agent tick`) | drafter (`claude -p`) |
|---|---|---|
| WhatsApp | every action | **none** |
| Gmail / Calendar | none | read-only MCP |
| Filesystem | full | no file tools; see [limits](#what-is-not-closed) |
| Composes text | never | yes |
| Chooses recipient | yes, from the queue item | **never** |

Nothing is sent without a whole-message `OK #XXX` typed into your own WhatsApp
self-chat. `OK #XXX but shorter` is ambiguous and never sends. Silence is never
consent.

## Finding: three holes in the model runs' isolation

**Fixed 2026-11-22. The code had them since the first commit.** If you
installed an earlier revision, update now: `git pull && uv sync`.

This file said the drafter had no filesystem and the summariser no tools.
Neither was true. All three were reproduced on Claude Code 2.1.330.

1. **The summariser had a pre-approved shell.** `--allowedTools` followed by an
   empty list swallowed `--disallowedTools`, turning the deny list into the
   allow list: `Bash`, `Write` and `Edit`, in the run that reads group messages.
   A message from a stranger could become code running as you.
2. **The drafter could read the project directory.** `--allowedTools`
   pre-approves; it does not limit. The run also had `Grep`, the claude.ai
   connectors and four further workspace-mcp tools.
3. **A message could attach a local file.** `claude -p` expands `@path` inside
   a prompt into that file's contents, with no tool call, in both runs.

The summariser now gets `--tools "" --strict-mcp-config`. The drafter gets
`--tools ""`, has connectors and unused server tools removed, and runs in
`dontAsk`. Both set `CLAUDE_CODE_DISABLE_ATTACHMENTS=1`.

**Follow-up, 2026-11-24.** The drafter also loaded your own Claude settings
and MCP servers, so any tool your allow rules pre-approved could run in it. It
now gets `--setting-sources local`, which loads only the scope where
workspace-mcp is registered. `wa-agent reauth`'s two runs are shut in the
same way, with `sleep` as the helper's only command.

**If you ran an earlier revision**, updating does not undo past access. Claude
Code's transcripts under `~/.claude/projects/` show tool calls and attached
files, but old ones are deleted, so a clean look is not proof. If in doubt, log
out the linked device, revoke the Google grant, replace the OAuth client secret
and any other secret your user account could read, and check `src/` and
`~/Library/LaunchAgents` for changes you did not make.

## Finding: the drafter could write the code the daemon executes

**Found 2026-09-02; that fix was incomplete until 2026-11-22 (see above).
Present in all earlier revisions.**

The drafting and summarising runs were granted `Read` and `Write`. `Edit` was
withheld, and the code said so explicitly — the claim being that a model which
cannot edit files cannot rewrite the daemon. That was wrong: `Write` does the
same job.

Verified, not theorised. A run with the summariser's exact tool set wrote a new
module into `src/wa_session/` — the package the daemon imports and executes on
its next tick, 300 seconds later. The summariser is the run that ingests group
chatter from strangers. The chain was therefore:

> a stranger sends a WhatsApp message → injected instruction → code runs as you

`Read` was equally live: it reached `~/.google_workspace_mcp/credentials/`,
where the Google OAuth refresh tokens live.

**Why path-scoping was not the fix.** `Write(<dir>/**)`, `<dir>/*` and
`<dir>/**/*` all fail closed in this CLI — `permission_denials: ['Write']` even
for a target inside the allowed directory. Scoping would have silently disabled
drafting rather than contained it.

**The fix.** The exchange no longer touches the filesystem. The queue item is
inlined into the prompt; the answer returns as the run's final message; the
daemon validates it and writes the file. `Read` and `Write` were added to the
deny list. That was believed to leave the drafter only Gmail and Calendar and
the summariser nothing; it did neither.

The lesson is narrow and worth stating plainly: an allowlist that omits a
dangerous tool is not the same as a boundary, and a comment asserting a
property is not evidence of it.

## What the boundary rests on, and what tests it

An adversarial review of the finished implementation was run on 2026-09-02
against the code rather than the design notes, covering permission boundaries,
prompt injection, secret handling, session data, MCP permissions, approval
bypasses, restart behaviour, duplicate sends and unattended execution. Its
findings were fixed, but it missed the three holes above: its tests checked
Python lists, not what the CLI did with them. The properties are asserted in
`tests/test_security_hardening.py` so a regression fails the suite rather than
being rediscovered in production:

- both runs' real command lines remove every built-in tool; the summariser
  also gets no MCP servers
- a message cannot attach a file: `tests/test_prompt_mentions.py` runs the real
  CLI. The variable it relies on is undocumented, so **run it after every Claude
  Code update**, with `tests/test_settings_isolation.py`, which checks that
  your own MCP servers and allow rules stay out. CI installs the CLI and runs
  both on every push and once a week; locally they skip without it
- the answer is parsed from the run's final message, never read back from a
  file the run could have written
- a caveat is never consent — `OK #X but shorter` refuses to send, and a later
  clean `OK #X` is still reachable past it
- draft ids are never reused: a collision would silently make an approval do
  nothing, so id generation checks the journal's sent and retired sets
- a send is journalled immediately before the click, so a crash cannot produce
  a second delivery
- nothing is marked seen until it has actually been delivered — a note is
  confirmed by reading back a message that was not there before, and a failed
  digest keeps its queue item and its watermarks
- a reply counts as sent only once WhatsApp acknowledges it; past the click it
  is never retried, and an unconfirmed one is reported as uncertain
- an `EDIT` redraft is checked against the same message as the original

The `--live` flag is honoured only immediately after a poll returned `approve`
for that exact draft id, and `propose`/`deliver` refuse any chat that is not in
`reply` mode. The recipient is verified on two independent DOM signals and any
conflict refuses the send.

## What is not closed

- **Injection can still steer the words.** The drafter can search all your mail
  and calendars and could put what it finds in a draft. Your reading the draft
  before `OK` is the control.
- **Claude Code itself is not pinned.** The isolation rests on how the CLI
  treats its flags and an undocumented environment variable. The two real-CLI
  checks run in CI on every push and weekly, so a CLI change that breaks them
  shows up as a failed run.

Closed on 2026-11-24, and listed here until then:

- Neither run loads your auto-memory or `CLAUDE.md` any more. Name spellings
  come from `.wa-agent/style.json`, which was already in every prompt.
- The drafter no longer has the attachment tool, which saved a file and
  returned a path it could not open. A message fetched with `full=True` is
  written to a folder that exists only for the length of the run.
- The manual `claude -p` checks in GOOGLE_SETUP carry the same lockdown flags.

## Handling of secrets

- `.wa-profile/` is a **live WhatsApp credential**. Anyone holding it can read
  and send as you. It is gitignored, `0700`, and never leaves the machine.
- `.wa-agent/` holds the allowlist, journal, queue and your account
  configuration. Gitignored, in a `0700` directory; files are `0600` except
  the logs launchd creates (`0644`).
- Real account and calendar identifiers live in `.wa-agent/context.json`, which
  is gitignored. `context.example.json` in the repo is a placeholder template.
- No credential is committed or logged to stdout.

## Reporting

Open an issue for anything non-sensitive. For a vulnerability, please use
GitHub's private security advisory rather than a public issue.
