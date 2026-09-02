import pytest

from wa_session.digest import LocalDigest, Summarizer, render
from wa_session.unread import UnreadChat, parse_unread_count, sort_chats, total_unread, truncate


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("3 unread messages", 3),
        ("1 unread message", 1),
        ("12 unread", 12),
        ("5", 5),
        ("99+", 99),
        (" 7 ", 7),
        ("", 0),
        (None, 0),
        ("Chat list", 0),          # unrelated aria-label
        ("no unread messages", 0), # no digit: must not guess
    ],
)
def test_parse_unread_count(raw, expected):
    assert parse_unread_count(raw) == expected


def test_negative_count_rejected():
    with pytest.raises(ValueError):
        UnreadChat(name="x", unread_count=-1)


@pytest.mark.parametrize(
    ("text", "limit", "expected"),
    [
        ("short", 20, "short"),
        ("  collapses   whitespace  ", 40, "collapses whitespace"),
        ("a" * 30, 10, "a" * 9 + "…"),
        ("", 10, ""),
    ],
)
def test_truncate(text, limit, expected):
    assert truncate(text, limit) == expected


def test_sort_is_busiest_first_then_stable():
    chats = [
        UnreadChat("bravo", 1),
        UnreadChat("alpha", 5),
        UnreadChat("Charlie", 5),
    ]
    assert [c.name for c in sort_chats(chats)] == ["alpha", "Charlie", "bravo"]


def test_total_unread():
    assert total_unread([UnreadChat("a", 2), UnreadChat("b", 3)]) == 5


def test_render_empty():
    assert render([]) == "No chats with unread messages."


def test_render_pluralisation_and_totals():
    out = render([UnreadChat("solo", 1, "8:14", "hi")])
    assert "1 chat with unread messages (1 total)" in out
    assert "1 unread · 8:14" in out
    assert "unreads" not in out


def test_render_handles_rtl_and_cyrillic_names():
    out = render([UnreadChat("גן ילדים", 5, "8:21", "דף קשר"), UnreadChat("Подруга", 1, "7:52", "Photo")])
    assert "גן ילדים" in out and "Подруга" in out
    assert out.index("גן ילדים") < out.index("Подруга")  # busiest first


def test_missing_preview_is_stated_not_blank():
    out = render([UnreadChat("quiet", 3)])
    assert "(no preview available)" in out


def test_local_digest_satisfies_the_protocol():
    assert isinstance(LocalDigest(), Summarizer)


def test_custom_summarizer_is_used():
    class Shouty:
        name = "shouty"

        def summarize(self, chat):
            return chat.preview.upper()

    out = render([UnreadChat("a", 1, "9:00", "quiet words")], summarizer=Shouty())
    assert "QUIET WORDS" in out


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # textContent has no newline, so the name runs straight into the badge
        # text. An unbounded pattern used to swallow the whole string.
        ("12 unread messagesשכנים בבניין", "שכנים בבניין"),
        ("5 unread messageצהרון שונית", "צהרון שונית"),
        ("1 unread message Bob", "Bob"),
        ("3 unread messagesПодруга", "Подруга"),
        ("no badge here", "no badge here"),
        ("12 unread messages", ""),
        ("Unread", "Unread"),          # the filter tab label must survive
        ("  spaced   out  ", "spaced out"),
    ],
)
def test_clean_strips_badge_text_without_eating_the_name(raw, expected):
    from wa_session.unread import _clean

    assert _clean(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("[17:05, 8/27/2026] Dmitrymel: ", ("17:05", "8/27/2026", "Dmitrymel")),
        ("[8:14, 28/08/2026] ~Rama Atias: ", ("8:14", "28/08/2026", "~Rama Atias")),
        ("[9:01, 1/2/2026] גן ילדים: ", ("9:01", "1/2/2026", "גן ילדים")),
        (None, ("", "", "")),
        ("not a header", ("", "", "")),
        ("", ("", "", "")),
    ],
)
def test_parse_pre_plain(raw, expected):
    from wa_session.messages import parse_pre_plain

    assert parse_pre_plain(raw) == expected


def test_read_requires_explicit_yes():
    from wa_session.cli import read_main

    # Without --yes it must refuse and never launch a browser.
    assert read_main([]) == 2


def test_export_run_dir_and_parent_are_owner_only(tmp_path, monkeypatch):
    from wa_session import export
    from wa_session.config import Config

    cfg = Config(
        profile_dir=tmp_path / "proj" / ".wa-profile",
        state_dir=tmp_path / "proj" / ".wa-state",
        rotate_after_hours=24.0,
    )
    monkeypatch.setattr(export, "load_config", lambda: cfg, raising=False)
    run_dir = export.new_run_dir(cfg)
    assert run_dir.stat().st_mode & 0o777 == 0o700
    assert export.export_root(cfg).stat().st_mode & 0o777 == 0o700
