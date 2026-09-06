"""A message reported once is never reported again.

This was one watermark per chat: the msg_id of the last message in a POSTED
digest. Two failures killed it.

`advance` overwrote the mark with the last message of whatever was captured.
When a capture lost its tail, that "last" message was an OLD one, so the mark
moved BACKWARDS -- and every digest after it re-reported everything that
followed. Seen 2026-09-06: quiet groups appearing in digest after digest with
nothing new, and content the user had already read three times.

And one mark cannot answer "has the user been told this?" without trusting
that the capture and the mark are in the same order and neither has been
disturbed. A set of reported ids answers it directly, only ever grows, and
cannot be rewound.
"""

import pytest

from wa_session.config import Config
from wa_session.watermarks import (CONTEXT_MESSAGES, ChatState, advance,
                                   read_reported, unseen, write_reported)


@pytest.fixture
def config(tmp_path) -> Config:
    (tmp_path / "p").mkdir()
    (tmp_path / "p" / ".wa-agent").mkdir(parents=True, exist_ok=True)
    return Config(profile_dir=tmp_path / "p" / ".wa-profile",
                  state_dir=tmp_path / "p" / ".wa-state", rotate_after_hours=24.0)


def msgs(*ids):
    return [{"msg_id": i, "text": i} for i in ids]


def seen_of(config, chat):
    return read_reported(config).get(chat, ChatState()).seen


# --- what is new ----------------------------------------------------------

def test_everything_is_new_the_first_time():
    got = unseen(msgs("a", "b"), None)
    assert [m["msg_id"] for m in got.messages] == ["a", "b"]
    assert got.gap is False and got.context == []


def test_reported_messages_never_come_back():
    got = unseen(msgs("a", "b", "c"), ChatState(seen={"a", "b"}))
    assert [m["msg_id"] for m in got.messages] == ["c"]
    assert got.gap is False


def test_a_chat_with_nothing_new_reports_nothing():
    got = unseen(msgs("a", "b"), ChatState(seen={"a", "b"}))
    assert got.messages == [] and got.gap is False


def test_a_deeper_capture_cannot_resurrect_old_messages():
    """The regression in one line: reaching further back than last time must
    not turn already-reported messages back into news."""
    state = ChatState(seen={"c", "d"})
    got = unseen(msgs("a", "b", "c", "d", "e"), state)
    assert [m["msg_id"] for m in got.messages] == ["e"], \
        "a, b are older than what was reported and were never new"


# --- context --------------------------------------------------------------

def test_the_messages_just_before_the_new_ones_come_back_as_context():
    """A reply is nonsense without the message it answers."""
    got = unseen(msgs("a", "b", "c", "d"), ChatState(seen={"a", "b", "c"}))
    assert [m["msg_id"] for m in got.messages] == ["d"]
    assert [m["msg_id"] for m in got.context] == ["a", "b", "c"]


def test_context_is_bounded():
    ids = [f"m{i}" for i in range(30)]
    got = unseen(msgs(*ids), ChatState(seen=set(ids[:-1])))
    assert len(got.context) == CONTEXT_MESSAGES


def test_context_is_never_marked_reported(config):
    """Otherwise it disappears from the digest that genuinely needs it."""
    advance(config, [{"chat": "G",
                      "messages": msgs("new"),
                      "context": msgs("old")}])
    assert seen_of(config, "G") == {"new"}


# --- gaps -----------------------------------------------------------------

def test_no_overlap_at_all_is_a_gap():
    got = unseen(msgs("x", "y"), ChatState(seen={"long-gone"}))
    assert got.messages == msgs("x", "y")
    assert got.gap is True and "not covered" in got.reason


def test_a_first_digest_is_not_a_gap():
    assert unseen(msgs("a"), ChatState()).gap is False


def test_overflowing_the_cap_is_reported():
    got = unseen(msgs(*[f"m{i}" for i in range(10)]), None, cap=4)
    assert [m["msg_id"] for m in got.messages] == ["m6", "m7", "m8", "m9"]
    assert got.gap is True and "left out" in got.reason


# --- the record itself ----------------------------------------------------

def test_advancing_records_what_was_reported(config):
    advance(config, [{"chat": "A", "messages": msgs("a1", "a2")},
                     {"chat": "B", "messages": msgs("b1")}])
    assert seen_of(config, "A") == {"a1", "a2"}
    assert seen_of(config, "B") == {"b1"}


def test_the_record_only_ever_grows(config):
    """The rewind, made impossible. `advance` used to assign, so a truncated
    capture moved the mark backwards and re-opened everything after it."""
    advance(config, [{"chat": "A", "messages": msgs("a1", "a2", "a3")}])
    advance(config, [{"chat": "A", "messages": msgs("a1")}])   # a short capture
    assert seen_of(config, "A") == {"a1", "a2", "a3"}


