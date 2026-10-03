"""Blame the layer that is actually broken.

2026-09-30 to 10-03, the three-day outage. Every drafting run refused with
`workspace-mcp status=pending`, so the whole diagnosis went to the MCP server,
Google's OAuth tokens and the uv cache -- and the real cause was the Claude
subscription. An unauthenticated CLI emits its init event with servers still
unconnected and then exits 1, so `mcp_health` fired on the symptom and killed
the run before the actual error was visible.

The evidence was in `daemon.log` the whole time and nobody read it: the
SUMMARISER, which gets zero tools and no MCP at all, was failing identically --
617 runs, `returncode: 1`, `stderr: ""` -- and a toolless run cannot fail for
want of a tool. The stall notice meanwhile recommended `reauth`, which would
have done nothing.

So a notice now states which layer failed, having checked, and the handshake
path reports the exit status it used to throw away.
"""

import pytest

from wa_session import drafter
from wa_session.config import Config
from wa_session.pipeline import QueueItem, read_queue, write_queue_item
from wa_session.tick import CONTEXT_STALL_ATTEMPTS, _report_stalled_items, _stall_note

from test_tick_retry import FakePage, fake_selfchat  # noqa: F401  (fixture)


@pytest.fixture
def config(tmp_path) -> Config:
    (tmp_path / "p").mkdir()
    return Config(profile_dir=tmp_path / "p" / ".wa-profile",
                  state_dir=tmp_path / "p" / ".wa-state", rotate_after_hours=24.0)


PENDING = "workspace-mcp status=pending"


# --- what the note says ----------------------------------------------------

def test_a_broken_cli_is_not_reported_as_a_google_problem():
    """The actual 2026-09-30 failure. `reauth` must NOT be the advice."""
    item = QueueItem(queue_id="q1", chat="S", attempts=400,
                     last_context_error=PENDING)
    note = _stall_note(item, False, "the claude CLI itself is failing (exit 1)")
    assert "NOT Google" in note
    assert "/login" in note
    assert "reauth" not in note
    # The handshake verdict is still shown, but labelled as what it is.
    assert PENDING in note and "symptom" in note


def test_a_working_cli_points_at_google_instead():
    item = QueueItem(queue_id="q1", chat="S", attempts=10,
                     last_context_error="a tool call failed: authentication needed")
    note = _stall_note(item, True, "the claude CLI runs")
    assert "reauth" in note
    assert "NOT Google" not in note


def test_either_way_nothing_was_sent():
    for ok in (True, False):
        note = _stall_note(QueueItem(queue_id="q1", chat="S", attempts=9), ok, "x")
        assert "Nothing has been sent" in note
        assert "still queued" in note


# --- the check runs before the claim ---------------------------------------

def test_the_cli_is_checked_before_blaming_anything(config, monkeypatch):
    write_queue_item(config, QueueItem(queue_id="q1", chat="S",
                                       attempts=CONTEXT_STALL_ATTEMPTS,
                                       last_context_error=PENDING))
    monkeypatch.setattr("wa_session.tick.cli_healthy",
                        lambda: (False, "the claude CLI itself is failing (exit 1)"))
    page, result = FakePage(), {"actions": []}
    _report_stalled_items(page, config, result)
    assert "NOT Google" in page.posted[0]
    # Recorded in the log too, so the next diagnosis starts from the answer.
    assert {"cli_check": "the claude CLI itself is failing (exit 1)"} in result["actions"]


def test_the_check_runs_once_per_tick_not_once_per_item(config, monkeypatch):
    """It spawns a process; two stalled chats must not mean two checks."""
    for qid in ("q1", "q2"):
        write_queue_item(config, QueueItem(queue_id=qid, chat=qid,
                                          attempts=CONTEXT_STALL_ATTEMPTS))
    calls = []

    def check():
        calls.append(1)
        return True, "the claude CLI runs"

    monkeypatch.setattr("wa_session.tick.cli_healthy", check)
    _report_stalled_items(FakePage(), config, {"actions": []})
    assert len(calls) == 1
    assert len(read_queue(config)) == 2      # both still queued


def test_the_check_is_skipped_while_nothing_is_stalled(config, monkeypatch):
    """No notice due means no spawn: this must not run every tick."""
    write_queue_item(config, QueueItem(queue_id="q1", chat="S", attempts=1))
    monkeypatch.setattr("wa_session.tick.cli_healthy",
                        lambda: pytest.fail("checked with nothing to report"))
    _report_stalled_items(FakePage(), config, {"actions": []})


# --- the check itself ------------------------------------------------------

class _Completed:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def test_a_clean_exit_means_the_cli_runs(monkeypatch):
    monkeypatch.setattr(drafter, "claude_binary", lambda: "/bin/true")
    monkeypatch.setattr(drafter.subprocess, "run",
                        lambda *a, **kw: _Completed(0, "ok"))
    ok, detail = drafter.cli_healthy()
    assert ok is True and "runs" in detail


def test_the_exact_failure_that_cost_three_days(monkeypatch):
    """Exit 1 with nothing on stderr -- measured across 617 runs."""
    monkeypatch.setattr(drafter, "claude_binary", lambda: "/bin/true")
    monkeypatch.setattr(drafter.subprocess, "run",
                        lambda *a, **kw: _Completed(1, "", ""))
    ok, detail = drafter.cli_healthy()
    assert ok is False
    assert "CLI itself is failing" in detail and "exit 1" in detail


def test_no_mcp_server_is_started_for_the_check(monkeypatch):
    """Otherwise it cannot tell the CLI apart from the server."""
    seen = {}
    monkeypatch.setattr(drafter, "claude_binary", lambda: "/bin/true")

    def run(cmd, **kw):
        seen["cmd"] = cmd
        return _Completed(0)

    monkeypatch.setattr(drafter.subprocess, "run", run)
    drafter.cli_healthy()
    assert "--strict-mcp-config" in seen["cmd"]
    assert "--mcp-config" not in seen["cmd"]        # strict + none = zero servers
    # And it stays inside the same tool boundary as everything else.
    for denied in ("Bash", "Read", "Write", "Edit", "Agent"):
        assert denied in seen["cmd"]


def test_a_hanging_cli_is_a_failure_not_an_exception(monkeypatch):
    monkeypatch.setattr(drafter, "claude_binary", lambda: "/bin/true")

    def run(cmd, **kw):
        raise drafter.subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(drafter.subprocess, "run", run)
    ok, detail = drafter.cli_healthy()
    assert ok is False and "in time" in detail


def test_a_missing_binary_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(drafter, "claude_binary", lambda: None)
    ok, detail = drafter.cli_healthy()
    assert ok is False and "not found" in detail
