"""A message that could not be drafted must stay queued -- and stay visible.

The incoming chat has already been opened by the time drafting is attempted, so
its read receipt is spent. Dropping the item there would leave the sender
looking at a message marked read that is never answered.
"""

import pytest

from wa_session.config import Config
from wa_session.pipeline import QueueItem, read_queue, write_queue_item
from wa_session.tick import (
    CONTEXT_STALL_ATTEMPTS,
    _count_context_failure,
    _report_stalled_items,
)


@pytest.fixture
def config(tmp_path) -> Config:
    (tmp_path / "p").mkdir()
    return Config(profile_dir=tmp_path / "p" / ".wa-profile",
                  state_dir=tmp_path / "p" / ".wa-state", rotate_after_hours=24.0)


class FakePage:
    """Records what would have been posted to the self-chat."""

    def __init__(self, fails=False):
        self.posted: list[str] = []
        self.fails = fails


@pytest.fixture(autouse=True)
def fake_selfchat(monkeypatch):
    import wa_session.selfchat as selfchat

    def post(page, text, dry_run=False):
        if page.fails:
            raise RuntimeError("composer not found")
        page.posted.append(text)
        return None

    monkeypatch.setattr(selfchat, "post", post)


def test_a_failed_attempt_keeps_the_item_queued(config):
    item = QueueItem(queue_id="q1", chat="Подруга")
    write_queue_item(config, item)
    _count_context_failure(config, item)
    queued = read_queue(config)
    assert [i.queue_id for i in queued] == ["q1"]
    assert queued[0].attempts == 1


def test_attempts_accumulate_across_ticks(config):
    write_queue_item(config, QueueItem(queue_id="q1", chat="S"))
    for _ in range(3):
        _count_context_failure(config, read_queue(config)[0])
    assert read_queue(config)[0].attempts == 3


def test_nothing_is_said_while_the_outage_is_still_brief(config):
    write_queue_item(config, QueueItem(queue_id="q1", chat="S",
                                       attempts=CONTEXT_STALL_ATTEMPTS - 1))
    page = FakePage()
    _report_stalled_items(page, config, {"actions": []})
    assert page.posted == []


def test_a_long_outage_is_reported_in_the_self_chat(config):
    write_queue_item(config, QueueItem(queue_id="q1", chat="Подруга",
                                       attempts=CONTEXT_STALL_ATTEMPTS))
    page = FakePage()
    result = {"actions": []}
    _report_stalled_items(page, config, result)
    assert len(page.posted) == 1
    note = page.posted[0]
    assert "Подруга" in note
    assert "Nothing has been sent" in note
    assert {"stalled": "q1", "chat": "Подруга"} in result["actions"]


def test_the_stall_is_reported_once_not_every_tick(config):
    write_queue_item(config, QueueItem(queue_id="q1", chat="S",
                                       attempts=CONTEXT_STALL_ATTEMPTS))
    page = FakePage()
    for _ in range(4):
        _report_stalled_items(page, config, {"actions": []})
    assert len(page.posted) == 1


def test_a_failed_notice_is_retried_rather_than_marked_done(config):
    write_queue_item(config, QueueItem(queue_id="q1", chat="S",
                                       attempts=CONTEXT_STALL_ATTEMPTS))
    result = {"actions": []}
    _report_stalled_items(FakePage(fails=True), config, result)
    assert read_queue(config)[0].stalled_notified is False
    assert any("stall_notice_failed" in a for a in result["actions"])


def test_group_digests_are_not_reported_as_stalled_replies(config):
    """A summary has no recipient waiting on it; it is not the same failure."""
    write_queue_item(config, QueueItem(queue_id="sum-1", chat="__summary__",
                                       attempts=CONTEXT_STALL_ATTEMPTS * 2))
    page = FakePage()
    _report_stalled_items(page, config, {"actions": []})
    assert page.posted == []


def test_retry_bookkeeping_survives_a_round_trip_through_disk(config):
    write_queue_item(config, QueueItem(queue_id="q1", chat="S", attempts=4,
                                       stalled_notified=True))
    item = read_queue(config)[0]
    assert item.attempts == 4 and item.stalled_notified is True
