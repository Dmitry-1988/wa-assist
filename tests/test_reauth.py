"""Re-authorising Google must be one command, not an investigation.

Google expires the refresh token of an unverified app requesting a restricted
scope after about seven days -- measured 6d19h35m and 6d20h14m here -- and
`gmail.readonly` is what forces the verification this project will not submit
for. Gmail stays: it answers "how much did we pay the vet", which no calendar
can. So the chore is weekly and the job of this module is to make it cheap.

The consent click needs a human. Everything that went wrong the first three
times did not:

  * a stale server on port 8000 held a deleted client, took the code and
    failed the exchange -- `(deleted_client)`
  * `claude -p` exits when the model stops talking, so telling it to "wait 20
    seconds" made it finish and killed the server the callback needed --
    `ERR_CONNECTION_REFUSED`, with a valid code in the URL bar
  * the authorisation URL only appeared in output that arrives too late
"""

import json
import time

import pytest

from wa_session import reauth


# --- never kill someone else's process ------------------------------------

def test_a_workspace_mcp_process_is_ours():
    h = reauth.Holder(pid=1, command="/x/bin/python /y/bin/workspace-mcp --read-only")
    assert h.is_ours


def test_uvx_wrapping_it_is_ours_too():
    h = reauth.Holder(pid=1, command="uv tool uvx workspace-mcp@1.26.0 --read-only")
    assert h.is_ours


@pytest.mark.parametrize("cmd", [
    "node /Users/me/project/dev-server.js",
    "python -m http.server 8000",
    "docker-proxy -container-port 8000",
    "",
])
def test_anything_else_is_not_ours(cmd):
    """Someone's dev server on 8000 is not this project's to stop."""
    assert not reauth.Holder(pid=1, command=cmd).is_ours


def test_a_foreign_holder_stops_the_flow(monkeypatch, tmp_path):
    """Killing it would be rude; ignoring it means the callback lands there."""
    monkeypatch.setattr(reauth, "port_holders",
                        lambda port=8000: [reauth.Holder(4, "python -m http.server")])
    killed, foreign = reauth.free_the_port()
    assert killed == [] and len(foreign) == 1


def test_our_stale_servers_are_killed(monkeypatch):
    sent = []
    monkeypatch.setattr(reauth, "port_holders",
                        lambda port=8000: [reauth.Holder(7, "uvx workspace-mcp")])
    monkeypatch.setattr(reauth.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(reauth.time, "sleep", lambda s: None)
    killed, foreign = reauth.free_the_port()
    assert killed == [7] and foreign == [] and sent == [(7, 15)]


# --- the credential file is the signal that consent happened --------------

def test_a_missing_credential_reads_as_zero(monkeypatch, tmp_path):
    monkeypatch.setattr(reauth, "CREDENTIALS_DIR", tmp_path)
    assert reauth.credential_stamp("nobody@example.com") == 0.0


def test_the_stamp_moves_when_the_file_is_rewritten(monkeypatch, tmp_path):
    """Watching the file, rather than parsing the helper's output, is what
    makes this reliable: whichever process performs the exchange writes it."""
    monkeypatch.setattr(reauth, "CREDENTIALS_DIR", tmp_path)
    p = tmp_path / "me@example.com.json"
    p.write_text("{}")
    first = reauth.credential_stamp("me@example.com")
    time.sleep(0.01)
    p.write_text('{"token": "new"}')
    assert reauth.credential_stamp("me@example.com") > first


# --- the URL, surfaced immediately rather than after the fact -------------

def _state_file(tmp_path, monkeypatch, *, expires_in=600, verifier="v" * 43):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    data = {"abc123": {
        "code_verifier": verifier,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=expires_in)).isoformat()}}
    monkeypatch.setattr(reauth, "CREDENTIALS_DIR", tmp_path)
    monkeypatch.setattr(reauth, "STATES_FILE", tmp_path / "oauth_states.json")
    (tmp_path / "oauth_states.json").write_text(json.dumps(data))
    (tmp_path / "me@example.com.json").write_text(
        json.dumps({"client_id": "123-abc.apps.googleusercontent.com"}))
    return data


def test_the_url_is_rebuilt_from_the_pending_state(tmp_path, monkeypatch):
    _state_file(tmp_path, monkeypatch)
    url = reauth.authorization_url("me@example.com")
    assert url.startswith("https://accounts.google.com/o/oauth2/auth?")
    assert "state=abc123" in url
    assert "code_challenge_method=S256" in url
    assert "client_id=123-abc" in url
    assert "gmail.readonly" in url and "calendar.readonly" in url
    assert "localhost%3A8000%2Foauth2callback" in url


def test_the_challenge_is_the_sha256_of_the_verifier(tmp_path, monkeypatch):
    import base64, hashlib, urllib.parse
    _state_file(tmp_path, monkeypatch, verifier="abcdef")
    url = reauth.authorization_url("me@example.com")
    got = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["code_challenge"][0]
    want = base64.urlsafe_b64encode(
        hashlib.sha256(b"abcdef").digest()).rstrip(b"=").decode()
    assert got == want


def test_an_expired_state_yields_no_url(tmp_path, monkeypatch):
    _state_file(tmp_path, monkeypatch, expires_in=-60)
    assert reauth.authorization_url("me@example.com") == ""


