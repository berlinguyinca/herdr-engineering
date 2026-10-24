# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""Live check: a real Pi-Bolt agent in a real (local) collaboration room over MCP, with a reconnect.

Opt-in: it makes real model calls through the Pi provider config, so it only runs when PIBOLT_LIVE is set and the
evaluation binaries exist. Nothing here is operator configuration; the variable only gates an expensive test.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from tests.control_plane.test_collab import BASE
from tests.control_plane.test_collab_public import H, connect, stack  # noqa: F401

BOLT = Path.home() / ".local/share/pi-bolt-eval/bolt-v1.0.0/std/pi-bolt-linux-x64/pi"
MODELS = Path.home() / ".pi/agent/models.json"
MODEL = "metabolomics/deepseek_v4-flash-modality-text-quant-mxfp4"

pytestmark = pytest.mark.skipif(
    not (os.environ.get("PIBOLT_LIVE") and BOLT.exists() and MODELS.exists()),
    reason="live Pi-Bolt collaboration check is opt-in (PIBOLT_LIVE=1, evaluation binary and provider config present)",
)


def run_agent(agent, port, room_id, text, *, kill_hard=False, timeout=240):
    """Start Pi-Bolt in RPC mode with the collab MCP server configured, ask it to post `text`, wait for the end."""
    seen = []
    d = Path(tempfile.mkdtemp(prefix="pibolt-collab-"))
    try:
        shutil.copy(MODELS, d / "models.json")
        os.chmod(d / "models.json", 0o600)
        (d / "mcp.json").write_text(json.dumps({"mcpServers": {"collab": {
            "url": f"http://127.0.0.1:{port}/mcp",
            "exposure": "direct",
            "headers": {"Authorization": f"Agent {agent['credential']}", "X-Herdr-Device": agent["device_id"], **H},
        }}}))
        env = {**os.environ, "PI_CODING_AGENT_DIR": str(d), "PI_SKIP_VERSION_CHECK": "1"}
        p = subprocess.Popen([str(BOLT), "--mode", "rpc", "--no-session", "--thinking", "off", "--model", MODEL],
                             cwd=d, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        diag = subprocess.run([str(BOLT), "mcp", "list"], cwd=d, env=env, capture_output=True, text=True, timeout=60)
        seen.append("MCPLIST:" + (diag.stdout + diag.stderr)[-500:])
        prompt = (f"Use your mcp__collab__room_post tool to post exactly this text to room {room_id}: {text}\n"
                  "Then reply DONE. If you cannot find the tool, reply NOTOOL.")
        p.stdin.write(json.dumps({"id": "p", "type": "prompt", "message": prompt}) + "\n")
        p.stdin.flush()
        deadline = time.time() + timeout
        ended = False
        for line in p.stdout:
            try:
                o = json.loads(line)
            except ValueError:
                continue
            seen.append(o.get("type"))
            if o.get("type") == "tool_execution_end" and kill_hard:
                p.kill()  # crash right after the tool ran: no clean shutdown, no final message
                break
            if o.get("type") == "agent_end":
                ended = True
                p.stdin.write(json.dumps({"id": "t", "type": "get_last_assistant_text"}) + "\n")
                p.stdin.flush()
                continue
            if o.get("type") == "response" and o.get("command") == "get_last_assistant_text":
                seen.append("TEXT:" + str((o.get("data") or {}).get("text"))[:300])
                break
            if time.time() > deadline:
                break
        p.kill()
        p.wait(timeout=10)
        return ended, seen
    finally:
        shutil.rmtree(d, ignore_errors=True)


def messages(stack, room_id):
    s, out = stack.main.call("GET", f"{BASE}/rooms/{room_id}/events", user="alice", headers=H)
    assert s == 200
    return [e for e in out["events"] if e["event_type"] == "message"]


def test_pibolt_agent_posts_crashes_and_reconnects_without_loss_or_duplicates(stack):
    s, p = stack.main.call("POST", f"{BASE}/projects", {"name": "p"}, user="alice", headers=H)
    s, r = stack.main.call("POST", f"{BASE}/projects/{p['project_id']}/rooms", {"name": "r"}, user="alice", headers=H)
    room = r["room_id"]
    agent = connect(stack.main, "alice")
    s, _ = stack.main.call("POST", f"{BASE}/rooms/{room}/agents", {"agent_id": agent["agent_id"], "actions": ["read", "post"]},
                           user="alice", headers=H)
    assert s == 201
    port = stack.srv.public_port

    # a person's client is watching the room's long-poll and has a cursor
    s, first = stack.main.call("GET", f"{BASE}/rooms/{room}/state?since=-1&wait=0", user="alice", headers=H)
    assert s == 200
    cursor = first["seq"]

    # 1. a Pi-Bolt agent posts through the collab MCP endpoint
    ended, seen = run_agent(agent, port, room, "PIBOLT-A first message")
    texts = [m["text"] for m in messages(stack, room)]
    assert any("PIBOLT-A" in t for t in texts), (ended, [x for x in seen if x and (x.startswith("TEXT") or x.startswith("MCPLIST") or "tool" in x)], texts)

    # 2. the client "disconnects", the agent process crashes right after a tool call, a NEW agent process reconnects and posts again
    run_agent(agent, port, room, "PIBOLT-B after reconnect", kill_hard=True)
    deadline = time.time() + 30
    while time.time() < deadline and not any("PIBOLT-B" in m["text"] for m in messages(stack, room)):
        time.sleep(1)

    # 3. the client reconnects with its OLD cursor and must see what it missed, nothing lost
    s, again = stack.main.call("GET", f"{BASE}/rooms/{room}/state?since={cursor}&wait=0", user="alice", headers=H)
    assert s == 200 and again["changed"] is True
    seen_texts = [e["text"] for e in again["events"] if e["event_type"] == "message"]
    a = [t for t in seen_texts if "PIBOLT-A" in t]
    b = [t for t in seen_texts if "PIBOLT-B" in t]
    assert len(a) == 1 and len(b) == 1, seen_texts
