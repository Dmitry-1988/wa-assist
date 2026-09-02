"""Allowlist modes are a capability boundary, not a label.

A SUMMARIZE chat is readable and digestible but must never become sendable --
that is what makes it safe to monitor a 40-person group.
"""

import pytest

from wa_session.allowlist import REPLY, SUMMARIZE, Allowlist, Entry
from wa_session.approval import is_groupsum
from wa_session.drafter import (
    MCP_TOOLS,
    SUMMARY_DISALLOWED_TOOLS,
    allowed_tools,
    summary_allowed_tools,
)


@pytest.fixture
def allowlist(tmp_path) -> Allowlist:
    return Allowlist(tmp_path / "a.json")


def test_summarize_chat_cannot_be_replied_to(allowlist):
    allowlist.add("שכנים בבניין", is_group=True, mode=SUMMARIZE)
    assert allowlist.allows("שכנים בבניין") is True     # known
    assert allowlist.can_reply("שכנים בבניין") is False  # but not sendable


def test_reply_chat_can(allowlist):
    allowlist.add("Подруга", mode=REPLY)
    assert allowlist.can_reply("Подруга") is True


def test_mode_defaults_to_reply_for_existing_entries(allowlist):
    allowlist.add("Bob")
    assert allowlist.get("Bob").mode == REPLY


def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError):
        Entry(name="x", mode="send-everything")


def test_unrecognised_mode_on_disk_does_not_fall_back_to_reply(tmp_path):
    """A corrupted mode must fail closed, never toward the more capable one."""
    path = tmp_path / "a.json"
    path.write_text('[{"name": "X", "mode": "whatever"}]')
    allow = Allowlist(path)
    assert allow.allows("X") is False
    assert allow.can_reply("X") is False


def test_summarize_chats_are_listed_separately(allowlist):
    allowlist.add("Подруга", mode=REPLY)
    allowlist.add("שכנים בבניין", is_group=True, mode=SUMMARIZE)
    assert [e.name for e in allowlist.summarize_chats()] == ["שכנים בבניין"]


# --- the GROUPSUM trigger --------------------------------------------------

@pytest.mark.parametrize("text", ["GROUPSUM", "groupsum", "GroupSum", " GROUPSUM ", "GROUPSUM."])
def test_groupsum_triggers(text):
    assert is_groupsum(text) is True


@pytest.mark.parametrize(
    "text",
    ["GROUPSUM later maybe", "ok groupsum", "do a groupsum please", "", "GROUP SUM"],
)
def test_groupsum_requires_a_whole_message(text):
    # Opening every group spends read receipts on dozens of people, so a
    # passing mention must not trigger it.
    assert is_groupsum(text) is False


# --- the summarizer's tool set --------------------------------------------

def test_summarizer_has_no_tools_at_all():
    """Group chats are untrusted input. The summariser gets no Gmail, no
    Calendar and no filesystem, so an injected message has nothing to reach.
    It once had Write, which reached src/wa_session/ -- the code the daemon
    imports and runs on its next tick."""
    assert summary_allowed_tools() == []


@pytest.mark.parametrize("tool", ["Read", "Write"])
def test_the_filesystem_is_denied_to_both_runs(tool):
    """Not merely absent from the allowlist -- denied outright."""
    from wa_session.drafter import DISALLOWED_TOOLS
    assert tool in DISALLOWED_TOOLS
    assert tool in SUMMARY_DISALLOWED_TOOLS


def test_summarizer_is_strictly_narrower_than_the_drafter():
    from wa_session.config import Config
    from pathlib import Path as _P
    cfg = Config(profile_dir=_P("/tmp/x/.wa-profile"),
                 state_dir=_P("/tmp/x/.wa-state"), rotate_after_hours=24.0)
    assert set(summary_allowed_tools()).issubset(set(allowed_tools(cfg)))


@pytest.mark.parametrize("tool", ["Bash", "Edit", "Agent", "WebFetch"])
def test_summarizer_escape_hatches_denied(tool):
    assert tool in SUMMARY_DISALLOWED_TOOLS
