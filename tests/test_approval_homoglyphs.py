"""An approval typed on a Russian keyboard must be understood.

"OK" on a Russian layout is "ОК" -- U+041E U+041A, the same glyphs as Latin
"OK" and a completely different string. On 2026-09-01 an approval that looked
exactly right was classified ambiguous and the message sat unsent, with no way
for the user to see why: the two spellings are indistinguishable on screen.

The caveat rule still comes first. Accepting a look-alike must not weaken
"OK <id> but shorter" -- that is still not an approval.
"""

import pytest

from wa_session.approval import Decision, latinize, parse_command

ID = "#47Q"
CYRILLIC_OK = "ОК"          # ОК
LATIN_OK = "OK"


def decide(text):
    return parse_command(text, ID).decision


def test_the_two_spellings_are_genuinely_different_strings():
    """If this ever fails the bug never existed and the fix is pointless."""
    assert CYRILLIC_OK != LATIN_OK
    assert latinize(CYRILLIC_OK) == LATIN_OK


def test_a_cyrillic_ok_approves():
    assert decide(f"{CYRILLIC_OK} {ID}") is Decision.APPROVE


def test_a_latin_ok_still_approves():
    assert decide(f"{LATIN_OK} {ID}") is Decision.APPROVE


@pytest.mark.parametrize("mixed", ["ОK", "OК"])
def test_a_half_cyrillic_ok_approves(mixed):
    """Layout switched mid-word; the user cannot see the difference anyway."""
    assert decide(f"{mixed} {ID}") is Decision.APPROVE


def test_lowercase_cyrillic_approves():
    assert decide(f"ок {ID}") is Decision.APPROVE


# --- the safety rule must survive ------------------------------------------

def test_a_cyrillic_ok_with_a_caveat_is_still_ambiguous():
    """The whole point of the approval rule: a caveat is not consent."""
    assert decide(f"{CYRILLIC_OK} {ID} but shorter") is Decision.AMBIGUOUS


def test_a_cyrillic_ok_for_a_different_draft_is_not_consent():
    assert parse_command(f"{CYRILLIC_OK} #ZZZ", ID).decision is Decision.NONE


def test_silence_is_still_not_consent():
    assert decide("") is Decision.NONE
    assert decide("what about the parking bit?") is Decision.NONE


# --- edits keep their Russian text -----------------------------------------

def test_edit_instructions_are_not_latinised():
    """Instructions are sliced from the original, never the normalised copy --
    otherwise the drafter would be handed mangled pseudo-Latin."""
    command = parse_command(f"EDIT {ID}: сделай короче", ID)
    assert command.decision is Decision.EDIT
    assert command.instructions == "сделай короче"


def test_edit_written_with_a_cyrillic_keyword_still_parses():
    command = parse_command(f"ЕDIT {ID}: короче", ID)
    assert command.decision is Decision.EDIT
    assert command.instructions == "короче"


def test_latinize_preserves_length_so_offsets_stay_valid():
    text = "EDIT #47Q: сделай короче и убери про парковку"
    assert len(latinize(text)) == len(text)


def test_a_cyrillic_reject_word_is_not_guessed_at():
    """'НЕТ' is a Russian word, not a look-alike for 'NO'. Refusing to
    guess leaves the draft pending, which is the safe direction."""
    assert decide(f"НЕТ {ID}") is Decision.AMBIGUOUS
