"""The approval gate. A false APPROVE sends something the user did not sanction,
so these tests lean hard on ambiguity resolving to "do not send".
"""

from datetime import datetime, timedelta, timezone

import pytest

from wa_session.approval import (
    Command,
    Decision,
    Draft,
    Journal,
    Status,
    new_draft_id,
    parse_command,
    render_draft_message,
    resolve,
)

ID = "#A7K"
NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)


def make_draft(**kw) -> Draft:
    base = dict(
        draft_id=ID,
        recipient="שכנים בבניין",
        source_chat="שכנים בבניין",
        body="שלום, יש לי כסא אוכל פנוי.",
        created_at=NOW,
    )
    base.update(kw)
    return Draft(**base)


@pytest.fixture
def journal(tmp_path) -> Journal:
    return Journal(tmp_path / "journal.jsonl")


# --- ids -------------------------------------------------------------------

def test_ids_are_distinctive_and_unique():
    ids = {new_draft_id() for _ in range(500)}
    assert len(ids) > 300                      # effectively no collisions
    assert all(i.startswith("#") and len(i) == 4 for i in ids)
    # Ambiguous glyphs are excluded so a typed approval cannot be misread.
    assert not any(set("IO01") & set(i[1:]) for i in ids)


# --- the safety-critical parse ---------------------------------------------

@pytest.mark.parametrize("text", [f"OK {ID}", f"ok {ID}", f"Ok {ID}", f"approve {ID}", f"send {ID}", f"OK {ID}."])
def test_clean_approval(text):
    assert parse_command(text, ID).decision is Decision.APPROVE


@pytest.mark.parametrize(
    "text",
    [
        f"OK {ID} but change the time",      # approval with a caveat
        f"OK {ID} — actually make it shorter",
        f"{ID} looks good",
        f"maybe OK {ID}?",
        f"thinking about {ID}",
        f"OK {ID} OK",
        f"not OK {ID}",
    ],
)
def test_qualified_approval_is_never_approval(text):
    # These must NOT send. A caveat attached to "OK" means the user wanted a
    # change, and sending the original would be exactly wrong.
    assert parse_command(text, ID).decision is not Decision.APPROVE


@pytest.mark.parametrize("text", ["yes", "sure", "👍", "ok", "send it", "go ahead"])
def test_approval_without_the_id_does_nothing(text):
    assert parse_command(text, ID).decision is Decision.NONE


@pytest.mark.parametrize("text", [f"NO {ID}", f"no {ID}", f"cancel {ID}", f"reject {ID}", f"drop {ID}"])
def test_rejection(text):
    assert parse_command(text, ID).decision is Decision.REJECT


def test_edit_carries_instructions():
    cmd = parse_command(f"EDIT {ID}: make it shorter and add a time", ID)
    assert cmd.decision is Decision.EDIT
    assert cmd.instructions == "make it shorter and add a time"


def test_edit_without_instructions_is_ambiguous():
    assert parse_command(f"EDIT {ID}", ID).decision is Decision.AMBIGUOUS


def test_command_for_a_different_draft_is_ignored():
    assert parse_command("OK #ZZZ", ID).decision is Decision.NONE


def test_empty_message():
    assert parse_command("", ID).decision is Decision.NONE


# --- expiry ----------------------------------------------------------------

def test_expiry_boundary():
    draft = make_draft(ttl_hours=2)
    assert draft.is_expired(NOW + timedelta(hours=1, minutes=59)) is False
    assert draft.is_expired(NOW + timedelta(hours=2)) is True


def test_expired_draft_resolves_to_reject_even_with_an_ok(journal):
    draft = make_draft(ttl_hours=1)
    msgs = [{"text": f"OK {ID}", "msg_id": "m1"}]
    cmd = resolve(draft, msgs, journal, now=NOW + timedelta(hours=3))
    assert cmd.decision is Decision.REJECT
    assert "expired" in cmd.reason


def test_silence_is_never_consent(journal):
    cmd = resolve(make_draft(), [], journal, now=NOW)
    assert cmd.decision is Decision.NONE


# --- replay / double-send protection ---------------------------------------

def test_send_attempt_is_recorded_before_sending(journal):
    draft = make_draft()
    journal.record_send_attempt(draft.draft_id, draft.recipient)
    assert journal.already_sent(draft.draft_id)
    # Even a fresh OK must not produce a second send.
    cmd = resolve(draft, [{"text": f"OK {ID}", "msg_id": "m9"}], journal, now=NOW)
    assert cmd.decision is Decision.NONE
    assert cmd.reason == "already sent"


def test_journal_survives_a_restart(tmp_path):
    path = tmp_path / "j.jsonl"
    first = Journal(path)
    first.record_send_attempt(ID, "someone")
    first.record_command("m1", Decision.APPROVE, ID)
    second = Journal(path)
    assert second.already_sent(ID)
    assert second.command_seen("m1")


