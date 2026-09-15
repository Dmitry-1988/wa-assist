"""Re-authorising Google, in one command instead of four attempts.

Google expires the refresh token of an unverified app requesting a restricted
scope after about seven days -- measured twice here at 6d19h35m and 6d20h14m,
and `gmail.readonly` is the scope that forces the verification this project
will never submit for. So this is a weekly chore, and the point of this module
is that it costs one command and one click rather than an investigation.

The consent click itself cannot be automated; Google requires a human. What
went wrong the first three times was everything around it:

* A server left over from an earlier session held port 8000 with a client id
  since deleted, took the authorisation code, and failed the exchange with
  `(deleted_client)` -- an error naming a client nobody was using.
* `claude -p` exits when the model stops talking, and its MCP server dies with
  it. Told to "wait 20 seconds and retry", the model announced the retry and
  finished, so the browser redirected to a closed door:
  `ERR_CONNECTION_REFUSED`, with a perfectly good code in the URL bar.
* The authorisation URL only appears in the run's final output, which arrives
  after the window to use it has passed.

So: kill anything stale, keep a server alive for the whole flow, surface the
URL immediately, wait for the credentials file to actually change, and verify
BOTH tools afterwards rather than trusting that consent implies access.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

CREDENTIALS_DIR = Path.home() / ".google_workspace_mcp" / "credentials"
STATES_FILE = CREDENTIALS_DIR / "oauth_states.json"
OAUTH_PORT = 8000
REDIRECT_URI = f"http://localhost:{OAUTH_PORT}/oauth2callback"

SCOPES = (
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
)

# What a workspace-mcp process looks like, so nothing else on this port is
# ever killed. Someone's dev server living on 8000 is not ours to stop.
_OURS = re.compile(r"workspace[-_]mcp", re.IGNORECASE)


def credential_path(account: str) -> Path:
    return CREDENTIALS_DIR / f"{account}.json"


def credential_stamp(account: str) -> float:
    """Mtime of the stored credential, or 0.0 if there is none.

    The signal that consent completed. Watching this rather than parsing the
    helper's output is what makes the wait reliable: the file is written by
    whichever process performs the token exchange, whoever that turns out to
    be.
    """
    try:
        return credential_path(account).stat().st_mtime
    except OSError:
        return 0.0


@dataclass(frozen=True)
class Holder:
    """A process listening on the OAuth port."""

    pid: int
    command: str

    @property
    def is_ours(self) -> bool:
        return bool(_OURS.search(self.command))


def port_holders(port: int = OAUTH_PORT) -> list[Holder]:
    """Processes listening on `port`. Empty when nothing is, or on any error."""
    try:
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return []
    holders = []
    for pid in {p.strip() for p in out.split() if p.strip().isdigit()}:
        try:
            cmd = subprocess.run(["ps", "-o", "command=", "-p", pid],
                                 capture_output=True, text=True,
                                 timeout=10).stdout.strip()
        except Exception:
            cmd = ""
        holders.append(Holder(pid=int(pid), command=cmd))
    return holders


def free_the_port(port: int = OAUTH_PORT) -> tuple[list[int], list[Holder]]:
    """Stop our stale servers on `port`. Returns (killed pids, foreign holders).

    A foreign holder is never killed and never tolerated either: the callback
    would land on it, so the caller must stop and say so.
    """
    killed, foreign = [], []
    for holder in port_holders(port):
        if not holder.is_ours:
            foreign.append(holder)
            continue
        try:
            os.kill(holder.pid, 15)
            killed.append(holder.pid)
        except OSError:
            pass
    if killed:
        time.sleep(2)
    return killed, foreign


def _client_id(account: str) -> str:
    """The OAuth client id, from the stored credential or Claude Code's config.

    The credential file records the client it was issued for, which is the
    authority when it exists. Falling back to the MCP registration covers a
    first run, or one where the client was just replaced.
    """
    try:
        return json.loads(credential_path(account).read_text())["client_id"]
    except Exception:
        pass
    try:
        data = json.loads((Path.home() / ".claude.json").read_text())
        for project in (data.get("projects") or {}).values():
            server = (project.get("mcpServers") or {}).get("workspace-mcp")
            if server:
                found = (server.get("env") or {}).get("GOOGLE_OAUTH_CLIENT_ID")
                if found:
                    return found
    except Exception:
        pass
    return ""


def live_states(now: float | None = None) -> list[tuple[str, dict]]:
    """Unexpired pending auth flows, newest last."""
    from datetime import datetime, timezone

    try:
        data = json.loads(STATES_FILE.read_text())
    except Exception:
        return []
    moment = datetime.now(timezone.utc) if now is None else \
        datetime.fromtimestamp(now, timezone.utc)
    out = []
    for state, info in (data or {}).items():
        try:
            if datetime.fromisoformat(info["expires_at"]) > moment:
                out.append((state, info))
        except Exception:
            continue
    return sorted(out, key=lambda kv: kv[1].get("created_at", ""))


def authorization_url(account: str) -> str:
    """Rebuild the consent URL for the newest pending flow, or "".

    The server opens a browser itself, but only the first time and only if it
    can. Printing the URL costs nothing and is the difference between a stuck
    user and a click.
    """
    states = live_states()
    client = _client_id(account)
    if not states or not client:
        return ""
    state, info = states[-1]
    verifier = info.get("code_verifier") or ""
    if not verifier:
        return ""
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    query = {
        "response_type": "code",
        "client_id": client,
        "redirect_uri": REDIRECT_URI,
        "scope": " ".join(SCOPES),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent",
        "login_hint": account,
    }
    return "https://accounts.google.com/o/oauth2/auth?" + \
        urllib.parse.urlencode(query)


# The helper exists to hold a server open while a human consents. It is given
# `sleep` for exactly that reason: a model asked to wait without the means to
# wait simply finishes, taking its server with it. This is NOT the drafting
# run -- that still has read-only Gmail and Calendar and nothing else.
HELPER_PROMPT = (
    "Call mcp__workspace-mcp__list_calendars for {account}. If it reports that "
    "Google authentication is needed, print the authorization URL, then use "
    "Bash to run 'sleep 15' and call the tool again. Repeat that "
    "sleep-and-retry until it succeeds, up to {tries} times. A human is "
    "completing consent in a browser while you wait, so do not stop after the "
    "first failure."
)


def helper_command(binary: str, account: str, tries: int) -> list[str]:
    return [
        binary, "-p", HELPER_PROMPT.format(account=account, tries=tries),
        "--output-format", "json",
        "--allowedTools", "mcp__workspace-mcp__list_calendars", "Bash(sleep:*)",
        "--max-turns", str(tries * 3),
    ]


def verify_command(binary: str, account: str) -> list[str]:
    """Check BOTH tools. Consent proves a grant, not that the tools work."""
    return [
        binary, "-p",
        f"Call get_events for {account} for the next 7 days, and "
        f"search_gmail_messages for 'from:me' with max 1 result. Reply with "
        f"exactly two lines: 'CALENDAR: ok' or 'CALENDAR: fail <why>', and "
        f"'GMAIL: ok' or 'GMAIL: fail <why>'.",
        "--output-format", "json",
        "--allowedTools",
        "mcp__workspace-mcp__get_events",
        "mcp__workspace-mcp__search_gmail_messages",
        "--max-turns", "8",
    ]


def read_result(raw: str) -> str:
    """The `result` field of a `claude -p --output-format json` reply."""
    try:
        return str(json.loads(raw or "{}").get("result") or "")
    except Exception:
        return (raw or "")[-400:]


def _verify(binary: str, account: str, cwd: str) -> tuple[bool, str]:
    """Do both tools actually work right now? Consent proves neither."""
    try:
        done = subprocess.run(verify_command(binary, account),
                              capture_output=True, text=True, timeout=180,
                              cwd=cwd)
    except Exception as exc:
        return False, f"verification could not run: {exc}"
    report = read_result(done.stdout).strip()
    low = report.lower()
    return ("calendar: ok" in low and "gmail: ok" in low), report


def run_reauth(config, timeout_s: float = 300.0, emit=print,
               force: bool = False) -> dict:
    """The whole flow. Returns a summary; never raises on an expected failure."""
    from .context import ContextError, load_context
    from .drafter import claude_binary

    binary = claude_binary()
    if binary is None:
        return {"ok": False, "reason": "claude CLI not found"}
    try:
        account = load_context(config).google_account
    except ContextError as exc:
        return {"ok": False, "reason": f"context: {exc}"}
    cwd = str(config.profile_dir.parent)

    # Ask before acting. A valid token makes the helper succeed instantly and
    # the credential file never changes, so a flow that only watches that file
    # would wait out its timeout and then report failure on a working setup.
    if not force:
        emit("  checking whether access already works…")
        ok, report = _verify(binary, account, cwd)
        if ok:
            return {"ok": True, "account": account, "report": report,
                    "note": "access already works; nothing to re-authorise "
                            "(pass --force to consent again anyway)"}
        emit("  no access — re-authorising")

    killed, foreign = free_the_port()
    if foreign:
        return {"ok": False, "reason":
                "something that is not workspace-mcp is listening on port "
                f"{OAUTH_PORT}: " + "; ".join(
                    f"pid {h.pid} ({h.command[:60]})" for h in foreign) +
                ". The callback would land on it. Stop it and retry."}
    if killed:
        emit(f"  stopped {len(killed)} stale server(s) holding port {OAUTH_PORT}")

    before = credential_stamp(account)
    emit(f"  starting the consent flow for {account}…")
    helper = subprocess.Popen(
        helper_command(binary, account, tries=14),
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, cwd=str(config.profile_dir.parent), stdin=subprocess.DEVNULL)

    shown = False
    deadline = time.monotonic() + timeout_s
    try:
        while time.monotonic() < deadline:
            if credential_stamp(account) > before:
                emit("  consent completed")
                break
            if not shown:
                url = authorization_url(account)
                if url:
                    emit("\n  A browser should have opened. If not, open this:\n")
                    emit(f"  {url}\n")
                    emit(f"  Sign in as {account}, click through the "
                         '"unverified app" warning, and approve.\n')
                    shown = True
            if helper.poll() is not None:
                if credential_stamp(account) > before:
                    break
                return {"ok": False, "reason":
                        "the consent helper exited before the credential "
                        "changed — nothing was authorised"}
            time.sleep(2)
        else:
            return {"ok": False, "reason":
                    f"no consent within {timeout_s:g}s; nothing changed"}
    finally:
        if helper.poll() is None:
            helper.terminate()
            try:
                helper.wait(timeout=10)
            except Exception:
                helper.kill()

    emit("  verifying Calendar and Gmail…")
    ok, report = _verify(binary, account, cwd)
    return {"ok": ok, "account": account, "report": report}
