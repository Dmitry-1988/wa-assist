"""What the model runs are handed besides the prompt.

Since 2026-11-24 neither run sees the user's auto-memory or CLAUDE.md, and
the drafter's MCP server keeps its files in a folder that lives exactly as
long as the run. The real-CLI half of this is in test_prompt_mentions.py.
"""

import os

from wa_session import drafter
from wa_session.pipeline import QueueItem


def test_every_run_switches_off_mentions_memory_and_claude_md():
    env = drafter._run_env()
    for key in ("CLAUDE_CODE_DISABLE_ATTACHMENTS",
                "CLAUDE_CODE_DISABLE_AUTO_MEMORY",
                "CLAUDE_CODE_DISABLE_CLAUDE_MDS"):
        assert env[key] == "1"


def test_extra_variables_cannot_switch_them_back_on_by_accident():
    env = drafter._run_env({"WORKSPACE_ATTACHMENT_DIR": "/x"})
    assert env["WORKSPACE_ATTACHMENT_DIR"] == "/x"
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"


def test_the_drafters_server_files_live_and_die_with_the_run(config, monkeypatch):
    seen = {}

    def fake_attempt(cmd, summary, item, config, timeout_s, env=None):
        folder = env["WORKSPACE_ATTACHMENT_DIR"]
        seen["dir"] = folder
        seen["existed"] = os.path.isdir(folder)
        open(os.path.join(folder, "message.eml"), "w").write("mail")
        seen["memory_off"] = env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"]
        return {"ok": True}

    monkeypatch.setattr(drafter, "claude_binary", lambda: "claude")
    monkeypatch.setattr(drafter, "build_prompt", lambda *a: "prompt")
    monkeypatch.setattr(drafter, "_attempt_draft", fake_attempt)
    drafter.run_drafter(QueueItem(queue_id="q1", chat="X"), config)
    assert seen["existed"] and seen["memory_off"] == "1"
    assert not os.path.exists(seen["dir"]), "the run's files must not outlive it"


def test_the_folder_goes_even_when_the_run_crashes(config, monkeypatch):
    seen = {}

    def crash(cmd, summary, item, config, timeout_s, env=None):
        seen["dir"] = env["WORKSPACE_ATTACHMENT_DIR"]
        raise RuntimeError("boom")

    monkeypatch.setattr(drafter, "claude_binary", lambda: "claude")
    monkeypatch.setattr(drafter, "build_prompt", lambda *a: "prompt")
    monkeypatch.setattr(drafter, "_attempt_draft", crash)
    try:
        drafter.run_drafter(QueueItem(queue_id="q1", chat="X"), config)
    except RuntimeError:
        pass
    assert not os.path.exists(seen["dir"])
