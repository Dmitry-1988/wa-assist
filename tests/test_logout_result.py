"""Two failures at logout are not equally serious, and the caller must tell
them apart.

Stopping at `menu` or `logout item` means Log out was never pressed, so the
device is certainly still linked. Stopping at `confirmation` means it WAS
pressed and only the QR never appeared -- on 2026-09-21 that sent the user to
check Linked Devices and there was nothing to remove, because the unlink had
worked. A warning that is usually wrong is one people stop reading, and this
one guards the only step in rotation that revokes anything.

No browser here: this is the decision the caller makes from the result.
"""

import pytest

from wa_session.logout import LogoutResult


def test_a_confirmed_unlink_is_truthy():
    """`if log_out(page):` must keep reading naturally."""
    assert LogoutResult(True)


@pytest.mark.parametrize("step", ["page state", "menu", "logout item",
                                  "confirmation", "browser did not start"])
def test_every_failure_is_falsy(step):
    assert not LogoutResult(False, step)


def test_a_confirmed_unlink_counts_as_pressed():
    assert LogoutResult(True).clicked is True


def test_only_the_confirmation_step_counts_as_pressed():
    assert LogoutResult(False, "confirmation").clicked is True


@pytest.mark.parametrize("step", ["page state", "menu", "logout item",
                                  "browser did not start"])
def test_stopping_before_the_press_does_not(step):
    """These mean the device is still linked; the loud warning is correct."""
    assert LogoutResult(False, step).clicked is False


def test_the_exception_path_default_is_safe():
    """`cli` seeds this before the browser opens, and both failure branches
    read `.step` -- a bare False would raise AttributeError there."""
    seeded = LogoutResult(False, "browser did not start")
    assert not seeded and seeded.clicked is False and seeded.step