def test_a_truncated_capture_does_not_re_open_old_messages(config):
    advance(config, [{"chat": "A", "messages": msgs("a1", "a2", "a3")}])
    advance(config, [{"chat": "A", "messages": msgs("a1")}])
    got = unseen(msgs("a1", "a2", "a3", "a4"), read_reported(config)["A"])
    assert [m["msg_id"] for m in got.messages] == ["a4"]


def test_other_chats_are_untouched(config):
    advance(config, [{"chat": "A", "messages": msgs("a1")}])
    advance(config, [{"chat": "B", "messages": msgs("b1")}])
    assert seen_of(config, "A") == {"a1"}


def test_an_unreadable_record_means_nothing_seen(config):
    from wa_session.watermarks import watermarks_path
    watermarks_path(config).write_text("{not json", encoding="utf-8")
    assert read_reported(config) == {}


def test_the_record_is_capped(config):
    from wa_session.watermarks import MAX_SEEN
    advance(config, [{"chat": "A",
                      "messages": msgs(*[f"m{i:05}" for i in range(MAX_SEEN + 50)])}])
    assert len(seen_of(config, "A")) == MAX_SEEN


def test_the_file_is_owner_only(config):
    from wa_session.watermarks import watermarks_path
    advance(config, [{"chat": "A", "messages": msgs("a1")}])
    assert oct(watermarks_path(config).stat().st_mode)[-3:] == "600"


# --- migrating from the single-mark format --------------------------------

def test_a_legacy_mark_is_read_as_a_floor(config):
    from wa_session.watermarks import watermarks_path
    watermarks_path(config).write_text(
        '{"chats": {"G": "ID7"}}', encoding="utf-8")
    state = read_reported(config)["G"]
    assert state.floor == "ID7" and state.seen == {"ID7"}


def test_a_legacy_mark_still_means_everything_up_to_it(config):
    """The old format only recorded the LAST reported id. Everything before it
    in the next capture was reported too, and must not come back."""
    state = ChatState(seen={"c"}, floor="c")
    got = unseen(msgs("a", "b", "c", "d"), state)
    assert [m["msg_id"] for m in got.messages] == ["d"]


def test_a_legacy_floor_is_dropped_once_real_ids_are_recorded(config):
    from wa_session.watermarks import watermarks_path
    watermarks_path(config).write_text('{"chats": {"G": "ID7"}}', encoding="utf-8")
    advance(config, [{"chat": "G", "messages": msgs("ID8")}])
    assert read_reported(config)["G"].floor == ""


def test_write_then_read_round_trips(config):
    write_reported(config, {"G": ChatState(seen={"a", "b"})})
    assert read_reported(config)["G"].seen == {"a", "b"}


# --- the exact complaint, as a test ----------------------------------------

def test_a_quiet_group_stays_out_of_the_digest(config):
    """"it mentioned groups which not hold any unread". A group whose every
    visible message has been reported must produce nothing at all."""
    captured = msgs("m1", "m2", "m3")
    advance(config, [{"chat": "G", "messages": captured}])
    assert unseen(captured, read_reported(config)["G"]).messages == []


def test_history_the_user_read_days_ago_is_not_news(config):
    """"it again gave content of earlier (earlier read also) messages". A
    capture reaching back further than the last one must not report what it
    finds down there."""
    advance(config, [{"chat": "G", "messages": msgs("m5", "m6")}])
    deeper = msgs("m1", "m2", "m3", "m4", "m5", "m6", "m7")
    got = unseen(deeper, read_reported(config)["G"])
    assert [m["msg_id"] for m in got.messages] == ["m7"]


def test_genuinely_new_messages_are_never_swallowed(config):
    """"it not queried 2 messages from [a group]". The other half of the same
    rule: what IS new must survive all of the filtering above."""
    advance(config, [{"chat": "G", "messages": msgs("m1", "m2", "m3")}])
    got = unseen(msgs("m1", "m2", "m3", "m4", "m5"), read_reported(config)["G"])
    assert [m["msg_id"] for m in got.messages] == ["m4", "m5"]


def test_a_repeated_digest_of_the_same_capture_says_nothing_the_second_time(config):
    """Two GROUPSUMs in a row with no new messages in between."""
    captured = msgs("m1", "m2")
    first = unseen(captured, read_reported(config).get("G"))
    advance(config, [{"chat": "G", "messages": first.messages}])
    second = unseen(captured, read_reported(config)["G"])
    assert first.messages and second.messages == []
