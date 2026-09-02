# Security

This project runs an unattended LLM against message content written by people
you have never met. The interesting part is not that it works; it is where the
boundaries are, and what happened when one of them turned out to be imaginary.

## The design

| | daemon (`wa-agent tick`) | drafter (`claude -p`) |
|---|---|---|
| WhatsApp | every action | **none** |
| Gmail / Calendar | none | read-only MCP |
| Filesystem | full | **none** |
| Composes text | never | yes |
| Chooses recipient | yes, from the queue item | **never** |

Nothing is sent without a whole-message `OK #XXX` typed into your own WhatsApp
self-chat. `OK #XXX but shorter` is ambiguous and never sends. Silence is never
consent.

## Finding: the drafter could write the code the daemon executes

**Fixed 2026-09-02. Present in all earlier revisions.**

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
daemon validates it and writes the file. The drafter now holds read-only Gmail
and Calendar and nothing else. The summariser holds no tools at all. `Read` and
`Write` are denied explicitly rather than merely left out of the allowlist.

The lesson is narrow and worth stating plainly: an allowlist that omits a
dangerous tool is not the same as a boundary, and a comment asserting a
property is not evidence of it.

## Handling of secrets

- `.wa-profile/` is a **live WhatsApp credential**. Anyone holding it can read
  and send as you. It is gitignored, `0700`, and never leaves the machine.
- `.wa-agent/` holds the allowlist, journal, queue and your account
  configuration. Gitignored, `0700`/`0600`.
- Real account and calendar identifiers live in `.wa-agent/context.json`, which
  is gitignored. `context.example.json` in the repo is a placeholder template.
- No credential is committed, logged to stdout, or placed in a draft.

## Reporting

Open an issue for anything non-sensitive. For a vulnerability, please use
GitHub's private security advisory rather than a public issue.
