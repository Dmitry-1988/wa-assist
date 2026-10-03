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
    STALL_RENOTIFY_ATTEMPTS,
    _count_context_failure,
    _report_stalled_items,
    _why,
)


@pytest.fixture
def config(tmp_path) -> Config:
    (tmp_path / "p").mkdir()
    return Config(profile_dir=tmp_path / "p" / ".wa-profile",
                  state_dir=tmp_path / "p" / ".wa-state", rotate_after_hours=24.0)


class _Msg:
    def __init__(self, text, msg_id):
        self.text, self.msg_id = text, msg_id


class FakePage:
    """Records what would have been posted, and echoes it back on read --
    post_note now confirms delivery instead of trusting the send."""

    def __init__(self, fails=False):
        self.posted: list[str] = []
        self.fails = fails

    def wait_for_timeout(self, ms):
        pass
    def evaluate(self, script, arg=None):
        # The per-message delivery status. A fake that never acknowledges
        # makes every post wait out its timeout and then fail.
        return getattr(self, "delivery", "Read")



@pytest.fixture(autouse=True)
def fake_selfchat(monkeypatch):
    import wa_session.selfchat as selfchat

    def post(page, text, dry_run=False):
        if page.fails:
            raise RuntimeError("composer not found")
        page.posted.append(text)
        return None

    def read(page, limit=60, **kw):
        return [_Msg(t, f"m{i}") for i, t in enumerate(page.posted)]

    monkeypatch.setattr(selfchat, "post", post)
    monkeypatch.setattr(selfchat, "read", read)


@pytest.fixture(autouse=True)
def cli_is_fine(monkeypatch):
    """Keep these tests off the real CLI.

    `_report_stalled_items` now checks whether `claude -p` runs at all before
    it blames anything. That spawns a process, so without this the suite is
    slow and depends on a live subscription.
    """
    monkeypatch.setattr("wa_session.tick.cli_healthy",
                        lambda: (True, "the claude CLI runs"))


def _stalled_action(result: dict) -> dict:
    return next(a for a in result["actions"] if "stalled" in a)


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
    logged = _stalled_action(result)
    assert logged["stalled"] == "q1" and logged["chat"] == "Подруга"
    # Which notice this is, and how long the outage has run, both belong in the
    # log: the previous version recorded neither, so a three-day outage and a
    # twelve-minute one left the same single line.
    assert logged["notice"] == 1 and logged["attempts"] == CONTEXT_STALL_ATTEMPTS


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
                                       stalled_notified=True, stall_notices=2,
                                       last_context_error="workspace-mcp status=pending"))
    item = read_queue(config)[0]
    assert item.attempts == 4 and item.stalled_notified is True
    assert item.stall_notices == 2
    assert item.last_context_error == "workspace-mcp status=pending"


# --- the outage is announced more than once -------------------------------
# 2026-09-30 to 10-03: 1821 refusals of one item, ONE notice, on attempt six.
# The user had no further word for three days while nobody got an answer.

def test_a_long_outage_is_announced_again_later(config):
    write_queue_item(config, QueueItem(queue_id="q1", chat="S",
                                       attempts=CONTEXT_STALL_ATTEMPTS))
    page = FakePage()
    _report_stalled_items(page, config, {"actions": []})
    assert len(page.posted) == 1

    # Still failing, much later: the threshold has moved, so it speaks again.
    item = read_queue(config)[0]
    item.attempts = CONTEXT_STALL_ATTEMPTS + STALL_RENOTIFY_ATTEMPTS
    write_queue_item(config, item)
    _report_stalled_items(page, config, {"actions": []})
    assert len(page.posted) == 2
    assert "STILL cannot" in page.posted[1]


def test_the_repeat_is_not_every_tick_in_between(config):
    """Between repeats it stays quiet, or the self-chat becomes noise."""
    write_queue_item(config, QueueItem(queue_id="q1", chat="S",
                                       attempts=CONTEXT_STALL_ATTEMPTS))
    page = FakePage()
    _report_stalled_items(page, config, {"actions": []})
    for extra in (1, 2, STALL_RENOTIFY_ATTEMPTS - 1):
        item = read_queue(config)[0]
        item.attempts = CONTEXT_STALL_ATTEMPTS + extra
        write_queue_item(config, item)
        _report_stalled_items(page, config, {"actions": []})
    assert len(page.posted) == 1


# --- the notice states the real reason -------------------------------------
# It used to name Google's token expiry whatever had happened. On 2026-10-03
# the cause was a server that had not finished starting, Google was fine, and
# `reauth` would have fixed nothing.

def test_the_notice_quotes_the_actual_failure(config):
    write_queue_item(config, QueueItem(
        queue_id="q1", chat="S", attempts=CONTEXT_STALL_ATTEMPTS,
        last_context_error="workspace-mcp status=pending"))
    page = FakePage()
    _report_stalled_items(page, config, {"actions": []})
    assert "workspace-mcp status=pending" in page.posted[0]


def test_the_failure_reason_is_recorded_for_the_notice(config):
    item = QueueItem(queue_id="q1", chat="S")
    write_queue_item(config, item)
    _count_context_failure(config, item, _why(
        {"ok": False, "context_unavailable": "workspace-mcp status=failed"}))
    assert read_queue(config)[0].last_context_error == "workspace-mcp status=failed"


@pytest.mark.parametrize("outcome, expected", [
    ({"context_unavailable": "workspace-mcp status=pending"},
     "workspace-mcp status=pending"),
    ({"error": "drafter timed out"}, "drafter timed out"),
    # context_unavailable wins: it is the more specific of the two.
    ({"context_unavailable": "no calendars", "error": "x"}, "no calendars"),
    ({"ok": False}, "the drafting run failed without saying why"),
])
def test_why_reports_what_the_run_said(outcome, expected):
    assert _why(outcome) == expected
