"""Concurrency between overlapping ticks.

The browser phase is serialised by one profile lock. The LLM phase runs outside
it on purpose -- a model thinking for minutes must not block every other tick --
which is exactly where a second tick could start a duplicate paid run.
"""

import multiprocessing as mp

import pytest

from wa_session.agent import agent_dir
from wa_session.config import Config
from wa_session.lock import Busy, profile_lock
from wa_session.pipeline import QueueItem, read_queue, write_queue_item


@pytest.fixture
def config(tmp_path) -> Config:
    return Config(profile_dir=tmp_path / "p" / ".wa-profile",
                  state_dir=tmp_path / "p" / ".wa-state", rotate_after_hours=24.0)


def _hold(path, started, release):
    with profile_lock(path):
        started.set()
        release.wait(timeout=10)


def test_a_second_tick_cannot_start_a_duplicate_run(config):
    """Two paid LLM runs racing to write one outbox file is the failure this
    prevents; it is likely, not theoretical, when a run outlasts the interval."""
    lock = agent_dir(config) / "run-sum-123.lock"
    ctx = mp.get_context("spawn")
    started, release = ctx.Event(), ctx.Event()
    proc = ctx.Process(target=_hold, args=(lock, started, release))
    proc.start()
    try:
        assert started.wait(timeout=10)
        with pytest.raises(Busy):
            with profile_lock(lock):
                pass
    finally:
        release.set()
        proc.join(timeout=10)


def test_run_locks_are_per_item_not_global(config):
    """One slow draft must not stall an unrelated one."""
    a = agent_dir(config) / "run-item-a.lock"
    b = agent_dir(config) / "run-item-b.lock"
    with profile_lock(a):
        with profile_lock(b):      # different item: must not block
            pass


def test_dispatch_skips_an_item_whose_outbox_already_exists(config, tmp_path):
    from wa_session.tick import _run_for

    outbox = tmp_path / "done.json"
    outbox.write_text("{}")
    out = _run_for(QueueItem(queue_id="q1", chat="S"), config, outbox)
    assert out["skipped"] == "already drafted"


def test_only_one_digest_is_outstanding_at_a_time(config):
    """Two GROUPSUMs in quick succession must not open every group twice."""
    from wa_session.tick import SUMMARY_PREFIX, _collect_group_messages

    write_queue_item(config, QueueItem(queue_id=f"{SUMMARY_PREFIX}1", chat="__summary__"))
    result = {"actions": []}
    assert _collect_group_messages(None, config, result) is None
    assert any("already in progress" in str(a) for a in result["actions"])
    assert len([i for i in read_queue(config)
                if i.queue_id.startswith(SUMMARY_PREFIX)]) == 1
