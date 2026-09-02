"""Agent-layer gates. These are the checks that stand between a draft and a
message arriving in someone else's phone.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from wa_session.agent import (
    PendingDraft,
    allowlist_path,
    load_pending,
    save_pending,
)
from wa_session.allowlist import Allowlist
from wa_session.approval import Draft, Journal, Status
from wa_session.config import Config


@pytest.fixture
def config(tmp_path) -> Config:
    return Config(
        profile_dir=tmp_path / "proj" / ".wa-profile",
        state_dir=tmp_path / "proj" / ".wa-state",
        rotate_after_hours=24.0,
    )


def make_draft() -> Draft:
    return Draft(
        draft_id="#T3S",
        recipient="Bob",
        source_chat="Bob",
        body="שלום, אפשר עד 18:00.",
        quoted="שאלה",
        sources=["calendar:1", "gmail:0"],
        created_at=datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc),
    )


def test_pending_draft_round_trips_through_disk(config):
    pending = PendingDraft(draft=make_draft(), marker_id="m42")
    save_pending(config, pending)
    loaded = load_pending(config, "#T3S")
    assert loaded.marker_id == "m42"
    assert loaded.draft.body == pending.draft.body      # RTL text preserved
    assert loaded.draft.sources == ["calendar:1", "gmail:0"]
    assert loaded.draft.created_at == pending.draft.created_at


def test_draft_file_is_owner_only(config):
    from wa_session.agent import draft_path

    save_pending(config, PendingDraft(draft=make_draft(), marker_id="m1"))
    assert draft_path(config, "#T3S").stat().st_mode & 0o777 == 0o600


def test_agent_dir_is_owner_only(config):
    from wa_session.agent import agent_dir

    assert agent_dir(config).stat().st_mode & 0o777 == 0o700


def test_allowlist_lives_under_the_agent_dir_and_starts_empty(config):
    allow = Allowlist(allowlist_path(config))
    assert len(allow) == 0
    assert allow.allows("Bob") is False


def test_journal_and_allowlist_are_independent_gates(config):
    """Approval alone is not enough: the chat must also be allowlisted."""
    allow = Allowlist(allowlist_path(config))
    journal = Journal(config.profile_dir.parent / ".wa-agent" / "journal.jsonl")
    draft = make_draft()
    journal.record_draft(draft)
    # Journalled, but never allowlisted.
    assert allow.allows(draft.recipient) is False


def test_expired_draft_reports_expired(config):
    draft = make_draft()
    assert draft.is_expired(draft.created_at + timedelta(hours=3)) is True
    assert draft.is_expired(draft.created_at + timedelta(minutes=30)) is False


def test_dry_run_must_not_spend_the_approval(config):
    """A rehearsal that consumed the OK would disarm the live send that follows."""
    import inspect

    from wa_session import agent

    source = inspect.getsource(agent.deliver)
    assert "consume=live" in source, "dry-run delivery must not consume the approval"


@pytest.mark.parametrize(
    ("row_title", "target", "matches"),
    [
        ("Дима Я Сам(You)", "Дима Я Сам", True),   # self-chat list rendering
        ("Дима Я Сам (You)", "Дима Я Сам", True),
        ("Дима Я Сам", "Дима Я Сам", True),
        ("שכנים בבניין", "שכנים בבניין", True),
        ("Bob", "Bobby", False),                   # no prefix matching
        ("Bobby", "Bob", False),
        ("Alice(You)", "Bob", False),
        ("", "Bob", False),
        ("Bob", "", False),
    ],
)
def test_row_title_matching_tolerates_only_the_you_suffix(row_title, target, matches):
    from wa_session.export import row_title_matches

    assert row_title_matches(row_title, target) is matches
