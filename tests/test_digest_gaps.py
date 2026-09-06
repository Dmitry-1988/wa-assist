"""A digest may be incomplete. It may never be incomplete in silence.

Three ways messages used to disappear between a group and a digest, none of
which left a trace in the log or the note:

* the capture was 15 rows deep, so a group that produced more than that
  between digests pushed its own watermark out of view. `since` then treated
  the whole window as new and the middle was never summarised.
* whatever survived that was trimmed to the newest 40, and the watermark was
  then advanced past the ones trimmed away -- so they could never be picked up
  by a later digest either.
* nothing reported any of it. The digest read as a complete account.
"""

import pytest

from wa_session.config import Config
from wa_session.pipeline import QueueItem, read_queue, write_queue_item
from wa_session.tick import (SUMMARY_CAPTURE_DEPTH, SUMMARY_MAX_MESSAGES,
                             _collect_group_messages, _gap_notice)
from wa_session.watermarks import advance, read_watermarks, since, window


def msgs(*ids):
    return [{"msg_id": i, "text": i} for i in ids]


# --- 1. the window itself ------------------------------------------------

def test_a_mark_still_in_view_is_not_a_gap():
    got = window(msgs("a", "b", "c"), "b")
    assert got.messages == msgs("c")
    assert got.gap is False and got.reason == ""


def test_a_first_ever_digest_is_not_a_gap():
    """No mark means nothing has been covered yet, not that something is lost."""
    got = window(msgs("a", "b"), None)
    assert got.messages == msgs("a", "b")
    assert got.gap is False


def test_a_mark_that_scrolled_out_of_view_is_a_gap():
    """The conversation moved past the mark: what sat between is not here."""
    got = window(msgs("x", "y"), "long-gone")
    assert got.messages == msgs("x", "y"), "still return what we do have"
    assert got.gap is True
    assert "no longer in view" in got.reason


def test_everything_covered_is_neither_new_nor_a_gap():
    got = window(msgs("a", "b", "c"), "c")
    assert got.messages == [] and got.gap is False


# --- 2. the cap ----------------------------------------------------------

def test_trimming_to_the_cap_is_reported():
    got = window(msgs(*[f"m{i}" for i in range(10)]), None, cap=4)
    assert [m["msg_id"] for m in got.messages] == ["m6", "m7", "m8", "m9"]
    assert got.gap is True
    assert "6 older message(s) left out" in got.reason


def test_exactly_the_cap_is_not_a_gap():
    got = window(msgs("a", "b", "c"), None, cap=3)
    assert len(got.messages) == 3 and got.gap is False


def test_a_missing_mark_and_an_overflowing_window_both_report():
    got = window(msgs(*[f"m{i}" for i in range(10)]), "gone", cap=4)
    assert len(got.messages) == 4 and got.gap is True


def test_since_still_answers_what_it_always_did():
    """The old helper keeps its contract; it just cannot tell you about gaps."""
    assert since(msgs("a", "b", "c"), "a") == msgs("b", "c")
    assert since(msgs("x"), "long-gone") == msgs("x")
    assert since(msgs("a"), None) == msgs("a")


# --- 3. the tick reports it ----------------------------------------------

@pytest.fixture
def config(tmp_path) -> Config:
    (tmp_path / "p").mkdir()
    cfg = Config(profile_dir=tmp_path / "p" / ".wa-profile",
                 state_dir=tmp_path / "p" / ".wa-state", rotate_after_hours=24.0)
    agent = tmp_path / "p" / ".wa-agent"
    agent.mkdir(parents=True, exist_ok=True)
    (agent / "allowlist.json").write_text(
        '[{"name": "G1", "is_group": true, "mode": "summarize"}]', encoding="utf-8")
    return cfg


class FakePage:
    def __init__(self):
        self.posted: list[str] = []

    def wait_for_timeout(self, ms):
        pass


@pytest.fixture(autouse=True)
def quiet_selfchat(monkeypatch):
    import wa_session.selfchat as selfchat
    monkeypatch.setattr("wa_session.tick.list_unread", lambda page: [])
    monkeypatch.setattr(selfchat, "post",
                        lambda page, text, dry_run=False: page.posted.append(text))


def test_a_summarize_chat_is_read_deeper_than_a_reply(config, monkeypatch):
    """15 rows is enough to answer a question and not enough to hold a mark."""
    seen = {}

    def fake_read(page, name, depth=10):
        seen[name] = depth
        return {"ok": True, "messages": msgs("a")}

    monkeypatch.setattr("wa_session.tick.read_chat", fake_read)
    _collect_group_messages(FakePage(), config, {"actions": []})
    assert seen["G1"] == SUMMARY_CAPTURE_DEPTH > 15


def test_a_watermark_out_of_view_is_logged_and_carried_on_the_item(config,
                                                                   monkeypatch):
    advance(config, [{"chat": "G1", "messages": msgs("old")}])
    monkeypatch.setattr("wa_session.tick.read_chat",
                        lambda page, name, depth=10: {"ok": True,
                                                      "messages": msgs("n1", "n2")})
    result = {"actions": []}
    _collect_group_messages(FakePage(), config, result)

    gap = [a for a in result["actions"] if "groupsum_window_gap" in a]
    assert gap, "the shortfall must reach daemon.log"
    assert gap[0]["groupsum_window_gap"][0]["chat"] == "G1"
    assert read_queue(config)[0].gaps[0]["chat"] == "G1"


