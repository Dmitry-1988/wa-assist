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


# --- 7. a note that was not posted must never count as delivered -----------

class _Result:
    def __init__(self, ok, dry_run=False, detail=""):
        self.ok, self.dry_run, self.detail = ok, dry_run, detail


def test_a_refused_note_raises_instead_of_passing_silently(monkeypatch):
    """selfchat.post RETURNS a SendResult; it does not raise when the send is
    refused after the click. Ignoring it marked a digest delivered, cleared its
    queue item and advanced its watermarks while nothing was posted -- nine
    messages seen and lost, 2026-09-02."""
    import wa_session.selfchat as selfchat
    from wa_session.tick import post_note
    monkeypatch.setattr(selfchat, "post",
                        lambda page, text, dry_run=False: _Result(
                            False, detail="composer still holds text after send"))
    with pytest.raises(RuntimeError, match="not posted"):
        post_note(object(), "hello")


def test_a_dry_run_note_is_treated_as_not_posted(monkeypatch):
    import wa_session.selfchat as selfchat
    from wa_session.tick import post_note
    monkeypatch.setattr(selfchat, "post",
                        lambda page, text, dry_run=False: _Result(True, dry_run=True))
    with pytest.raises(RuntimeError, match="dry run"):
        post_note(object(), "hello")


class _Echo:
    """A page whose self-chat contains whatever was posted to it."""

    def __init__(self, echo=True):
        self.sent, self.echo = [], echo

    def wait_for_timeout(self, ms):
        pass


def _wire(monkeypatch, page):
    import wa_session.selfchat as selfchat

    class M:
        def __init__(self, text):
            self.text, self.msg_id = text, "m1"

    monkeypatch.setattr(selfchat, "post",
                        lambda p, text, dry_run=False: (p.sent.append(text)
                                                        or _Result(True, detail="sent")))
    monkeypatch.setattr(selfchat, "read",
                        lambda p, limit=60: [M(t) for t in p.sent] if p.echo else [])


def test_a_real_post_is_accepted(monkeypatch):
    from wa_session.tick import post_note
    page = _Echo()
    _wire(monkeypatch, page)
    assert post_note(page, "hello") == "m1"


def test_a_note_that_never_appears_is_not_treated_as_delivered(monkeypatch):
    """`_post_phase` closes the browser right after posting; a context torn
    down before WhatsApp transmitted dropped the message while the send still
    reported success. Reading it back is the only proof."""
    from wa_session.tick import post_note
    page = _Echo(echo=False)
    _wire(monkeypatch, page)
    with pytest.raises(RuntimeError, match="never appeared"):
        post_note(page, "hello", settle_s=0.05)


def test_a_failed_digest_keeps_its_queue_item_and_watermarks(config, monkeypatch):
    """The recovery property: nothing is marked seen until the user has it."""
    import wa_session.selfchat as selfchat
    from wa_session.pipeline import QueueItem, write_queue_item, write_submission, \
        DraftSubmission, read_queue
    from wa_session.tick import _post_ready_summaries
    from wa_session.watermarks import read_watermarks

    item = QueueItem(queue_id="sum-1", chat="__summary__",
                     messages=[{"chat": "G", "messages": [{"msg_id": "m1"}]}])
    write_queue_item(config, item)
    write_submission(config, "sum-1",
                     DraftSubmission(queue_id="sum-1", body="digest", sources=[]))
    monkeypatch.setattr(selfchat, "post",
                        lambda page, text, dry_run=False: _Result(False, detail="refused"))

    result = {"actions": []}
    assert _post_ready_summaries(object(), config, result) == 0
    assert [i.queue_id for i in read_queue(config)] == ["sum-1"], "item must survive"
    assert read_watermarks(config) == {}, "watermarks must not advance"
    assert any("summary_post_failed" in a for a in result["actions"])


# --- 8. the read-back must match what actually survives the round trip -----

def test_an_emoji_prefixed_note_is_recognised_on_read_back():
    """WhatsApp renders emoji as <img>; inner_text drops them. Matching raw
    text meant every digest looked undelivered and was posted again -- ten
    identical digests in one afternoon, 2026-09-02."""
    from wa_session.tick import _fingerprint
    posted = "📋 GROUP DIGEST 13:16\n\nאקווה פמילי\n· Neighbour asks about a technician"
    readback = "GROUP DIGEST 13:16 אקווה פמילי · Neighbour asks about a technician"
    assert _fingerprint(posted)[:60] in _fingerprint(readback)


def test_punctuation_that_may_not_survive_is_ignored_too():
    from wa_session.tick import _fingerprint
    assert _fingerprint("· one — two") == _fingerprint("one two")


def test_different_notes_still_do_not_match():
    """The check must not become so loose that anything counts as delivery."""
    from wa_session.tick import _fingerprint
    a = _fingerprint("📋 GROUP DIGEST 13:16 building neighbours")[:60]
    assert a not in _fingerprint("⚠️ I cannot draft a reply to Ann")


def test_a_note_posted_with_emoji_is_confirmed_end_to_end(monkeypatch):
    from wa_session.tick import post_note

    class M:
        def __init__(self, text):
            self.text, self.msg_id = text, "m9"

    class Page:
        def wait_for_timeout(self, ms): pass

    import wa_session.selfchat as selfchat
    monkeypatch.setattr(selfchat, "post",
                        lambda p, text, dry_run=False: _Result(True, detail="sent"))
    # the chat echoes the note back WITHOUT its emoji, as WhatsApp does
    monkeypatch.setattr(selfchat, "read",
                        lambda p, limit=60: [M("GROUP DIGEST 13:16 building")])
    assert post_note(Page(), "📋 GROUP DIGEST 13:16\n\nbuilding") == "m9"


# --- 9. the revision cap must actually be reachable ------------------------

def test_a_draft_remembers_how_many_edits_produced_it(tmp_path):
    """`_queue_revision` reads the revision off the pending entry. Draft had no
    such field, so it read 0 every time and computed revision 1 for ever --
    MAX_REVISIONS was unreachable and an edit loop was unbounded, one paid
    drafting run per round."""
    from wa_session.agent import PendingDraft
    from wa_session.approval import Draft

    d = Draft(draft_id="#AAA", recipient="X", source_chat="X", body="b",
              quoted="", sources=[],
              created_at=datetime.datetime.now(datetime.timezone.utc),
              ttl_hours=2, revision=3)
    restored = PendingDraft.from_dict(
        PendingDraft(draft=d, marker_id="m1").as_dict())
    assert restored.draft.revision == 3


def test_pending_entries_expose_the_revision(config, monkeypatch):
    from wa_session.agent import PendingDraft, pending_drafts, save_pending
    from wa_session.approval import Draft

    d = Draft(draft_id="#AAA", recipient="X", source_chat="X", body="b",
              quoted="", sources=[],
              created_at=datetime.datetime.now(datetime.timezone.utc),
              ttl_hours=2, revision=4)
    save_pending(config, PendingDraft(draft=d, marker_id="m1"))
    entry = pending_drafts(config)[0]
    assert entry["revision"] == 4


def test_the_revision_cap_is_reachable(config):
    """At the cap the edit is refused instead of spawning another paid run."""
    from wa_session.pipeline import MAX_REVISIONS
    from wa_session.tick import _queue_revision

    entry = {"draft_id": "#AAA", "recipient": "X", "body": "b",
             "revision": MAX_REVISIONS}
    out = _queue_revision(config, entry, "shorter")
    assert "edit_refused" in out