def test_no_state_file_yields_no_url(tmp_path, monkeypatch):
    monkeypatch.setattr(reauth, "STATES_FILE", tmp_path / "absent.json")
    monkeypatch.setattr(reauth, "CREDENTIALS_DIR", tmp_path)
    assert reauth.authorization_url("me@example.com") == ""


def test_the_client_id_falls_back_to_the_mcp_registration(tmp_path, monkeypatch):
    """A first run, or one where the client was just replaced, has no
    credential file to read the client id from."""
    monkeypatch.setattr(reauth, "CREDENTIALS_DIR", tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text(json.dumps({"projects": {"/p": {
        "mcpServers": {"workspace-mcp": {"env": {
            "GOOGLE_OAUTH_CLIENT_ID": "fallback-id"}}}}}}))
    monkeypatch.setattr(reauth.Path, "home", staticmethod(lambda: home))
    assert reauth._client_id("me@example.com") == "fallback-id"


# --- the helper, and why it is allowed to sleep ---------------------------

def test_the_helper_can_actually_wait():
    """Without this it announces a retry, finishes, and takes the server the
    callback needs with it."""
    cmd = reauth.helper_command("/bin/claude", "me@example.com", tries=14)
    assert "Bash(sleep:*)" in cmd


def test_the_helper_gets_nothing_else():
    """It is not the drafting run, but it should still hold the minimum."""
    cmd = reauth.helper_command("/bin/claude", "me@example.com", tries=5)
    tools = cmd[cmd.index("--allowedTools") + 1:cmd.index("--max-turns")]
    assert set(tools) == {"mcp__workspace-mcp__list_calendars", "Bash(sleep:*)"}


def test_the_helper_is_told_not_to_give_up_on_the_first_failure():
    cmd = reauth.helper_command("/bin/claude", "me@example.com", tries=14)
    assert "do not stop after the first failure" in cmd[2].lower()


def test_verification_checks_both_tools():
    """Consent proves a grant, not that the tools work -- 2026-09-08 had a
    connected server whose every call failed."""
    cmd = reauth.verify_command("/bin/claude", "me@example.com")
    joined = " ".join(cmd)
    assert "get_events" in joined and "search_gmail_messages" in joined


def test_the_result_field_is_extracted():
    assert reauth.read_result('{"result": "CALENDAR: ok\\nGMAIL: ok"}') \
        == "CALENDAR: ok\nGMAIL: ok"


def test_unparseable_output_is_not_mistaken_for_success():
    assert "ok" not in reauth.read_result("Traceback (most recent call last)").lower()


# --- running it when nothing is wrong ------------------------------------

def test_a_working_setup_is_left_alone(monkeypatch, tmp_path):
    """The case that would have hung. A valid token makes the helper succeed
    instantly and the credential file never changes, so a flow watching only
    that file waits out its timeout and reports failure on a healthy setup."""
    from wa_session.config import Config

    (tmp_path / "p").mkdir()
    cfg = Config(profile_dir=tmp_path / "p" / ".wa-profile",
                 state_dir=tmp_path / "p" / ".wa-state", rotate_after_hours=336.0)

    monkeypatch.setattr("wa_session.drafter.claude_binary", lambda: "/bin/claude")
    monkeypatch.setattr("wa_session.context.load_context",
                        lambda c: type("C", (), {"google_account": "me@x.com"})())
    monkeypatch.setattr(reauth, "_verify",
                        lambda b, a, c: (True, "CALENDAR: ok\nGMAIL: ok"))

    def refuse(*a, **k):
        raise AssertionError("must not start a consent flow when access works")

    monkeypatch.setattr(reauth, "free_the_port", refuse)
    out = reauth.run_reauth(cfg, emit=lambda *a: None)
    assert out["ok"] is True
    assert "already works" in out["note"]


def test_force_consents_anyway(monkeypatch, tmp_path):
    from wa_session.config import Config

    (tmp_path / "p").mkdir()
    cfg = Config(profile_dir=tmp_path / "p" / ".wa-profile",
                 state_dir=tmp_path / "p" / ".wa-state", rotate_after_hours=336.0)
    monkeypatch.setattr("wa_session.drafter.claude_binary", lambda: "/bin/claude")
    monkeypatch.setattr("wa_session.context.load_context",
                        lambda c: type("C", (), {"google_account": "me@x.com"})())
    reached = {}
    monkeypatch.setattr(reauth, "free_the_port",
                        lambda port=8000: (reached.setdefault("yes", True), ([], []))[1])
    monkeypatch.setattr(reauth, "credential_stamp", lambda a: 0.0)
    monkeypatch.setattr(reauth.subprocess, "Popen",
                        lambda *a, **k: type("P", (), {"poll": lambda s: 0,
                                                       "terminate": lambda s: None,
                                                       "wait": lambda s, timeout=0: None,
                                                       "kill": lambda s: None})())
    monkeypatch.setattr(reauth, "authorization_url", lambda a: "")
    monkeypatch.setattr(reauth.time, "sleep", lambda s: None)
    reauth.run_reauth(cfg, emit=lambda *a: None, force=True, timeout_s=0.1)
    assert reached.get("yes"), "--force must skip the up-front check"
