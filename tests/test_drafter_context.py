"""A drafting run without mail and calendar must not become a message.

The failure this guards against is not hypothetical. On 2026-09-01 workspace-mcp
failed to connect for a few minutes; the run exited 0 with no tools, and the
daemon published a reply telling the recipient the sender had lost access to his
email. Exit status cannot tell that apart from a good run -- only the handshake
can.
"""

import json
import os
import stat

import pytest

from conftest import write_context

from wa_session.config import Config
from wa_session.drafter import mcp_health, run_drafter
from wa_session.pipeline import QueueItem, outbox_dir


def init_event(status="connected", tools=None, name="workspace-mcp"):
    return {
        "type": "system",
        "subtype": "init",
        "mcp_servers": [{"name": name, "status": status}],
        "tools": ["Read", "Write"] + (
            ["mcp__workspace-mcp__get_events",
             "mcp__workspace-mcp__search_gmail_messages"]
            if tools is None else tools
        ),
    }


# --- the health check itself ----------------------------------------------

def test_connected_server_with_its_tools_is_healthy():
    healthy, detail = mcp_health(init_event())
    assert healthy is True
    assert detail == "connected"


@pytest.mark.parametrize("status", ["failed", "pending", "needs-auth"])
def test_any_status_but_connected_is_unhealthy(status):
    healthy, detail = mcp_health(init_event(status=status))
    assert healthy is False
    assert status in detail


def test_absent_server_is_unhealthy_not_assumed_fine():
    healthy, detail = mcp_health({"type": "system", "subtype": "init",
                                  "mcp_servers": [], "tools": []})
    assert healthy is False
    assert "not configured" in detail


def test_connected_but_without_calendar_is_unhealthy():
    """'Never invent availability' is unenforceable without get_events."""
    healthy, detail = mcp_health(
        init_event(tools=["mcp__workspace-mcp__search_gmail_messages"])
    )
    assert healthy is False
    assert "get_events" in detail


# --- the run ---------------------------------------------------------------

@pytest.fixture
def config(tmp_path) -> Config:
    (tmp_path / "p").mkdir()
    cfg = Config(profile_dir=tmp_path / "p" / ".wa-profile",
                 state_dir=tmp_path / "p" / ".wa-state", rotate_after_hours=24.0)
    write_context(cfg)
    return cfg


ANSWER = {"queue_id": "q1", "body": "готово", "sources": ["календарь"]}


def result_event(answer=None, is_error=False):
    """The run's final message: the drafter now REPLIES with its JSON."""
    return {"type": "result", "is_error": is_error,
            "result": json.dumps(ANSWER if answer is None else answer,
                                 ensure_ascii=False)}


def fake_claude(tmp_path, *lines: dict):
    """A stand-in CLI that emits stream-json and exits 0, like the real one."""
    script = tmp_path / "claude"
    body = "\n".join(f"echo {json.dumps(json.dumps(line))}" for line in lines)
    script.write_text(f"#!/bin/sh\n{body}\nexit 0\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def use(monkeypatch, script):
    from wa_session import drafter
    monkeypatch.setattr(drafter, "claude_binary", lambda: str(script))


def test_unreachable_context_fails_the_run_instead_of_drafting(
    monkeypatch, tmp_path, config
):
    use(monkeypatch, fake_claude(
        tmp_path,
        init_event(status="failed"),
        {"type": "result", "is_error": False, "result": "done"},
    ))
    out = run_drafter(QueueItem(queue_id="q1", chat="Подруга"), config)
    assert out["ok"] is False
    assert "status=failed" in out["context_unavailable"]


def test_a_dead_run_leaves_no_outbox_for_a_later_tick_to_publish(
    monkeypatch, tmp_path, config
):
    stale = outbox_dir(config) / "q1.json"
    stale.write_text(json.dumps({"queue_id": "q1", "body": "guesswork",
                                 "sources": []}))
    use(monkeypatch, fake_claude(tmp_path, init_event(status="failed")))
    run_drafter(QueueItem(queue_id="q1", chat="Подруга"), config)
    assert not stale.exists()


def test_healthy_run_is_reported_ok(monkeypatch, tmp_path, config):
    use(monkeypatch, fake_claude(
        tmp_path,
        init_event(),
        result_event(),
    ))
    out = run_drafter(QueueItem(queue_id="q1", chat="Подруга"), config)
    assert out["ok"] is True
    assert out["mcp"] == "connected"
    assert "context_unavailable" not in out
    # The DAEMON wrote the outbox; the model has no filesystem.
    assert json.loads(
        (outbox_dir(config) / "q1.json").read_text(encoding="utf-8"))["body"] == "готово"


def test_error_result_is_not_ok_even_on_exit_zero(monkeypatch, tmp_path, config):
    use(monkeypatch, fake_claude(
        tmp_path, init_event(), {"type": "result", "is_error": True},
    ))
    assert run_drafter(QueueItem(queue_id="q1", chat="S"), config)["ok"] is False


def test_a_run_that_never_handshakes_is_not_ok(monkeypatch, tmp_path, config):
    """No init line means the CLI never started properly; do not treat exit 0
    as evidence a draft was produced."""
    use(monkeypatch, fake_claude(tmp_path, {"type": "result", "is_error": False}))
    out = run_drafter(QueueItem(queue_id="q1", chat="S"), config)
    assert out["ok"] is False
    assert "never started" in out["error"]


def test_noise_on_stdout_does_not_break_parsing(monkeypatch, tmp_path, config):
    script = tmp_path / "claude"
    script.write_text(
        "#!/bin/sh\n"
        "echo 'not json at all'\n"
        f"echo {json.dumps(json.dumps(init_event()))}\n"
        f"echo {json.dumps(json.dumps(result_event()))}\n"
        "exit 0\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    use(monkeypatch, script)
    assert run_drafter(QueueItem(queue_id="q1", chat="S"), config)["ok"] is True


def test_the_handshake_is_found_even_when_it_is_not_the_first_line(
    monkeypatch, tmp_path, config
):
    """Observed against the real CLI on 2026-09-01: a rate_limit_event can be
    emitted before the init message, so position must not be assumed."""
    use(monkeypatch, fake_claude(
        tmp_path,
        {"type": "rate_limit_event"},
        init_event(status="failed"),
        {"type": "result", "is_error": False},
    ))
    out = run_drafter(QueueItem(queue_id="q1", chat="S"), config)
    assert out["ok"] is False
    assert "status=failed" in out["context_unavailable"]


def test_an_earlier_event_is_not_mistaken_for_the_handshake(
    monkeypatch, tmp_path, config
):
    use(monkeypatch, fake_claude(
        tmp_path,
        {"type": "rate_limit_event"},
        init_event(),
        result_event(),
    ))
    assert run_drafter(QueueItem(queue_id="q1", chat="S"), config)["ok"] is True
