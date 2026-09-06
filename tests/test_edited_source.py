"""A draft answers ONE message. If that message changes, the draft is void.

WhatsApp lets the sender edit a message after it arrives, and the edit
replaces the text in place -- the recipient's chat shows only the new wording.

On 2026-09-06 that happened inside a single minute: a message was captured at
20:39, drafted against, and edited before the draft was posted at 20:41. The
draft answered "where did we last light a bonfire"; the chat by then read
"when is your next psychiatrist appointment". Nothing checked, so `OK #TMQ`
would have sent a cheerful "don't remember, I'll look and tell you" in reply
to that. From the user's side it looked like the model had invented a
question -- it had not; it answered exactly what was on screen when it read.
"""

import pytest

from wa_session.agent import _source_message_changed


class _M:
    def __init__(self, text):
        self.text = text


@pytest.fixture
def chat(monkeypatch):
    def _set(texts, raises=False):
        def fake(page, minimum):
            if raises:
                raise RuntimeError("pane detached")
            return [_M(t) for t in texts]
        monkeypatch.setattr("wa_session.messages.read_window", fake)
    return _set


def test_an_unchanged_message_sends(chat):
    chat(["hello", "when did we go?"])
    assert _source_message_changed(object(), "when did we go?") == ""


def test_an_edited_message_refuses(chat):
    """The exact failure: the text is simply not there any more."""
    chat(["hello", "when is your next appointment?"])
    reason = _source_message_changed(object(), "where did we last light a fire?")
    assert reason
    assert "edited or deleted" in reason
    assert "where did we last light a fire?" in reason, \
        "say what it was, or the user cannot tell which draft this is about"


def test_a_deleted_message_refuses(chat):
    chat(["hello"])
    assert _source_message_changed(object(), "gone now")


def test_a_draft_with_no_quoted_message_is_not_blocked(chat):
    """Older drafts, and any future path that does not record one."""
    chat(["anything"])
    assert _source_message_changed(object(), "") == ""


def test_a_read_failure_refuses_rather_than_assuming(chat):
    """Uncertainty is not permission. verify_recipient refuses on a conflict;
    this refuses when it cannot check at all."""
    chat([], raises=True)
    reason = _source_message_changed(object(), "something")
    assert "could not re-read" in reason


def test_whitespace_normalisation_matches_the_capture(chat):
    """`extract_messages` collapses runs of whitespace on both sides, so an
    identical message must still compare equal."""
    chat(["one two three"])
    assert _source_message_changed(object(), "one two three") == ""


# --- and the user must be told -------------------------------------------

def test_a_refused_send_tells_the_user(tmp_path, monkeypatch):
    """An approval that produces nothing looks exactly like a dead daemon --
    the failure this project keeps having to hunt down. The pre-send checks
    exist to refuse, so a refusal has to be spoken."""
    from wa_session.approval import Command, Decision
    from wa_session.config import Config
    from wa_session.tick import _handle_pending

    (tmp_path / "p" / ".wa-agent").mkdir(parents=True)
    cfg = Config(profile_dir=tmp_path / "p" / ".wa-profile",
                 state_dir=tmp_path / "p" / ".wa-state", rotate_after_hours=24.0)

    posted = []
    monkeypatch.setattr("wa_session.tick.poll",
                        lambda page, config, draft_id: Command(
                            decision=Decision.APPROVE, draft_id=draft_id))
    monkeypatch.setattr("wa_session.tick.deliver",
                        lambda page, config, draft_id, live=False: {
                            "ok": False,
                            "reason": "the message this answers is no longer "
                                      "in the chat as it was captured"})
    monkeypatch.setattr("wa_session.tick.post_note",
                        lambda page, text, **kw: posted.append(text) or "id")

    result = {"actions": []}
    _handle_pending(object(), cfg, {"draft_id": "#ABC"}, result)

    assert posted, "a refused send must not be silent"
    assert "#ABC was NOT sent" in posted[0]
    assert "no longer in the chat" in posted[0]
    assert "Nothing was delivered" in posted[0]
