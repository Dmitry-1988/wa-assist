"""Two ways a draft can be built on nothing at all.

Both happened on 2026-09-08, in the same draft.

FIRST: it answered the user's OWN message. The newest thing in the chat was a
photo with no caption, `extract_messages` drops rows with no text -- correctly,
that is how date separators and encryption notices are excluded -- so the last
message carrying text was the user's own reply from the night before, and
`quoted` takes the last message without ever asking who sent it. The draft went
out as `Re: "Да бери дв7"`, quoting him to himself.

SECOND: every tool call failed. `mcp_health` gates on the init handshake, and
the handshake had passed: workspace-mcp reported "connected". Its Google OAuth
was dead, so every calendar and Gmail call answered "Google Authentication
Needed". Six earlier ticks had refused correctly on status=pending; the server
then reconnected and the seventh drafted a reply having checked nothing --
saying so, in its own sources, in the message the user was invited to approve.
"""

import pytest

from wa_session.drafter import tool_failure
from wa_session.tick import _nothing_to_answer


def msg(sender, text="hi"):
    return {"sender": sender, "text": text, "msg_id": "m1"}


# --- who sent it ----------------------------------------------------------

def test_a_message_from_them_is_answerable():
    assert _nothing_to_answer([msg("Ann")], "Ann") == ""


def test_our_own_message_is_not_answered():
    """The regression, in one line."""
    why = _nothing_to_answer([msg("Ann"), msg("Dmitrymel", "Да бери дв7")], "Ann")
    assert why and "your own" in why


def test_a_photo_leaves_our_message_last_and_is_still_refused():
    """The actual sequence: they send an image, it has no text, so the newest
    text in the chat is ours from hours earlier."""
    captured = [msg("Ann", "which one?"), msg("Dmitrymel", "take both")]
    assert _nothing_to_answer(captured, "Ann")


def test_a_chat_with_no_text_at_all_is_refused():
    assert _nothing_to_answer([], "Ann")


def test_an_unattributable_message_is_refused():
    """Not knowing who wrote it is not a reason to answer it."""
    assert _nothing_to_answer([msg("", "???")], "Ann")


def test_the_reason_names_the_sender_for_the_log():
    why = _nothing_to_answer([msg("Dmitrymel")], "Ann")
    assert "Dmitrymel" in why


# --- and whether the tools actually worked --------------------------------

def result_event(text, kind="tool_result"):
    return {"type": "user",
            "message": {"content": [{"type": kind, "content": text}]}}


def test_a_google_auth_failure_is_caught():
    assert tool_failure(result_event("Google Authentication Needed"))


@pytest.mark.parametrize("text", [
    "Google Authentication Needed",
    "invalid_grant: Token has been expired or revoked",
    "credentials are missing",
    "The user is not authenticated",
    "Please reauthenticate the account",
])
def test_every_shape_of_auth_failure_is_caught(text):
    assert tool_failure(result_event(text))


def test_a_working_tool_call_is_not_a_failure():
    assert tool_failure(result_event('{"events": [{"summary": "Dentist"}]}')) == ""


def test_a_nested_content_block_is_read():
    """stream-json wraps tool output in {type: text, text: ...} blocks."""
    event = {"type": "user", "message": {"content": [
        {"type": "tool_result",
         "content": [{"type": "text", "text": "Google Authentication Needed"}]}]}}
    assert tool_failure(event)


def test_a_plain_string_content_is_read():
    event = {"type": "user", "message": {"content": "not authenticated"}}
    assert tool_failure(event)


@pytest.mark.parametrize("event", [
    {"type": "assistant", "message": {"content": "Authentication Needed"}},
    {"type": "result", "result": "not authenticated"},
    {"type": "system", "subtype": "init"},
    {},
])
def test_only_tool_results_count(event):
    """The model discussing an auth error, or a final message mentioning one,
    is not evidence that a call failed."""
    assert tool_failure(event) == ""


def test_an_ordinary_tool_error_is_not_an_auth_failure():
    """A 404 is one bad call; an auth failure means every call will fail."""
    assert tool_failure(result_event("404: calendar not found")) == ""
