"""A digest must cover what is new, and only what is new.

Before this, every GROUPSUM captured the last N messages in each group
regardless of what a previous digest had already said, so asking twice in an
afternoon restated the whole thing with the one new message buried inside.
"""

import json

import pytest

from wa_session.config import Config
from wa_session.watermarks import (
    advance,
    last_id,
    read_watermarks,
    since,
    watermarks_path,
    write_watermarks,
)


@pytest.fixture
def config(tmp_path) -> Config:
    (tmp_path / "p").mkdir()
    return Config(profile_dir=tmp_path / "p" / ".wa-profile",
                  state_dir=tmp_path / "p" / ".wa-state", rotate_after_hours=24.0)


def msgs(*ids):
    return [{"msg_id": i, "text": f"body {i}"} for i in ids]


# --- picking what is new ---------------------------------------------------

def test_with_no_mark_everything_is_new():
    assert since(msgs("a", "b", "c"), None) == msgs("a", "b", "c")
    assert since(msgs("a", "b"), "") == msgs("a", "b")


def test_only_messages_after_the_mark_are_returned():
    assert since(msgs("a", "b", "c", "d"), "b") == msgs("c", "d")


def test_a_mark_on_the_last_message_leaves_nothing():
    assert since(msgs("a", "b", "c"), "c") == []


def test_a_mark_that_scrolled_out_of_the_window_returns_everything():
    """Returning nothing here would silently drop whatever went by in between."""
    assert since(msgs("x", "y"), "long-gone") == msgs("x", "y")


def test_the_newest_message_is_what_gets_marked():
    assert last_id(msgs("a", "b", "c")) == "c"


def test_a_message_without_an_id_does_not_become_the_mark():
    assert last_id([{"msg_id": "a"}, {"msg_id": ""}]) == "a"


def test_no_ids_at_all_marks_nothing():
    assert last_id([{"text": "no id"}]) == ""


# --- persistence -----------------------------------------------------------

def test_marks_survive_a_round_trip(config):
    write_watermarks(config, {"שכנים בבניין": "ID1"})
    assert read_watermarks(config) == {"שכנים בבניין": "ID1"}


def test_a_missing_file_means_nothing_is_marked(config):
    assert read_watermarks(config) == {}


def test_a_corrupt_file_means_nothing_is_marked(config):
    """Failing towards 'repeat a digest' beats failing towards 'lose one'."""
    path = watermarks_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    assert read_watermarks(config) == {}


def test_advancing_records_the_last_id_per_chat(config):
    advance(config, [{"chat": "A", "messages": msgs("a1", "a2")},
                     {"chat": "B", "messages": msgs("b1")}])
    assert read_watermarks(config) == {"A": "a2", "B": "b1"}


def test_advancing_one_chat_leaves_the_others_alone(config):
    write_watermarks(config, {"A": "old", "B": "keep"})
    advance(config, [{"chat": "A", "messages": msgs("new")}])
    assert read_watermarks(config) == {"A": "new", "B": "keep"}


def test_a_chat_with_no_messages_is_not_marked(config):
    write_watermarks(config, {"A": "old"})
    advance(config, [{"chat": "A", "messages": []}])
    assert read_watermarks(config)["A"] == "old"


def test_the_file_is_owner_only(config):
    write_watermarks(config, {"A": "x"})
    assert watermarks_path(config).stat().st_mode & 0o777 == 0o600


def test_a_second_digest_over_the_same_window_finds_nothing(config):
    """The exact complaint: ask twice, get the same digest back."""
    captured = msgs("m1", "m2", "m3")
    first = since(captured, read_watermarks(config).get("G"))
    assert first == captured
    advance(config, [{"chat": "G", "messages": first}])
    assert since(captured, read_watermarks(config).get("G")) == []


def test_only_the_genuinely_new_message_survives_the_second_pass(config):
    advance(config, [{"chat": "G", "messages": msgs("m1", "m2", "m3")}])
    later = msgs("m1", "m2", "m3", "m4")
    assert since(later, read_watermarks(config).get("G")) == msgs("m4")
