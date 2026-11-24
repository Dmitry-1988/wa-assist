"""The drafter must not inherit the user's own MCP servers or allow rules.

`dontAsk` refuses a tool that nothing pre-approved, but an allow rule in the
user's settings counts as pre-approval. Measured 2026-11-24: a user-scope
server's tool was offered to the drafter and RAN. `--setting-sources local`
loads only the scope where workspace-mcp is registered.

Real CLI, throwaway HOME, stub MCP servers and a fake API that asks for the
tool. Run after updating Claude Code, with test_prompt_mentions.py.
"""

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from wa_session import drafter

CLAUDE = drafter.claude_binary()
pytestmark = pytest.mark.skipif(CLAUDE is None, reason="claude CLI not installed")

SERVER = '''
import json, sys
name, flag = sys.argv[1], sys.argv[2]
for line in sys.stdin:
    q = json.loads(line); i = q.get("id")
    if i is None: continue
    m = q.get("method")
    if m == "initialize":
        r = {"protocolVersion": q["params"]["protocolVersion"],
             "capabilities": {"tools": {}}, "serverInfo": {"name": name, "version": "1"}}
    elif m == "tools/list":
        r = {"tools": [{"name": "poke", "description": "x",
                        "inputSchema": {"type": "object", "properties": {}}}]}
    elif m == "tools/call":
        open(flag, "w").write("ran"); r = {"content": [{"type": "text", "text": "done"}]}
    else:
        r = {}
    print(json.dumps({"jsonrpc": "2.0", "id": i, "result": r}), flush=True)
'''


def _events(first, tool):
    block = ({"type": "tool_use", "id": "toolu_1", "name": tool, "input": {}}
             if first else {"type": "text", "text": ""})
    delta = ({"type": "input_json_delta", "partial_json": "{}"} if first
             else {"type": "text_delta", "text": "done"})
    return [
        {"type": "message_start", "message": {
            "id": "m", "type": "message", "role": "assistant", "model": "test",
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1}}},
        {"type": "content_block_start", "index": 0, "content_block": block},
        {"type": "content_block_delta", "index": 0, "delta": delta},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {
            "stop_reason": "tool_use" if first else "end_turn",
            "stop_sequence": None}, "usage": {"output_tokens": 1}},
        {"type": "message_stop"},
    ]


def _run(tmp_path, extra: list[str], ask_for: str):
    """Spawn the CLI with the drafter's flags plus `extra`.

    Returns (tools offered to the model, servers whose tool actually ran).
    """
    home, project = tmp_path / "home", tmp_path / "project"
    (home / ".claude").mkdir(parents=True)
    project.mkdir()
    project = project.resolve()
    (home / ".claude" / "settings.json").write_text(json.dumps(
        {"permissions": {"allow": ["mcp__other", "mcp__loc"]}}))
    script = tmp_path / "server.py"
    script.write_text(SERVER)

    def server(name):
        return {"command": sys.executable,
                "args": [str(script), name, str(tmp_path / f"ran_{name}")]}
    (home / ".claude.json").write_text(json.dumps({
        "mcpServers": {"other": server("other")},
        "projects": {str(project): {"mcpServers": {"loc": server("loc")}}}}))

    offered: list[list[str]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_HEAD(self):
            self.send_response(200)
            self.end_headers()

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
            offered.append(sorted(t["name"] for t in body.get("tools", [])))
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            for event in _events(len(offered) == 1, ask_for):
                self.wfile.write(
                    f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())

    api = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=api.serve_forever, daemon=True).start()
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CLAUDE", "ANTHROPIC")) and not k.lower().endswith("_proxy")}
    env.update(HOME=str(home), ANTHROPIC_API_KEY="dummy-local",
               ANTHROPIC_BASE_URL=f"http://127.0.0.1:{api.server_port}",
               CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", DISABLE_AUTOUPDATER="1",
               CLAUDE_CODE_DISABLE_ATTACHMENTS="1")
    try:
        subprocess.run(
            [CLAUDE, "-p", "hi", "--output-format", "json", "--max-turns", "3",
             "--tools", "", "--settings", drafter.DRAFTER_SETTINGS, *extra,
             "--permission-mode", "dontAsk", "--allowedTools", "mcp__loc__poke"],
            cwd=project, env=env, stdin=subprocess.DEVNULL,
            capture_output=True, timeout=90)
    finally:
        api.shutdown()
        api.server_close()
    assert offered, "the CLI never reached the fake API"
    ran = {n for n in ("other", "loc") if (tmp_path / f"ran_{n}").exists()}
    return offered[0], ran


LOCAL = ["--setting-sources", drafter.DRAFTER_SETTING_SOURCES]


def test_a_user_server_is_not_even_offered(tmp_path):
    offered, ran = _run(tmp_path, LOCAL, "mcp__other__poke")
    assert offered == ["mcp__loc__poke"]
    assert ran == set()


def test_the_local_registration_still_works(tmp_path):
    """workspace-mcp is registered in the local scope; drafting needs it."""
    offered, ran = _run(tmp_path, LOCAL, "mcp__loc__poke")
    assert ran == {"loc"}


def test_the_control_does_detect_the_leak(tmp_path):
    """Without the flag the same setup leaks. If this fails, the test above
    proves nothing any more: the CLI changed how it loads settings."""
    offered, ran = _run(tmp_path, [], "mcp__other__poke")
    assert "mcp__other__poke" in offered and ran == {"other"}
