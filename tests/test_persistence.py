"""The core promise: storage written in one run is still there in the next.

Uses a routed fake https origin so localStorage has a real origin to persist
against, without any network access.
"""

import pytest

from wa_session.session import first_page, persistent_context

pytestmark = pytest.mark.browser

ORIGIN = "https://web.whatsapp.test/"


def _open(page):
    page.route(
        f"{ORIGIN}**",
        lambda route: route.fulfill(content_type="text/html", body="<html></html>"),
    )
    page.goto(ORIGIN)


def test_local_storage_survives_a_restart(tmp_path, playwright, launch_kwargs):
    profile = tmp_path / "profile"

    with persistent_context(profile, playwright=playwright, **launch_kwargs) as context:
        page = first_page(context)
        _open(page)
        page.evaluate("localStorage.setItem('wa-token', 'abc123')")

    assert profile.is_dir(), "profile directory should outlive the context"

    with persistent_context(profile, playwright=playwright, **launch_kwargs) as context:
        page = first_page(context)
        _open(page)
        assert page.evaluate("localStorage.getItem('wa-token')") == "abc123"


def test_separate_profiles_do_not_share_storage(tmp_path, playwright, launch_kwargs):
    with persistent_context(tmp_path / "a", playwright=playwright, **launch_kwargs) as context:
        page = first_page(context)
        _open(page)
        page.evaluate("localStorage.setItem('wa-token', 'abc123')")

    with persistent_context(tmp_path / "b", playwright=playwright, **launch_kwargs) as context:
        page = first_page(context)
        _open(page)
        assert page.evaluate("localStorage.getItem('wa-token')") is None


def test_launch_creates_owner_only_profile_dir(tmp_path, playwright, launch_kwargs):
    profile = tmp_path / "profile"
    with persistent_context(profile, playwright=playwright, **launch_kwargs):
        pass
    assert profile.stat().st_mode & 0o777 == 0o700