def test_a_command_message_is_consumed_only_once(journal):
    draft = make_draft()
    msgs = [{"text": f"OK {ID}", "msg_id": "m1"}]
    assert resolve(draft, msgs, journal, now=NOW).decision is Decision.APPROVE
    # Re-reading the same self-chat message must not approve again.
    assert resolve(draft, msgs, journal, now=NOW).decision is Decision.NONE


def test_corrupt_journal_lines_are_skipped(tmp_path):
    path = tmp_path / "j.jsonl"
    path.write_text('{"kind":"send_attempt","draft_id":"#AAA"}\nnot json\n')
    journal = Journal(path)
    assert journal.already_sent("#AAA")


def test_journal_file_is_owner_only(journal):
    journal.record_command("m1", Decision.APPROVE, ID)
    assert journal.path.stat().st_mode & 0o777 == 0o600


# --- ordering --------------------------------------------------------------

def test_first_decisive_message_wins(journal):
    msgs = [
        {"text": "unrelated", "msg_id": "m1"},
        {"text": f"NO {ID}", "msg_id": "m2"},
        {"text": f"OK {ID}", "msg_id": "m3"},
    ]
    assert resolve(make_draft(), msgs, journal, now=NOW).decision is Decision.REJECT


# --- what the user actually sees -------------------------------------------

def test_rendered_draft_contains_the_exact_body_and_recipient():
    draft = make_draft(sources=["calendar:1"], quoted="יש למישהו כסא אוכל?")
    out = render_draft_message(draft, audience="group, ~40 people")
    assert draft.body in out                 # verbatim, not a paraphrase
    assert draft.recipient in out
    assert "group, ~40 people" in out
    assert f"OK {ID}" in out and f"NO {ID}" in out
    assert "calendar:1" in out


def test_rendered_draft_states_when_no_sources_were_used():
    assert "Sources: none" in render_draft_message(make_draft())


# --- self-chat ordering ----------------------------------------------------

def test_read_after_returns_nothing_when_marker_is_not_visible():
    """A draft scrolled out of view must not inherit an older 'OK'.

    Returning the whole list here would let an approval posted BEFORE the draft
    approve it.
    """
    from wa_session.messages import Message
    from wa_session.selfchat import read_after

    class FakePage:
        pass

    msgs = [Message(text="OK #OL1", msg_id="m1"), Message(text="note", msg_id="m2")]
    # Exercise the pure slicing logic directly.
    import wa_session.selfchat as sc

    original = sc.read
    sc.read = lambda page, limit=60: msgs
    try:
        assert read_after(FakePage(), "missing-id") == []
        assert read_after(FakePage(), "m1") == [msgs[1]]
        assert read_after(FakePage(), "") == msgs
    finally:
        sc.read = original


def test_find_message_id_prefers_the_most_recent_match():
    from wa_session.messages import Message
    from wa_session.selfchat import find_message_id

    msgs = [
        Message(text="DRAFT #AAA old", msg_id="m1"),
        Message(text="DRAFT #AAA new", msg_id="m2"),
    ]
    assert find_message_id(msgs, "#AAA") == "m2"
    assert find_message_id(msgs, "#ZZZ") == ""


def test_inspecting_a_decision_does_not_spend_it(journal):
    """poll() must be read-only, or deliver() would find nothing left."""
    draft = make_draft()
    msgs = [{"text": f"OK {ID}", "msg_id": "m1"}]
    assert resolve(draft, msgs, journal, now=NOW, consume=False).decision is Decision.APPROVE
    # Still there on a second look...
    assert resolve(draft, msgs, journal, now=NOW, consume=False).decision is Decision.APPROVE
    # ...and consumable exactly once when it is acted on.
    assert resolve(draft, msgs, journal, now=NOW, consume=True).decision is Decision.APPROVE
    assert resolve(draft, msgs, journal, now=NOW, consume=True).decision is Decision.NONE


def test_a_retired_draft_can_never_be_approved(journal):
    """Superseded drafts stay visible in the self-chat; typing an old id must
    not resurrect one -- especially a factually wrong one."""
    draft = make_draft()
    journal.retire(draft.draft_id, "superseded by a corrected draft")
    cmd = resolve(draft, [{"text": f"OK {ID}", "msg_id": "m1"}], journal, now=NOW)
    assert cmd.decision is Decision.REJECT
    assert cmd.reason == "withdrawn"


def test_retirement_survives_a_restart(tmp_path):
    path = tmp_path / "j.jsonl"
    Journal(path).retire("#XYZ", "superseded")
    assert Journal(path).is_retired("#XYZ") is True
