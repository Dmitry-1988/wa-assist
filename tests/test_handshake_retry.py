"""`pending` is a race, not a verdict.

Between 2026-09-30 and 2026-10-03 one queue item was refused 1821 times with
`workspace-mcp status=pending` while Gmail and Calendar were in fact reachable
-- verified by calling them. The init event is emitted once per process, so the
handshake could only be re-read by spawning again, and nothing did: every tick
made the same cold start and lost the same race. The run is killed AT init,
before a token is spent, so another spawn costs nothing.

What must NOT happen is retrying a definitive refusal. `failed` is what a
server whose command cannot be found reports (verified by taking uvx off the
PATH), and an auth failure is global -- retrying either just pays twice.
"""

import io
import json

import pytest

from conftest import write_context

from wa_session.config import Config
from wa_session.drafter import (
    HANDSHAKE_RETRIES,
    handshake_retryable,
    run_drafter,
)
from wa_session.pipeline import QueueItem


@pytest.fixture
def config(tmp_path) -> Config:
    (tmp_path / "p").mkdir()
    cfg = Config(profile_dir=tmp_path / "p" / ".wa-profile",
                 state_dir=tmp_path / "p" / ".wa-state", rotate_after_hours=24.0)
    write_context(cfg)
    return cfg


# --- which refusals are worth another spawn -------------------------------

@pytest.mark.parametrize("status", ["pending", "connecting"])
def test_a_server_still_connecting_is_retryable(status):
    assert handshake_retryable(f"workspace-mcp status={status}") is True


@pytest.mark.parametrize("detail", [
    "workspace-mcp status=failed",            # command not found; waiting won't help
    "workspace-mcp status=needs-auth",        # a human must act
    "workspace-mcp is not configured for this run",
    "workspace-mcp connected but not offering get_events",
    "connected",
])
def test_a_definitive_refusal_is_not_retryable(detail):
    assert handshake_retryable(detail) is False


def test_prose_mentioning_pending_is_not_retryable():
    """Matched exactly, not by substring.

    A tool result is free to use the word in a sentence; auth is global, so
    retrying one of those would buy the identical refusal a second time.
    """
    assert handshake_retryable(
        "Google Authentication Needed: authorization pending") is False


# --- the retry in run_drafter ----------------------------------------------

class _Spawner:
    """Stands in for `_attempt_draft`, returning a scripted outcome per call."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def __call__(self, cmd, summary, item, config, timeout_s):
        self.calls += 1
        outcome = self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)]
        return {**summary, **outcome}


PENDING = {"ok": False, "context_unavailable": "workspace-mcp status=pending"}
GOOD = {"ok": True, "returncode": 0, "mcp": "connected"}


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch):
    monkeypatch.setattr("wa_session.drafter.time.sleep", lambda s: None)


def _run(monkeypatch, config, spawner):
    monkeypatch.setattr("wa_session.drafter._attempt_draft", spawner)
    monkeypatch.setattr("wa_session.drafter.claude_binary", lambda: "/bin/true")
    return run_drafter(QueueItem(queue_id="q1", chat="S",
                                 messages=[{"sender": "S", "text": "hi"}]),
                       config)


def test_a_cold_start_is_retried_and_succeeds(monkeypatch, config):
    """The first spawn does the resolving, so the second runs warm."""
    spawner = _Spawner(PENDING, GOOD)
    outcome = _run(monkeypatch, config, spawner)
    assert spawner.calls == 2
    assert outcome["ok"] is True
    assert outcome["handshake_attempts"] == 2


def test_a_persistent_pending_still_refuses(monkeypatch, config):
    """Retrying is not the same as giving up on the gate."""
    spawner = _Spawner(PENDING)
    outcome = _run(monkeypatch, config, spawner)
    assert spawner.calls == HANDSHAKE_RETRIES + 1
    assert outcome["ok"] is False
    assert outcome["context_unavailable"] == "workspace-mcp status=pending"
    assert outcome["handshake_attempts"] == HANDSHAKE_RETRIES + 1


def test_a_definitive_refusal_is_not_spawned_again(monkeypatch, config):
    spawner = _Spawner({"ok": False,
                        "context_unavailable": "workspace-mcp status=failed"})
    outcome = _run(monkeypatch, config, spawner)
    assert spawner.calls == 1
    assert "handshake_attempts" not in outcome


def test_an_auth_failure_is_not_spawned_again(monkeypatch, config):
    """Auth is global: the first failure is enough to abandon the run."""
    spawner = _Spawner({"ok": False,
                        "context_unavailable": "Google Authentication Needed"})
    outcome = _run(monkeypatch, config, spawner)
    assert spawner.calls == 1


def test_a_refused_handshake_reports_the_exit_status(monkeypatch, config):
    """The field whose absence hid the real cause for three days.

    A negative code is our own kill at init; a POSITIVE one is the CLI dying
    on its own, which means the handshake verdict is a symptom and the layer
    to look at is Claude, not Google.
    """
    import wa_session.drafter as drafter

    init = json.dumps({
        "type": "system", "subtype": "init",
        "mcp_servers": [{"name": "workspace-mcp", "status": "pending"}],
        "tools": [],
    })

    class _Proc:
        """A CLI that announces unconnected servers and exits 1 by itself.

        Fresh per spawn: `pending` is retried, so a stream shared between
        attempts would be empty on the second and report no handshake at all.
        """

        def __init__(self):
            self.returncode = 1
            self.stdout = io.StringIO(init + "\n")
            self.stderr = io.StringIO("")

        def kill(self): pass
        def wait(self): return 1

    monkeypatch.setattr(drafter, "claude_binary", lambda: "/bin/true")
    monkeypatch.setattr(drafter.subprocess, "Popen", lambda *a, **kw: _Proc())
    outcome = run_drafter(QueueItem(queue_id="q1", chat="S",
                                    messages=[{"sender": "S", "text": "hi"}]),
                          config)
    assert outcome["ok"] is False
    assert outcome["context_unavailable"] == "workspace-mcp status=pending"
    assert outcome["returncode"] == 1


def test_a_good_run_is_not_spawned_again(monkeypatch, config):
    spawner = _Spawner(GOOD)
    outcome = _run(monkeypatch, config, spawner)
    assert spawner.calls == 1
    assert outcome["ok"] is True
    # A clean first run must not be labelled as having needed retries.
    assert "handshake_attempts" not in outcome
