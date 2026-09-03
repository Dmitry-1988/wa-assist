"""The group digest is written in English, not in a mix of languages.

The monitored groups are Hebrew; the digest that came back was Russian prose
with Hebrew fragments inside it. A summary is only useful if it reads in one
language, so the prompt names one -- while the REPLY drafter keeps answering in
whatever language the other person wrote in.
"""

from wa_session.config import Config
from wa_session.drafter import build_prompt, build_summary_prompt
from wa_session.pipeline import QueueItem

import pytest

from conftest import write_context


@pytest.fixture
def config(tmp_path) -> Config:
    cfg = Config(profile_dir=tmp_path / "p" / ".wa-profile",
                 state_dir=tmp_path / "p" / ".wa-state", rotate_after_hours=24.0)
    write_context(cfg)
    return cfg


def summary() -> str:
    return build_summary_prompt([{"chat": "G", "messages": []}], "sum-1")


def test_the_digest_is_asked_for_in_english():
    text = summary()
    assert "ENGLISH" in text
    assert "digest text in English" in text


def test_the_old_bilingual_instruction_is_gone():
    """'Russian or English' is what produced the mixed-language digest."""
    assert "Russian or English" not in summary()


def test_mixing_languages_is_forbidden_explicitly():
    assert "do not mix languages" in summary()


def test_hebrew_may_still_be_quoted_where_it_carries_meaning():
    """Translating a name, an address or a link away would lose the point."""
    text = summary()
    assert "Quote the original Hebrew ONLY where" in text
    assert "quotation marks" in text


def test_chat_names_are_left_as_they_are():
    assert "Chat names stay as they are written" in summary()


def test_the_digest_has_a_word_budget():
    """Two messages produced a screen-filling near-verbatim restatement."""
    text = summary()
    assert "under 120 words" in text
    assert "never exceed" in text


def test_restating_a_message_in_full_is_forbidden():
    text = summary()
    assert "never restate a message in full" in text
    assert "one short bullet, not a" in text


def test_bullets_per_chat_are_capped():
    assert "At most 3 bullets per chat" in summary()


def test_noise_is_dropped_not_summarised():
    """"nice idea!" is not worth a line in a digest."""
    assert "Drop pleasantries" in summary()


def test_replies_are_not_given_a_digest_word_budget(config):
    """A reply must be as long as it needs to be; the budget is digest-only."""
    prompt = build_prompt(QueueItem(queue_id="q1", chat="Подруга"), config)
    assert "120 words" not in prompt


def test_replies_still_follow_the_other_person_s_language(config):
    """A digest is for the user; a reply is for the recipient. Forcing English
    here would answer his Russian-speaking wife in English."""
    prompt = build_prompt(QueueItem(queue_id="q1", chat="Подруга"), config)
    assert "in the language the other person used" in prompt
    assert "ENGLISH" not in prompt


# --- report what was said; never derive what it means ----------------------

def test_inferring_a_consequence_is_forbidden():
    """A teacher wrote "Thursday and Friday are my days off"; the digest
    reported "no kindergarten those days", which was false -- a substitute was
    covering, and the parent nearly kept a child home on it."""
    text = summary()
    assert "REPORT, DO NOT INFER" in text
    assert "Never\nstate a consequence nobody wrote" in text


def test_the_real_failure_is_carried_as_the_example():
    """Keeping the actual case in the prompt beats an abstract rule."""
    text = summary()
    assert "days off" in text
    assert "no kindergarten" in text
    assert "a substitute was" in text


def test_the_user_draws_the_conclusion_not_the_digest():
    assert "report the words and stop" in summary()


def test_the_digest_must_not_title_itself():
    """The daemon adds its own header; a second one lands underneath it."""
    text = summary()
    assert "DO NOT TITLE THE DIGEST" in text
    assert "Start straight at the first chat name" in text


# --- and the code strips one anyway ----------------------------------------

@pytest.mark.parametrize("body", [
    "📋 GROUP DIGEST\n\nאקווה\n· x",
    "GROUP DIGEST 06:31\n\nאקווה\n· x",
    "📋 GROUP DIGEST 06:31\nאקווה\n· x",
])
def test_a_self_added_title_is_removed(body):
    from wa_session.tick import _strip_own_title
    assert _strip_own_title(body).startswith("אקווה")


def test_a_body_without_a_title_is_untouched():
    from wa_session.tick import _strip_own_title
    body = "אקווה פמילי\n· Lighting at the back is broken."
    assert _strip_own_title(body) == body


def test_only_the_leading_title_is_stripped():
    """A later mention must survive; this is a header fix, not a censor."""
    from wa_session.tick import _strip_own_title
    body = "📋 GROUP DIGEST\n\nא\n· someone said GROUP DIGEST in the chat"
    assert "someone said GROUP DIGEST in the chat" in _strip_own_title(body)
