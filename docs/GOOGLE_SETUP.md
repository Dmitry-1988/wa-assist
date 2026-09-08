# Giving the drafter Gmail and Calendar

The drafter answers questions like "when did we drive to the wedding?" by
reading your mail and calendar through
[workspace-mcp](https://github.com/taylorwilsdon/google_workspace_mcp). That
needs a Google OAuth client of your own.

Make your own — a client is per-person, not something to share.

This takes about fifteen minutes and has four traps in it. Each one is called
out below, because every one of them has already cost someone an evening.

---

## 1. A project, and two APIs

1. Open the [Google Cloud Console](https://console.cloud.google.com/) and
   create a project (or pick an existing one — it need not be dedicated).
2. **APIs & Services → Enabled APIs & services → + Enable APIs and services**
3. Enable **Gmail API** and **Google Calendar API**. Both, separately.

Nothing here costs money at this volume.

---

## 2. The consent screen

**APIs & Services → OAuth consent screen** (newer consoles call this
*Google Auth Platform → Branding*).

| field | value |
|---|---|
| App name | anything — `wa-assist` |
| User support email | your own address |
| Developer contact information | your own address, at the bottom of the page |

Leave **Application home page**, **Privacy policy link**, **Terms of service
link** and **Authorized domains** blank. They are only needed if you have real
pages to point at.

> **Trap 1 — do not upload a logo.** The page says so itself in small print:
> uploading one requires submitting the app for verification. A logo buys you
> nothing on a consent screen only you will ever see, and costs you a review
> process.

### Then publish it

**Audience → Publish app**, moving it out of *Testing*.

> **Trap 2 — an app left in Testing expires its refresh token after exactly
> seven days.** Not the access token, which is meant to expire hourly and is
> renewed automatically: the *refresh* token, the thing that makes renewal
> possible. Every seven days the daemon silently loses Gmail and Calendar and
> you re-authorise by hand. It is not obvious, because everything works
> perfectly for six days.

You will be told the app becomes available to any Google account. That is fine.
Nobody else has your client secret, and the credentials live only on your
machine.

Publishing does **not** require verification here. Verification is about
removing the browser warning and raising user caps, neither of which matters
for one person.

---

## 3. The OAuth client

**APIs & Services → Credentials → + Create credentials → OAuth client ID**

> **Trap 3 — choose "Web application", NOT "Desktop app".** Desktop clients do
> not carry a registered redirect URI in the way this server needs, and Google
> answers the login attempt with
> `Error 400: invalid_request … redirect_uri=http://localhost:8000/oauth2callback`.
> The error names the redirect URI, which makes it look like a typo. It is not:
> it is the client type.

Under **Authorized redirect URIs**, add exactly:

```
http://localhost:8000/oauth2callback
```

No trailing slash, `http` not `https`, and the port must match
`WORKSPACE_MCP_PORT` below. Save, and copy the **client ID** and **client
secret**.

Google can take a minute to propagate a new redirect URI. If the first attempt
fails, wait and retry before changing anything.

---

## 4. Register the server with Claude Code

From the project directory:

```bash
claude mcp add workspace-mcp --scope project \
  -e GOOGLE_OAUTH_CLIENT_ID=... \
  -e GOOGLE_OAUTH_CLIENT_SECRET=... \
  -e WORKSPACE_MCP_PORT=8000 \
  -e GOOGLE_OAUTH_REDIRECT_URI=http://localhost:8000/oauth2callback \
  -e OAUTHLIB_INSECURE_TRANSPORT=1 \
  -- uvx workspace-mcp@1.26.0 --read-only --tools gmail calendar
```

`--read-only` is the point of the whole arrangement: the drafter gets
`search_gmail_messages`, `get_events` and their siblings, and cannot send mail
or touch your calendar. `OAUTHLIB_INSECURE_TRANSPORT=1` permits the plain-HTTP
loopback redirect, which never leaves your machine.

**The version is pinned deliberately.** `uvx workspace-mcp` without `@1.26.0`
resolves whatever is newest at the moment each drafting run starts — an
unpinned third-party dependency in the daemon's critical path, with no test on
this side that would notice a breaking change. It would present as tools that
stop working for no visible reason, which is an afternoon nobody needs twice.
Raise the pin deliberately, and re-run the check below afterwards.

---

## 5. Authorise, once

```bash
claude -p "Call list_calendars for <you>@gmail.com" \
  --allowedTools "mcp__workspace-mcp__list_calendars" --max-turns 3
```

The first call fails on purpose, opens a browser, and prints an authorisation
URL. Sign in, click through **"Google hasn't verified this app" → Advanced →
Go to …** — expected, and permanent for an unverified app — and approve.

> **Trap 4 — the process that receives the callback must be the one holding
> your current credentials.** The redirect goes to `localhost:8000`, and
> whichever workspace-mcp process owns that port answers it. A server left
> running from an earlier session, with an older client id in its environment,
> will happily take your authorisation code and fail the exchange with
> `(deleted_client) The OAuth client was deleted` — an error about a client you
> are not using. If you see it, or any `invalid_client`:
>
> ```bash
> lsof -nP -iTCP:8000 -sTCP:LISTEN     # find the process
> kill <pid>                           # it restarts on demand
> ```
>
> then authorise again. Because `claude -p` exits as soon as it answers, ask it
> to retry in a loop so its server is still alive when you finish consenting:
>
> ```bash
> claude -p "Call list_calendars for <you>@gmail.com. If it needs
>   authentication, print the URL and retry every 20 seconds up to 12 times
>   while a human completes consent." \
>   --allowedTools "mcp__workspace-mcp__list_calendars" --max-turns 30
> ```

Credentials land in `~/.google_workspace_mcp/credentials/<you>@gmail.com.json`.
That file contains a refresh token: treat it like a password.

---

## Checking it works

```bash
claude -p "Call get_events for <you>@gmail.com for the next 7 days" \
  --allowedTools "mcp__workspace-mcp__get_events" --max-turns 5
```

A list of events, or an honest "no events", means you are done.

---

## When it breaks later

| symptom | cause | fix |
|---|---|---|
| `Error 400: invalid_request`, naming the redirect URI | client is a Desktop app, or the URI is not registered | Trap 3 |
| `(deleted_client)` or `invalid_client` at the end of consent | a stale server on port 8000 holds an old client | Trap 4 |
| Worked for a week, now `Google Authentication Needed` | consent screen still in Testing | Trap 2 |
| `invalid_grant` | the grant was revoked, or the account's password changed | authorise again (step 5) |
| `context_unavailable: a tool call failed` in `daemon.log` | any of the above | the daemon is refusing to draft without context; that is correct |

That last row matters. When Google access is broken the daemon does **not**
fall back to answering from memory. It keeps the message queued, retries, and
tells you in your self-chat after several failures. A reply built on no
calendar and no mail is worse than a late one.
