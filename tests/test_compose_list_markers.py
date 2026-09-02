"""A leading "-" bullet must not be able to block a message for ever.

WhatsApp's composer turns a line starting "- " into a real list and emits its
own "•" marker, which replaces the line break in the read-back: "…\\n- Tomorrow"
comes back as "…•- Tomorrow". Same length, so it is not truncation -- and the
pre-send check correctly refuses, leaving a perfectly good digest stuck on
every tick. Verified against the live site 2026-09-01.
"""

import pytest

from wa_session.compose import neutralize_list_markers as clean


@pytest.mark.parametrize("marker", ["-", "*", "+"])
def test_leading_list_markers_are_replaced(marker):
    assert clean(f"head\n{marker} item") == "head\n· item"


def test_every_bullet_in_a_body_is_handled():
    assert clean("h\n- one\n- two\n- three") == "h\n· one\n· two\n· three"


def test_an_indented_bullet_keeps_its_indent():
    assert clean("h\n  - item") == "h\n  · item"


def test_a_bullet_on_the_very_first_line_is_handled():
    assert clean("- item") == "· item"


def test_tabs_before_the_marker_are_preserved():
    assert clean("h\n\t- item") == "h\n\t· item"


# --- things that must NOT be touched ---------------------------------------

def test_a_negative_number_is_not_a_bullet():
    assert clean("-5 degrees outside") == "-5 degrees outside"


def test_a_horizontal_rule_is_left_alone():
    """The digest used '---' as a separator; it round-trips fine."""
    assert clean("above\n---\nbelow") == "above\n---\nbelow"


def test_whatsapp_bold_syntax_is_left_alone():
    """'*bold*' has no space after the star, so it is not a list."""
    assert clean("this is *bold* text") == "this is *bold* text"


def test_a_dash_inside_a_line_is_left_alone():
    assert clean("cost is 40 - 50 shekels") == "cost is 40 - 50 shekels"


def test_an_em_dash_bullet_is_already_safe():
    assert clean("h\n– item") == "h\n– item"


def test_the_replacement_is_idempotent():
    once = clean("h\n- item")
    assert clean(once) == once


def test_hebrew_lines_are_untouched():
    text = "צהרון א/ב\n· כבר בסדר"
    assert clean(text) == text


def test_the_exact_failing_digest_shape_is_fixed():
    """The real body that stalled: an RTL heading followed by '- ' bullets."""
    body = ("צהרון א/ב\n"
            "- Tomorrow is the final adjustment day.\n"
            "- Clubs only start 6/9.")
    assert "\n- " not in clean(body)
    assert clean(body).count("· ") == 2
