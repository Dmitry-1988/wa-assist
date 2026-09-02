from wa_session.cli import build_parser


def test_no_flags_is_a_plain_run():
    args = build_parser().parse_args([])
    assert not args.reset
    assert not args.status


def test_reset_flag():
    assert build_parser().parse_args(["--reset"]).reset is True


def test_status_flag():
    assert build_parser().parse_args(["--status"]).status is True


def test_has_profile_detects_empty_and_missing(config):
    from wa_session.cli import _has_profile

    assert _has_profile(config) is False           # missing
    config.profile_dir.mkdir(parents=True)
    assert _has_profile(config) is False           # present but empty
    (config.profile_dir / "Cookies").write_text("x")
    assert _has_profile(config) is True
