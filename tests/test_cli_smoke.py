"""Every subcommand must at least get off the ground.

`wa-agent list` -- the first command the setup instructions tell a new user to
run -- died with UnboundLocalError for a day, because a local import shadowed
a module-level one inside `main()`. Nothing exercised the command itself, so
nothing noticed. These run the argument parsing and the offline branches; the
browser ones are covered elsewhere.
"""

import json

import pytest

from wa_session import agent_cli


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    (root / ".wa-agent").mkdir(parents=True)
    (root / ".wa-agent" / "allowlist.json").write_text(
        '[{"name": "Ann", "is_group": false, "mode": "reply"}]', encoding="utf-8")
    monkeypatch.setenv("WA_PROFILE_DIR", str(root / ".wa-profile"))
    monkeypatch.setenv("WA_STATE_DIR", str(root / ".wa-state"))
    return root


def run(argv, capsys):
    code = agent_cli.main(argv)
    return code, capsys.readouterr().out


def test_list_works_on_a_fresh_checkout(project, capsys):
    code, out = run(["list"], capsys)
    assert code == 0
    assert json.loads(out)["allowlist"][0]["name"] == "Ann"


def test_list_works_with_no_allowlist_at_all(tmp_path, monkeypatch, capsys):
    """A brand-new install has no allowlist file yet."""
    monkeypatch.setenv("WA_PROFILE_DIR", str(tmp_path / "p" / ".wa-profile"))
    monkeypatch.setenv("WA_STATE_DIR", str(tmp_path / "p" / ".wa-state"))
    code, out = run(["list"], capsys)
    assert code == 0
    assert json.loads(out)["allowlist"] == []


def test_pending_works_with_nothing_pending(project, capsys):
    code, out = run(["pending"], capsys)
    assert code == 0 and json.loads(out) == []


def test_allow_then_list_round_trips(project, capsys):
    assert run(["allow", "Bob", "--mode", "reply"], capsys)[0] == 0
    names = [e["name"] for e in json.loads(run(["list"], capsys)[1])["allowlist"]]
    assert "Bob" in names


def test_deny_removes_it(project, capsys):
    run(["allow", "Bob", "--mode", "reply"], capsys)
    run(["deny", "Bob"], capsys)
    names = [e["name"] for e in json.loads(run(["list"], capsys)[1])["allowlist"]]
    assert "Bob" not in names


def test_read_refuses_without_consent(project, capsys):
    """Opening a chat spends a read receipt, so it needs --yes."""
    code, out = run(["read", "--chat", "Ann"], capsys)
    assert code == 2
    assert "read receipts" in json.loads(out)["reason"]


@pytest.mark.parametrize("cmd", ["list", "pending", "drop", "read", "tick",
                                 "unread", "chats", "propose", "poll", "send",
                                 "digest-catchup", "allow", "deny"])
def test_every_subcommand_is_registered(cmd, capsys):
    """A subcommand that stops parsing is invisible until someone types it.
    argparse exits 0 for a known command's --help, 2 for an unknown one."""
    with pytest.raises(SystemExit) as exit_:
        agent_cli.main([cmd, "--help"])
    assert exit_.value.code == 0, f"{cmd!r} is not a registered subcommand"
