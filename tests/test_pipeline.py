"""The capability boundary between the drafter and the daemon.

The drafter has no shell and no WhatsApp access, so it cannot send. These tests
cover the remaining channel it *does* have -- the outbox file -- and prove it
cannot use that to influence WHO a message goes to, or to send at all.
"""

import json

import pytest

from wa_session.config import Config
from wa_session.pipeline import (
    ContractError,
    DraftSubmission,
    MAX_BODY_CHARS,
    QueueItem,
    clear_item,
    outbox_dir,
    parse_submission,
    queue_dir,
    read_queue,
    take_submission,
    write_queue_item,
)


@pytest.fixture
def config(tmp_path) -> Config:
    return Config(
        profile_dir=tmp_path / "proj" / ".wa-profile",
        state_dir=tmp_path / "proj" / ".wa-state",
        rotate_after_hours=24.0,
    )


def ok_payload(**over) -> str:
    data = {"queue_id": "q1", "body": "Привет!", "sources": ["calendar: 0 events"]}
    data.update(over)
    return json.dumps(data, ensure_ascii=False)


# --- the routing boundary --------------------------------------------------

@pytest.mark.parametrize("field", ["chat", "recipient", "to", "send", "live", "draft_id"])
def test_outbox_may_not_steer_routing_or_sending(field):
    """An injected message must not be able to re-aim a draft at another chat.

    Removing the ability to SEND is not enough if the drafter still chooses the
    RECIPIENT -- the daemon would faithfully deliver to whoever it named.
    """
    with pytest.raises(ContractError, match="daemon"):
        parse_submission(ok_payload(**{field: "שכנים בבניין"}), "q1")


def test_submission_carries_no_recipient_at_all():
    sub = parse_submission(ok_payload(), "q1")
    assert isinstance(sub, DraftSubmission)
    assert not hasattr(sub, "chat") and not hasattr(sub, "recipient")


def test_queue_id_mismatch_is_rejected():
    """A drafter must not answer a queue item it was not given."""
    with pytest.raises(ContractError, match="does not match"):
        parse_submission(ok_payload(queue_id="other"), "q1")


# --- malformed input -------------------------------------------------------

@pytest.mark.parametrize(
    ("raw", "match"),
    [
        ("not json", "valid JSON"),
        ('["a"]', "JSON object"),
        ('{"queue_id":"q1","body":""}', "non-empty"),
        ('{"queue_id":"q1","body":"   "}', "non-empty"),
        ('{"queue_id":"q1","body":123}', "non-empty"),
        ('{"queue_id":"q1"}', "non-empty"),
        ('{"queue_id":"q1","body":"hi","sources":"cal"}', "list of strings"),
        ('{"queue_id":"q1","body":"hi","sources":[1]}', "list of strings"),
    ],
)
def test_malformed_outbox_is_rejected_not_repaired(raw, match):
    with pytest.raises(ContractError, match=match):
        parse_submission(raw, "q1")


def test_oversized_body_is_rejected():
    with pytest.raises(ContractError, match="exceeds"):
        parse_submission(ok_payload(body="x" * (MAX_BODY_CHARS + 1)), "q1")


def test_body_is_taken_verbatim_apart_from_surrounding_whitespace():
    sub = parse_submission(ok_payload(body="  שלום\nעולם  "), "q1")
    assert sub.body == "שלום\nעולם"


def test_sources_are_capped():
    sub = parse_submission(ok_payload(sources=[f"s{i}" for i in range(50)]), "q1")
    assert len(sub.sources) == 20


# --- queue round trip ------------------------------------------------------

def test_queue_round_trip_preserves_routing_and_revision(config):
    item = QueueItem(queue_id="q1", chat="Подруга", revision=2,
                     messages=[{"text": "привет"}], edit_instructions="shorter")
    write_queue_item(config, item)
    back = read_queue(config)
    assert len(back) == 1
    assert back[0].chat == "Подруга"
    assert back[0].revision == 2
    assert back[0].edit_instructions == "shorter"


@pytest.mark.parametrize("bad", ["../escape", "a/b", "", "x" * 65, "a b"])
def test_unsafe_queue_ids_are_refused(config, bad):
    with pytest.raises(ContractError, match="unsafe queue id"):
        write_queue_item(config, QueueItem(queue_id=bad, chat="Подруга"))


def test_corrupt_queue_files_are_skipped_not_fatal(config):
    write_queue_item(config, QueueItem(queue_id="good", chat="Подруга"))
    (queue_dir(config) / "bad.json").write_text("{oops")
    assert [i.queue_id for i in read_queue(config)] == ["good"]


def test_take_submission_returns_none_when_drafter_has_not_answered(config):
    assert take_submission(config, "q1") is None


def test_clear_item_removes_both_sides(config):
    write_queue_item(config, QueueItem(queue_id="q1", chat="Подруга"))
    (outbox_dir(config) / "q1.json").write_text(ok_payload())
    clear_item(config, "q1")
    assert not (queue_dir(config) / "q1.json").exists()
    assert not (outbox_dir(config) / "q1.json").exists()


def test_queue_and_outbox_dirs_are_owner_only(config):
    assert queue_dir(config).stat().st_mode & 0o777 == 0o700
    assert outbox_dir(config).stat().st_mode & 0o777 == 0o700


# --- revision cap ----------------------------------------------------------

def test_edit_revisions_are_capped(config, monkeypatch):
    """Edit -> redraft -> edit could otherwise loop forever, each cycle
    spending an LLM run and posting another draft to the self-chat."""
    from wa_session import tick as tick_mod

    monkeypatch.setattr(tick_mod, "retire_draft", lambda *a, **k: None)
    entry = {"draft_id": "#AAA", "recipient": "Подруга", "body": "старый",
             "revision": tick_mod.MAX_REVISIONS}
    out = tick_mod._queue_revision(config, entry, "ещё короче")
    assert "edit_refused" in out
    assert read_queue(config) == []          # nothing queued past the cap


def test_edit_under_the_cap_queues_a_revision(config, monkeypatch):
    from wa_session import tick as tick_mod

    monkeypatch.setattr(tick_mod, "retire_draft", lambda *a, **k: None)
    entry = {"draft_id": "#AAA", "recipient": "Подруга", "body": "старый", "revision": 0}
    out = tick_mod._queue_revision(config, entry, "сделай короче")
    assert out["revision"] == 1
    items = read_queue(config)
    assert len(items) == 1
    assert items[0].chat == "Подруга"                  # routing preserved
    assert items[0].edit_instructions == "сделай короче"
    assert items[0].previous_body == "старый"


def test_queue_ids_are_filesystem_safe_for_any_chat_name(config):
    from wa_session.tick import _queue_id

    for name in ["Подруга", "שכנים בבניין", "../../etc/passwd", "a b/c", "x" * 200]:
        qid = _queue_id(name)
        write_queue_item(config, QueueItem(queue_id=qid, chat=name))  # must not raise
    assert len(read_queue(config)) == 5
