"""Boot the real control plane (agent_ops enabled), seed events through the real ingest path, and capture the
terminal map (ANSI + plain + HTML) at 120x40 and 80x24. Run: python capture.py <outdir>  (then shoot.js for PNG)."""
import hashlib
import html
import pathlib
import re
import sys
import threading

from herdr_engineering import agent_ops_tui as tui
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.collab import CollabService, Principal, StaticTokenAuthenticator
from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.repo import ControlPlaneRepo

OP = {"kind": "operator", "id": "key:op", "authenticated_by": "api_key"}
SEED = [  # (agent, event_type, extra fields)
    ("claude", "session.started", {"session_id": "claude", "host_id": "mac-01"}),
    ("claude", "model.selected", {"session_id": "claude", "model_id": "opus-4.1"}),
    ("claude", "context.metric", {"session_id": "claude", "payload": {"context_used": 82, "context_limit": 100, "context_source": "pi"}}),
    ("claude", "tool.started", {"session_id": "claude"}),
    ("qwen", "session.started", {"session_id": "qwen", "host_id": "dev-02"}),
    ("qwen", "model.selected", {"session_id": "qwen", "model_id": "qwen3-72b"}),
    ("qwen", "context.metric", {"session_id": "qwen", "payload": {"context_used": 68, "context_limit": 100}}),
    ("qwen", "tool.started", {"session_id": "qwen"}),
    ("codex", "session.started", {"session_id": "codex", "host_id": "dev-03"}),
    ("codex", "model.selected", {"session_id": "codex", "model_id": "gpt-4o"}),
    ("codex", "tool.started", {"session_id": "codex"}),
    ("opus", "session.started", {"session_id": "opus", "host_id": "mac-02"}),
    ("opus", "tool.failed", {"session_id": "opus"}),
    ("claude", "owner.transferred", {"session_id": "claude", "task_id": "api-impl", "payload": {"new_owner_agent_id": "claude"}}),
    ("claude", "delegation.accepted", {"session_id": "claude", "task_id": "api-impl", "payload": {"target_agent_id": "qwen"}}),
    ("claude", "delegation.proposed", {"session_id": "claude", "task_id": "ui", "payload": {"target_agent_id": "codex"}}),
    ("qwen", "review.requested", {"session_id": "qwen", "task_id": "api-impl", "payload": {"target_agent_id": "opus"}}),
    ("codex", "message.sent", {"session_id": "codex", "task_id": "ui", "payload": {"target_agent_id": "claude"}}),
    ("codex", "artifact.created", {"session_id": "codex", "task_id": "ui"}),
]


def boot():
    repo = ControlPlaneRepo()
    auth = StaticTokenAuthenticator({"alice": hashlib.sha256(b"tok-alice-0123456789").hexdigest()})
    collab = CollabService(auth)
    srv = ControlPlaneServer(repo, host="127.0.0.1", port=0, collab=collab,
                             api_keys={"view-key": "viewer"}, agent_ops_config={"enabled": True})
    threading.Thread(target=srv.serve, daemon=True).start()
    srv.wait_until_ready()
    alice = Principal("user", "alice", "alice")   # a real room: its members make the hub header
    room = collab.create_room(alice, collab.create_project(alice, {"name": "ops"})["project_id"], {"name": "war-room"})["room_id"]
    for n, (agent, etype, extra) in enumerate(SEED, 1):
        IngestService(repo).ingest({"schema_version": 2, "event_id": f"cap-{n}", "source": "pi", "source_sequence": n,
                                    "entity_type": "lifecycle", "entity_id": agent, "event_type": etype,
                                    "occurred_at": f"2026-11-12T12:43:{n:02d}Z",
                                    "visibility_scope": "room" if etype == "session.started" else "org",
                                    "room_id": room if etype == "session.started" else None,
                                    "agent_id": agent, "provenance": "observed", "payload": {}, **extra}, OP)
    return srv


PAL = {203: "#ff5f5f", 78: "#5fd787", 215: "#ffaf5f", 75: "#5fafff", 141: "#af87ff", 80: "#5fd7d7", 244: "#808080", 252: "#d0d0d0"}


def to_html(text):
    out, style = [], []
    for part in re.split(r"(\x1b\[[0-9;]*m)", text):
        m = re.fullmatch(r"\x1b\[([0-9;]*)m", part)
        if m:
            codes = m.group(1).split(";")
            if codes in ([""], ["0"]):
                style = []
            else:
                if codes[0] == "38":
                    style.append(f"color:{PAL.get(int(codes[2]), '#d0d0d0')}")
                elif codes[0] == "1":
                    style.append("font-weight:bold")
                elif codes[0] == "2":
                    style.append("opacity:.6")
            continue
        if part:
            out.append(f'<span style="{";".join(style)}">{html.escape(part)}</span>')
    return "".join(out)


def main(outdir):
    out = pathlib.Path(outdir)
    srv = boot()
    client = tui.Client(f"http://127.0.0.1:{srv.port}", "view-key", "tok-alice-0123456789")
    snap = client.get("snapshot")
    opts = tui.RenderOpts(color=True, unicode=True, reduced_motion=False)
    for w, h in ((120, 40), (80, 24)):
        for tick in (3,):
            lines = tui.render(snap, w, h, "activity", 0, tick, opts)
            ansi = "\n".join(lines)
            (out / f"terminal-{w}x{h}.ansi").write_text(ansi + "\n")
            (out / f"terminal-{w}x{h}.txt").write_text("\n".join(tui._ANSI.sub("", l) for l in lines) + "\n")
            (out / f"terminal-{w}x{h}.html").write_text(
                '<!doctype html><meta charset=utf-8><body style="margin:0;background:#0b0f1a">'
                '<pre id=t style="margin:0;padding:12px;display:inline-block;background:#0b0f1a;color:#d0d0d0;'
                'font:14px/17px Menlo,Monaco,monospace">' + to_html(ansi) + "</pre>")
    # replay frame at the first accepted delegation, to show scrubbing
    cursors = client.all_cursors()
    early = client.get("replay", until=cursors[8])
    ro = tui.RenderOpts(color=True, status="replay", replay_pos=(8, len(cursors)), replay_paused=True)
    lines = tui.render(early, 120, 40, "replay", 0, 0, ro)
    (out / "terminal-120x40-replay.txt").write_text("\n".join(tui._ANSI.sub("", l) for l in lines) + "\n")
    (out / "terminal-120x40-replay.html").write_text(
        '<!doctype html><meta charset=utf-8><body style="margin:0;background:#0b0f1a"><pre id=t style="margin:0;padding:12px;'
        'display:inline-block;background:#0b0f1a;color:#d0d0d0;font:14px/17px Menlo,Monaco,monospace">' + to_html("\n".join(lines)) + "</pre>")
    srv.shutdown()


if __name__ == "__main__":
    main(sys.argv[1])
