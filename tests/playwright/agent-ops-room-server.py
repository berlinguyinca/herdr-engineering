"""Screenshot fixture: the REAL control plane with Agent Operations on AND a real collaboration room the operator can read,
so the map shows a Room cluster (an API key alone reads no rooms; the single operator's own collab identity does).

    python tests/playwright/agent-ops-room-server.py     # prints PORT=<n> once ready
    OFF=1 ...                                            # control_plane.agent_ops.enabled false (disablement check)

Events go through the real HTTP POST /api/v1/ingest with visibility_scope "room" and a room_id; the room, its members and the
operator are real CollabService state. Sources are Pi-, Claude- and Codex-shaped; nothing is stubbed.
"""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO))

from herdr_engineering.control_plane.api import ControlPlaneServer  # noqa: E402
from herdr_engineering.control_plane.collab import CollabService, FileEventLog, StaticTokenAuthenticator, _digest  # noqa: E402
from herdr_engineering.control_plane.repo import ControlPlaneRepo  # noqa: E402

_seq: dict[str, int] = {}
_n = [0]
_BASE = datetime.now(UTC) - timedelta(minutes=14)


def post(base, room, source, event_type, agent, **fields):
    _n[0] += 1
    _seq[source] = _seq.get(source, 0) + 1
    stamp = (_BASE + timedelta(seconds=_n[0] * 20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = {"schema_version": 2, "event_id": f"r-{_n[0]:03d}", "source": source, "source_sequence": _seq[source],
            "entity_type": "lifecycle", "entity_id": agent or "host", "event_type": event_type, "occurred_at": stamp,
            "visibility_scope": "room", "room_id": room, "agent_id": agent, "provenance": "observed", "payload": {}, **fields}
    req = urllib.request.Request(f"{base}/api/v1/ingest", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10).read()


def seed(base, room):
    PI, CL, CX = "pi", "claude-code", "codex"
    # Claude-shaped planner on mac-01: context RED (92%)
    post(base, room, CL, "session.started", "claude", session_id="s-claude", host_id="mac-01", model_id="claude-opus-5", sanitized_label="Claude (Planner)", payload={"provider": "anthropic"})
    post(base, room, CL, "context.metric", "claude", session_id="s-claude", payload={"context_used": 184000, "context_limit": 200000, "context_source": "claude-code"})
    # Pi-shaped backend on dev-02: context ok (45%)
    post(base, room, PI, "session.started", "qwen", session_id="s-qwen", host_id="dev-02", model_id="qwen3-72b", sanitized_label="Qwen (Backend)", payload={"provider": "alibaba"})
    post(base, room, PI, "context.metric", "qwen", session_id="s-qwen", payload={"context_used": 58000, "context_limit": 128000, "context_source": "pi"})
    # Codex-shaped frontend on dev-03: context AMBER (78%)
    post(base, room, CX, "session.started", "codex", session_id="s-codex", host_id="dev-03", model_id="gpt-5.5", sanitized_label="Codex (Frontend)", payload={"provider": "openai"})
    post(base, room, CX, "context.metric", "codex", session_id="s-codex", payload={"context_used": 156000, "context_limit": 200000, "context_source": "codex"})
    # Reviewer on mac-01: NO context measurement -> "ctx unknown"
    post(base, room, CL, "session.started", "opus", session_id="s-opus", host_id="mac-01", model_id="claude-opus-5", sanitized_label="Opus (Reviewer)", payload={"provider": "anthropic"})
    # two more Pi agents used for the interrupted handoffs
    post(base, room, PI, "session.started", "gem", session_id="s-gem", host_id="dev-04", model_id="gemini-3", sanitized_label="Gem (Docs)", payload={"provider": "google"})
    post(base, room, PI, "session.started", "lint", session_id="s-lint", host_id="dev-04", model_id="qwen3-8b", sanitized_label="Lint bot")
    # a person gives an instruction
    post(base, room, CL, "message.sent", "user:alice", task_id="T-428", payload={"target_session_id": "s-claude"})
    post(base, room, CL, "tool.started", "claude", session_id="s-claude", task_id="T-428")
    # ownership + delegation (distinct edges)
    post(base, room, CL, "owner.transferred", "claude", session_id="s-claude", task_id="T-428", payload={"new_owner_agent_id": "qwen"})
    post(base, room, CL, "delegation.proposed", "claude", session_id="s-claude", task_id="T-428", payload={"target_session_id": "s-qwen"})
    post(base, room, CL, "delegation.accepted", "claude", session_id="s-claude", task_id="T-428", payload={"target_session_id": "s-qwen"})
    post(base, room, CL, "delegation.proposed", "claude", session_id="s-claude", task_id="T-429", payload={"target_session_id": "s-codex"})
    post(base, room, PI, "tool.started", "qwen", session_id="s-qwen", task_id="T-428")
    post(base, room, CX, "tool.started", "codex", session_id="s-codex", task_id="T-429")
    post(base, room, CX, "message.sent", "codex", session_id="s-codex", task_id="T-429", payload={"target_session_id": "s-claude"})
    post(base, room, CL, "message.delivered", "claude", session_id="s-claude", task_id="T-429", payload={"target_session_id": "s-codex"})
    post(base, room, PI, "review.requested", "qwen", session_id="s-qwen", task_id="T-428", payload={"target_session_id": "s-opus"})
    post(base, room, PI, "tests.completed", "qwen", session_id="s-qwen", task_id="T-428")
    # model handoff, completed: Codex hands the UI work to Opus, and Opus switches model
    post(base, room, CX, "handoff.state", "codex", session_id="s-codex", handoff_id="ho-model", status="completed", task_id="T-429", payload={"target_session_id": "s-opus"})
    post(base, room, CL, "model.switched", "opus", session_id="s-opus", model_id="claude-sonnet-5")
    # handoff that STALLS (target host connection drops), one that FAILS (target session ends)
    post(base, room, PI, "handoff.state", "qwen", session_id="s-qwen", handoff_id="ho-stall", status="accepted", task_id="T-430", payload={"target_session_id": "s-gem"})
    post(base, room, PI, "agent.disconnected", "gem", session_id="s-gem", host_id="dev-04")
    post(base, room, PI, "handoff.state", "qwen", session_id="s-qwen", handoff_id="ho-fail", status="accepted", task_id="T-431", payload={"target_session_id": "s-lint"})
    post(base, room, PI, "session.ended", "lint", session_id="s-lint")
    # drift: Qwen reports a drift warning; Codex fails tools three times in a row
    post(base, room, PI, "drift.warning", "qwen", session_id="s-qwen", task_id="T-428")
    for _ in range(3):
        post(base, room, CX, "tool.failed", "codex", session_id="s-codex", task_id="T-429")
    # host strip: mac-01 explicitly reconnected (connected), dev-02/dev-03 never reported a link state (unknown), dev-04 disconnected
    post(base, room, CL, "agent.reconnected", None, host_id="mac-01")


def main():
    off = os.environ.get("OFF") == "1"
    tmp = tempfile.TemporaryDirectory()
    token = "tok-alice-0123456789abcdef"
    svc = CollabService(StaticTokenAuthenticator({"alice": _digest(token)}), sink=FileEventLog(str(Path(tmp.name) / "log")))
    alice = svc.authenticate(f"Bearer {token}", None)
    proj = svc.create_project(alice, {"name": "Herdr engineering"})["project_id"]
    room = svc.create_room(alice, proj, {"name": "product-dev"})["room_id"]
    repo = ControlPlaneRepo()
    srv = ControlPlaneServer(repo, host="127.0.0.1", port=0, web_root=_REPO / "packages", collab=svc, operator_user="alice",
                             single_operator=True, agent_ops_config=None if off else {"enabled": True})
    threading.Thread(target=srv.serve, daemon=True).start()
    srv.wait_until_ready()
    base = f"http://127.0.0.1:{srv.port}"
    if not off:
        seed(base, room)
    print(f"PORT={srv.port}", flush=True)
    print(f"ROOM={room}", flush=True)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
