"""The profile must never live in a folder that uploads it.

`.wa-profile` is a live WhatsApp credential -- anyone holding it can read and
send as you. Cloud sync defeats every other protection here at once: the file
mode is irrelevant once Dropbox has a copy, FileVault protects a disk that is
not the one it ends up on, and rotation does not help because the sync client
uploads each new session too.

It also fails silently. Everything works exactly as normal while a copy of the
session sits on someone else's servers, which is why this refuses rather than
warns.
"""

import pytest

from wa_session.config import (SYNCED_ROOTS, SyncedProfile, assert_not_synced,
                               ensure_private_dir)


def test_an_ordinary_path_is_fine(tmp_path):
    assert_not_synced(tmp_path / "project" / ".wa-profile") is None


@pytest.mark.parametrize("root", SYNCED_ROOTS)
def test_every_known_sync_root_is_refused(tmp_path, root):
    target = tmp_path / root / "project" / ".wa-profile"
    with pytest.raises(SyncedProfile):
        assert_not_synced(target)


def test_icloud_is_refused(tmp_path):
    target = tmp_path / "Library" / "Mobile Documents" / "x" / ".wa-profile"
    with pytest.raises(SyncedProfile, match="Mobile Documents"):
        assert_not_synced(target)


def test_modern_macos_cloudstorage_mounts_are_refused(tmp_path):
    """Google Drive, OneDrive, Dropbox and Box all mount here now."""
    target = tmp_path / "Library" / "CloudStorage" / "GoogleDrive-me" / "p"
    with pytest.raises(SyncedProfile, match="CloudStorage"):
        assert_not_synced(target)


def test_the_message_says_how_to_fix_it(tmp_path):
    with pytest.raises(SyncedProfile) as exc:
        assert_not_synced(tmp_path / "Dropbox" / ".wa-profile")
    assert "WA_PROFILE_DIR" in str(exc.value)


def test_a_lookalike_name_is_not_refused(tmp_path):
    """"Dropbox" as a path SEGMENT, not a substring: a directory called
    `my-Dropbox-notes` is not a sync root, and refusing it would be a bug."""
    assert_not_synced(tmp_path / "my-Dropbox-notes" / ".wa-profile") is None
    assert_not_synced(tmp_path / "OneDriveBackups" / ".wa-profile") is None


def test_the_check_runs_before_the_directory_is_created(tmp_path):
    """Every browser launch goes through `ensure_private_dir`, login included,
    so there is no path that creates a profile without passing this."""
    target = tmp_path / "Dropbox" / ".wa-profile"
    with pytest.raises(SyncedProfile):
        ensure_private_dir(target)
    assert not target.exists(), "it must refuse before creating anything"


def test_an_ordinary_profile_is_still_created(tmp_path):
    target = tmp_path / "work" / ".wa-profile"
    ensure_private_dir(target)
    assert target.is_dir()
    assert oct(target.stat().st_mode)[-3:] == "700"


# --- and the policy reads as a duration, not a number ---------------------

@pytest.mark.parametrize("hours,shown", [
    (336.0, "14 days"),
    (168.0, "7 days"),
    (720.0, "30 days"),
    (24.0, "24h"),
    (36.0, "36h"),
])
def test_the_policy_is_shown_in_days_where_that_reads_better(hours, shown):
    from wa_session.cli import _policy_age
    assert _policy_age(hours) == shown
