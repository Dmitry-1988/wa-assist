"""A reply must read like the user texting, not like a research report.

The draft that prompted this ran to three paragraphs and cited its own
workings: "у меня в календаре так и стоит", "пришло письмо от Pango", plus a
closing paragraph about what was NOT found. All of that is provenance the
recipient never asked for and should not see -- it belongs in "sources", which
only the user sees when approving.

The opposite failure is just as bad: the first attempt at this prompt answered
a two-part question with "3 июня, в среду." and silently dropped the parking
half.
"""

import pytest

from conftest import write_context

from wa_session.config import Config
from wa_session.drafter import build_prompt, build_summary_prompt
from wa_session.pipeline import QueueItem


@pytest.fixture
def config(tmp_path) -> Config:
    cfg = Config(profile_dir=tmp_path / "p" / ".wa-profile",
                 state_dir=tmp_path / "p" / ".wa-state", rotate_after_hours=24.0)
    write_context(cfg)
    return cfg


def reply(config) -> str:
    return build_prompt(QueueItem(queue_id="q1", chat="Подруга"), config)


def test_the_reply_is_written_as_the_user_not_as_an_assistant(config):
    text = reply(config)
    assert "writing AS the user" in text
    assert "not like an assistant reporting findings" in text


def test_length_is_bounded_to_a_sentence_or_two(config):
    assert "one or two sentences" in reply(config)


def test_provenance_is_banned_from_the_message_body(config):
    text = reply(config)
    assert "KEEP YOUR WORKINGS OUT OF THE MESSAGE" in text
    assert "do not name a calendar, an email, a" in text
    assert 'do not say "I checked"' in text


def test_failures_to_find_are_not_narrated_to_the_recipient(config):
    assert "do not\nlist what you failed to find" in reply(config)


def test_sources_are_marked_private(config):
    """The user sees them at approval; the recipient never does."""
    text = reply(config)
    assert "PRIVATE" in text
    assert "never sent" in text


def test_every_part_of_the_question_must_be_answered(config):
    """Brevity must not become dropping half the question."""
    text = reply(config)
    assert "ALL of it" in text
    assert "quietly drops half the question is worse" in text


def test_a_solid_answer_is_not_hedged(config):
    assert "One clean\nfact beats three qualified ones" in reply(config)


# --- the safety rule must survive the brevity push -------------------------

def test_inventing_a_fact_is_still_forbidden(config):
    assert "Never invent a fact or an availability" in reply(config)


def test_not_knowing_is_said_briefly_not_reported(config):
    """'I don't know' must not come back as a survey of what was searched."""
    text = reply(config)
    assert "не помню точно" in text
    assert "rather than a report" in text


def test_all_three_calendars_are_still_required(config):
    text = reply(config)
    assert "Query EVERY calendar" in text
    assert "family0000000000000000000@group.calendar.google.com" in text
    assert "Never use old-account@example.com" in text


def test_the_digest_prompt_is_not_given_the_reply_voice(config):
    """A digest is the user's own notes, not a text message to anyone."""
    digest = build_summary_prompt([{"chat": "G", "messages": []}], "sum-1")
    assert "writing AS the user" not in digest
    assert "ENGLISH" in digest


# --- the drafter may report facts, never create obligations ----------------

def test_commitments_are_forbidden(config):
    """A draft answered "Ага, вечером скину)" and proposed two nights away.
    Neither was ever agreed to. The prompt forbade inventing FACTS and said
    nothing about inventing COMMITMENTS."""
    text = reply(config)
    assert "COMMITMENTS ARE NOT YOURS TO MAKE" in text
    assert "You may say what IS true. You may not decide" in text


def test_promises_plans_and_spending_are_named_explicitly(config):
    text = reply(config)
    for forbidden in ("promise anything on the user's behalf",
                      "accept, decline or propose plans",
                      "bookings, spending, invitations or attendance"):
        assert forbidden in text


def test_a_calendar_does_not_imply_willingness(config):
    """Free time is not consent to fill it."""
    assert "does not tell you what the\nuser is willing to do" in reply(config)


def test_the_decision_is_handed_back_not_taken(config):
    text = reply(config)
    assert "hand\nthe decision back as one short question" in text


def test_answering_is_the_one_promise_still_allowed(config):
    """Over-correcting into refusing "гляну и скажу" would break the
    already-tested honest-ignorance path."""
    text = reply(config)
    assert "The one promise you may make is about answering" in text
    assert "гляну и скажу" in text


def test_the_digest_is_not_given_the_commitment_rule():
    """A digest reports what a group said; it never speaks for the user."""
    assert "COMMITMENTS ARE NOT YOURS" not in build_summary_prompt(
        [{"chat": "G", "messages": []}], "sum-1")
