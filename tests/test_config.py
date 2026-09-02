import pytest

from wa_session.config import (
    DEFAULT_ROTATE_AFTER_HOURS,
    ensure_private_dir,
    load_config,
    project_root,
)


def test_defaults_are_project_local():
    config = load_config(env={})
    root = project_root()
    assert config.profile_dir == root / ".wa-profile"
    assert config.state_dir == root / ".wa-state"
    assert config.rotate_after_hours == DEFAULT_ROTATE_AFTER_HOURS


def test_env_overrides_paths_and_policy(tmp_path):
    config = load_config(
        env={
            "WA_PROFILE_DIR": str(tmp_path / "p"),
            "WA_STATE_DIR": str(tmp_path / "s"),
            "WA_ROTATE_AFTER_HOURS": "1.5",
        }
    )
    assert config.profile_dir == tmp_path / "p"
    assert config.state_dir == tmp_path / "s"
    assert config.rotate_after_hours == 1.5


def test_state_file_lives_under_state_dir(tmp_path):
    config = load_config(env={"WA_STATE_DIR": str(tmp_path)})
    assert config.state_file == tmp_path / "session.json"


def test_tilde_is_expanded():
    config = load_config(env={"WA_PROFILE_DIR": "~/wa"})
    assert "~" not in str(config.profile_dir)
    assert config.profile_dir.is_absolute()


@pytest.mark.parametrize("bad", ["abc", "0", "-4"])
def test_invalid_rotation_hours_rejected(bad):
    with pytest.raises(ValueError):
        load_config(env={"WA_ROTATE_AFTER_HOURS": bad})


def test_blank_rotation_hours_falls_back_to_default():
    assert load_config(env={"WA_ROTATE_AFTER_HOURS": "  "}).rotate_after_hours == 24.0


def test_profile_dir_is_owner_only(tmp_path):
    target = tmp_path / "nested" / "profile"
    ensure_private_dir(target)
    assert target.is_dir()
    assert target.stat().st_mode & 0o777 == 0o700


def test_existing_loose_dir_is_tightened(tmp_path):
    target = tmp_path / "profile"
    target.mkdir(mode=0o755)
    ensure_private_dir(target)
    assert target.stat().st_mode & 0o777 == 0o700
