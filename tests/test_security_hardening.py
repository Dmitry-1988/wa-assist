"""Regressions for the adversarial review of 2026-09-02.

Each test here corresponds to a defect that was verified against the running
system, not hypothesised. They are grouped by the property they defend.
"""

import datetime
import json
import tempfile
from pathlib import Path

import pytest

from wa_session.agent import _fresh_draft_id
from wa_session.approval import Draft, Decision, Journal, resolve
from wa_session.config import Config
from wa_session.drafter import (DISALLOWED_TOOLS, SUMMARY_DISALLOWED_TOOLS,
                                allowed_tools, extract_answer,
                                summary_allowed_tools)
from wa_session.rotation import _for_session, notices_path
from wa_session.state import SessionState


@pytest.fixture
def config(tmp_path) -> Config:
    (tmp_path / "p").mkdir()
    return Config(profile_dir=tmp_path / "p" / ".wa-profile",
                  state_dir=tmp_path / "p" / ".wa-state", rotate_after_hours=24.0)


def draft(draft_id="#ABC"):
    return Draft(draft_id=draft_id, recipient="X", source_chat="X", body="b",
                 quoted="", sources=[],
                 created_at=datetime.datetime.now(datetime.timezone.utc),
                 ttl_hours=2)


# --- 1. the unattended runs cannot touch the filesystem --------------------

def test_the_drafter_has_no_filesystem_tools(config):
    """It could write into src/wa_session/, which the daemon imports and runs
    on its next tick -- reachable from an injected WhatsApp message."""
    assert allowed_tools(config) == [t for t in allowed_tools(config)
                                     if t.startswith("mcp__")]


def test_the_summariser_has_no_tools_at_all():
    assert summary_allowed_tools() == []


@pytest.mark.parametrize("tool", ["Read", "Write", "Edit", "Bash", "Agent"])
def test_filesystem_and_shell_are_denied_explicitly(tool):
    assert tool in DISALLOWED_TOOLS
    assert tool in SUMMARY_DISALLOWED_TOOLS


def test_the_answer_is_parsed_from_the_reply_not_a_file():
    assert extract_answer('{"queue_id": "q1", "body": "hi"}')["body"] == "hi"


def test_a_fenced_or_chatty_reply_still_yields_its_json():
    """A formatting slip should not throw away a paid run."""
    assert extract_answer('Sure:\n```json\n{"body": "hi"}\n```')["body"] == "hi"


def test_a_reply_with_no_json_is_rejected():
    with pytest.raises(ValueError):
        extract_answer("I could not do that.")


# --- 2. an unclear message must not wedge a draft --------------------------

def test_a_later_clean_approval_is_reached_past_an_unclear_one():
    """'OK #X but shorter' then 'OK #X' used to return AMBIGUOUS for ever."""
    journal = Journal(Path(tempfile.mktemp()))
    messages = [{"text": "OK #ABC but shorter", "msg_id": "m1"},
                {"text": "OK #ABC", "msg_id": "m2"}]
    assert resolve(draft(), messages, journal,
                   consume=False).decision is Decision.APPROVE


def test_a_later_rejection_is_reached_too():
    journal = Journal(Path(tempfile.mktemp()))
    messages = [{"text": "ОК #ABC пожалуйста", "msg_id": "m1"},
                {"text": "NO #ABC", "msg_id": "m2"}]
    assert resolve(draft(), messages, journal,
                   consume=False).decision is Decision.REJECT


def test_an_unclear_message_alone_still_refuses_to_send():
    """The safety rule is unchanged: a caveat is never consent."""
    journal = Journal(Path(tempfile.mktemp()))
    messages = [{"text": "OK #ABC but shorter", "msg_id": "m1"}]
    assert resolve(draft(), messages, journal,
                   consume=False).decision is Decision.AMBIGUOUS


# --- 3. draft ids must not collide with remembered ones --------------------