def test_a_group_whose_mark_is_in_view_reports_no_gap(config, monkeypatch):
    advance(config, [{"chat": "G1", "messages": msgs("a")}])
    monkeypatch.setattr("wa_session.tick.read_chat",
                        lambda page, name, depth=10: {"ok": True,
                                                      "messages": msgs("a", "b")})
    result = {"actions": []}
    _collect_group_messages(FakePage(), config, result)
    assert not [a for a in result["actions"] if "groupsum_window_gap" in a]
    assert read_queue(config)[0].gaps == []


def test_an_overflowing_backlog_is_capped_and_says_so(config, monkeypatch):
    """The old code kept the newest 40 and advanced the mark past the rest."""
    many = msgs(*[f"m{i}" for i in range(SUMMARY_MAX_MESSAGES + 25)])
    monkeypatch.setattr("wa_session.tick.read_chat",
                        lambda page, name, depth=10: {"ok": True, "messages": many})
    result = {"actions": []}
    _collect_group_messages(FakePage(), config, result)
    queued = read_queue(config)[0]
    assert len(queued.messages[0]["messages"]) == SUMMARY_MAX_MESSAGES
    assert queued.gaps and "left out" in queued.gaps[0]["reason"]


def test_the_gaps_survive_a_round_trip_through_the_queue_file(config, monkeypatch):
    """The item is written before the LLM runs and read back after it."""
    write_queue_item(config, QueueItem(queue_id="sum-9", chat="__summary__",
                                       messages=[],
                                       gaps=[{"chat": "G1", "reason": "why"}]))
    assert read_queue(config)[0].gaps == [{"chat": "G1", "reason": "why"}]


# --- 4. and the user is told, in the digest --------------------------------

def test_a_clean_digest_carries_no_warning():
    assert _gap_notice([]) == ""


def test_the_notice_names_the_chat_and_what_to_do():
    text = _gap_notice([{"chat": "G1", "reason": "the mark is gone"}])
    assert "INCOMPLETE" in text
    assert "G1 — the mark is gone" in text
    assert "GROUPSUM" in text


def test_the_posted_digest_carries_the_warning(config, monkeypatch):
    import wa_session.selfchat as selfchat
    from wa_session.pipeline import DraftSubmission, write_submission
    from wa_session.tick import _post_ready_summaries

    write_queue_item(config, QueueItem(
        queue_id="sum-2", chat="__summary__",
        messages=[{"chat": "G1", "messages": msgs("m1")}],
        gaps=[{"chat": "G1", "reason": "the mark is gone"}]))
    write_submission(config, "sum-2",
                     DraftSubmission(queue_id="sum-2", body="all quiet",
                                     sources=["G1"]))
    page = FakePage()
    monkeypatch.setattr("wa_session.tick.post_note",
                        lambda page_, text, **kw: page.posted.append(text) or "id")

    assert _post_ready_summaries(page, config, {"actions": []}) == 1
    assert "INCOMPLETE" in page.posted[0]
    assert "G1 — the mark is gone" in page.posted[0]


def test_a_gap_does_not_stop_the_watermark_advancing(config, monkeypatch):
    """What WAS covered is covered. Refusing to advance would re-summarise it
    for ever and never recover the part that is already out of reach."""
    from wa_session.pipeline import DraftSubmission, write_submission
    from wa_session.tick import _post_ready_summaries

    write_queue_item(config, QueueItem(
        queue_id="sum-3", chat="__summary__",
        messages=[{"chat": "G1", "messages": msgs("m1", "m2")}],
        gaps=[{"chat": "G1", "reason": "the mark is gone"}]))
    write_submission(config, "sum-3",
                     DraftSubmission(queue_id="sum-3", body="d", sources=[]))
    monkeypatch.setattr("wa_session.tick.post_note", lambda page_, text, **kw: "id")

    _post_ready_summaries(FakePage(), config, {"actions": []})
    assert read_watermarks(config) == {"G1": "m2"}


def test_the_summariser_is_never_handed_the_gap(config):
    """It is the daemon's fact to state. A summariser told 'something is
    missing' would be guessing at what, which is exactly what digests must not
    do -- and it only ever receives `messages`."""
    from wa_session.drafter import build_summary_prompt

    item = QueueItem(queue_id="sum-4", chat="__summary__",
                     messages=[{"chat": "G1", "messages": msgs("m1")}],
                     gaps=[{"chat": "G1", "reason": "the mark is gone"}])
    prompt = build_summary_prompt(item.messages, item.queue_id)
    assert "the mark is gone" not in prompt
    assert "INCOMPLETE" not in prompt


def test_the_warning_survives_the_read_back_fingerprint():
    """`post_note` confirms delivery by fingerprint. A note carrying ⚠️ and
    `·` must still match what WhatsApp renders back, or every digest with a
    gap is declared undelivered and posted again -- the ten-duplicate bug."""
    from wa_session.tick import _fingerprint

    notice = _gap_notice([{"chat": "G1", "reason": "the mark is gone"}])
    posted = f"📋 GROUP DIGEST 13:16\n\nall quiet\n\n· G1{notice}"
    # What inner_text gives back: emoji dropped, punctuation unreliable.
    rendered = posted.replace("📋 ", "").replace("⚠️ ", "").replace("·", "")
    assert _fingerprint(posted) == _fingerprint(rendered)


def test_the_warning_needs_no_list_marker_neutralising():
    """A line opening `- `, `* ` or `+ ` is rewritten by the composer into its
    own bullet, replacing the newline. The notice must not contain one."""
    from wa_session.compose import neutralize_list_markers

    notice = _gap_notice([{"chat": "G1", "reason": "the mark is gone"}])
    assert neutralize_list_markers(notice) == notice
