import json

import pytest

from wa_session.allowlist import Allowlist, Entry, filter_allowed
from wa_session.unread import UnreadChat


@pytest.fixture
def allowlist(tmp_path) -> Allowlist:
    return Allowlist(tmp_path / "allowlist.json")


def test_starts_empty_and_blocks_everything(allowlist):
    assert len(allowlist) == 0
    assert allowlist.allows("שכנים בבניין") is False


def test_add_then_allow(allowlist):
    allowlist.add("שכנים בבניין", is_group=True, note="building")
    assert allowlist.allows("שכנים בבניין") is True
    assert allowlist.get("שכנים בבניין").is_group is True


def test_persists_across_instances(tmp_path):
    path = tmp_path / "a.json"
    Allowlist(path).add("Bob")
    assert Allowlist(path).allows("Bob") is True


@pytest.mark.parametrize(
    "probe",
    ["אקווה", "שכנים בבניין ", " שכנים בבניין", "ACME", "acme corp", "ACME Corp x"],
)
def test_match_is_exact_only(allowlist, probe):
    # A contact can rename themselves; fuzzy matching would let them opt in.
    allowlist.add("ACME Corp")
    allowlist.add("שכנים בבניין")
    if probe in ("ACME Corp", "שכנים בבניין"):
        return
    assert allowlist.allows(probe) is False


def test_remove(allowlist):
    allowlist.add("Bob")
    assert allowlist.remove("Bob") is True
    assert allowlist.allows("Bob") is False
    assert allowlist.remove("Bob") is False


def test_corrupt_file_blocks_rather_than_widens(tmp_path):
    path = tmp_path / "a.json"
    path.write_text("{not json")
    allowlist = Allowlist(path)
    assert len(allowlist) == 0
    assert allowlist.allows("anything") is False


def test_file_is_owner_only(allowlist):
    allowlist.add("Bob")
    assert allowlist.path.stat().st_mode & 0o777 == 0o600


def test_filter_allowed_splits(allowlist):
    allowlist.add("Bob")
    chats = [UnreadChat("Bob", 2), UnreadChat("שכנים בבניין", 13)]
    allowed, skipped = filter_allowed(chats, allowlist)
    assert [c.name for c in allowed] == ["Bob"]
    assert [c.name for c in skipped] == ["שכנים בבניין"]


def test_group_audience_is_spelled_out():
    assert "everyone" in Entry("g", is_group=True).audience()
    assert Entry("p").audience() == "1:1"