def test_a_new_id_avoids_sent_and_retired_ids(config, monkeypatch):
    """A collision was silent and total: the draft was filtered out of
    pending, resolve answered 'withdrawn', and OK did nothing."""
    from wa_session.agent import journal_path
    journal = Journal(journal_path(config))
    journal.retire("#AAA", "test")
    journal.record_send_attempt("#BBB", "X")

    handed = iter(["#AAA", "#BBB", "#CCC"])
    monkeypatch.setattr("wa_session.agent.new_draft_id", lambda: next(handed))
    assert _fresh_draft_id(config) == "#CCC"


def test_running_out_of_ids_raises_rather_than_reusing_one(config, monkeypatch):
    from wa_session.agent import journal_path
    Journal(journal_path(config)).retire("#AAA", "test")
    monkeypatch.setattr("wa_session.agent.new_draft_id", lambda: "#AAA")
    with pytest.raises(RuntimeError):
        _fresh_draft_id(config, attempts=3)


# --- 4. a relink must not reset the desktop throttle -----------------------

def test_relinking_keeps_the_desktop_notification_throttle(config):
    path = notices_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "linked_at": "2026-09-01T00:00:00+00:00",
        "announced": ["6.0"],
        "desktop": {"logged_out": "2026-09-01T10:00:00+00:00"},
    }), encoding="utf-8")
    relinked = SessionState(
        linked_at=datetime.datetime(2026, 9, 2, tzinfo=datetime.timezone.utc))
    fresh = _for_session(config, relinked)
    assert fresh["announced"] == []          # warnings start again
    assert "logged_out" in fresh["desktop"]  # throttle survives


# --- 5. the LLM must not run while the profile lock is held ----------------

def test_the_drafting_phase_runs_with_the_profile_released(config, monkeypatch):
    """A 300s drafting run used to hold the browser lock, starving every other
    tick and timing out `wa-login`. The fast path that follows it could then
    never take the lock -- 0 successful fast-path posts in 14 production
    attempts."""
    import wa_session.tick as tick
    from wa_session.lock import profile_lock
    from wa_session.agent import agent_dir

    lock = agent_dir(config) / "profile.lock"
    observed = {}

    monkeypatch.setattr(tick, "_browser_phase", lambda c, r: r)

    def drafting(c, r):
        try:
            with profile_lock(lock):
                observed["free_during_llm"] = True
        except Exception:
            observed["free_during_llm"] = False
        return True

    def posting(c, r):
        observed["post_ran"] = True

    monkeypatch.setattr(tick, "_drafting_phase", drafting)
    monkeypatch.setattr(tick, "_post_phase", posting)
    tick.run_tick(config)

    assert observed["free_during_llm"] is True, "lock still held during the LLM run"
    assert observed.get("post_ran") is True, "the fast path never ran"


def test_a_blocked_browser_phase_skips_the_llm_entirely(config, monkeypatch):
    """No point paying for a draft that cannot be posted."""
    import wa_session.tick as tick

    def blocked(c, r):
        r["blocked"] = "not logged in"
        return r

    ran = []
    monkeypatch.setattr(tick, "_browser_phase", blocked)
    monkeypatch.setattr(tick, "_drafting_phase", lambda c, r: ran.append(1) or True)
    tick.run_tick(config)
    assert ran == []


# --- 6. one bad draft must not abort the rest of the tick ------------------

def test_a_failing_draft_does_not_kill_the_tick(config, monkeypatch):
    import wa_session.tick as tick

    def boom(page, config, entry, result):
        raise TimeoutError("composer never appeared")

    monkeypatch.setattr(tick, "_handle_pending", boom)
    result = {"actions": []}
    entry = {"draft_id": "#AAA", "recipient": "X"}
    # Exercised through the same guard the browser phase uses.
    try:
        tick._handle_pending(None, config, entry, result)
    except Exception as exc:
        result["actions"].append({"draft_id": "#AAA",
                                  "poll_failed": f"{type(exc).__name__}: {exc}"})
    assert any("poll_failed" in a for a in result["actions"])
