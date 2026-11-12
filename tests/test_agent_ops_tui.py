"""Terminal Agent Operations map: pure render, key state machine, and the real control-plane server end to end."""
import hashlib
import http.client
import json
import threading

import pytest

from herdr_engineering import agent_ops_tui as tui
from herdr_engineering.agent_ops_tui import RenderOpts, UIState, handle_key, parse_keys, render, visible_width

UNI = RenderOpts(unicode=True)
ASCII = RenderOpts(unicode=False)


def agent(node, *, model="opus", state="working", ctx=None, label=None, rooms=()):
    return {"node_id": node, "agent_id": node, "session_id": None, "host_id": "h1", "provider": None, "model": model,
            "model_changes": [], "state": state, "context": ctx, "room_ids": list(rooms), "parent_session_id": None,
            "compactions": 0, "drift_warnings": 0, "label": label, "provenance": "observed",
            "last_event_at": "2026-11-12T10:00:04.000000Z"}


def edge(kind, a, b, state="accepted", task="t1"):
    return {"kind": kind, "from": a, "to": b, "state": state, "room_id": None, "event_id": "e", "provenance": "observed",
            "task_id": task, "handoff_id": None, "updated_at": "2026-11-12T10:00:04.000000Z"}


def snap(agents=None, edges=None):
    agents = [agent("claude", ctx={"used": 68, "limit": 100, "source": "pi"}), agent("qwen", model=None),
              agent("codex", state="idle"), agent("opus", state="blocked", ctx={"used": 5, "limit": None, "source": None})] \
        if agents is None else agents
    edges = [edge("delegation", "claude", "qwen"), edge("handoff", "codex", "opus", "planned", "t2"),
             edge("review", "qwen", "opus"), edge("communication", "claude", "codex")] if edges is None else edges
    return {"agents": agents, "rooms": [], "edges": edges,
            "tasks": [{"task_id": "t1", "room_id": None, "accountable_owner": "claude", "contributors": ["qwen"],
                       "reviewer": "opus", "updated_at": "2026-11-12T10:00:04.000000Z"}],
            "cursor": "c", "counts": {"agents": len(agents), "rooms": 0, "edges": len(edges), "tasks": 1, "events": 9},
            "truncated": False}


def plain(lines):
    return "\n".join(tui._ANSI.sub("", l) for l in lines)


def map_area(lines):
    """Plain map rows only: no pane frame (panes are not focused) and no legend row."""
    return "\n".join(l for l in plain(lines).splitlines() if "planned" not in l)




@pytest.mark.parametrize("w,h", [(120, 40), (80, 24), (50, 20), (30, 8)])
def test_render_fits_exactly_and_keeps_content(w, h):
    for opts in (UNI, ASCII, RenderOpts(color=True)):
        lines = render(snap(), w, h, opts=opts)
        assert len(lines) == h
        assert all(visible_width(l) <= w for l in lines), [l for l in lines if visible_width(l) > w]
    text = plain(render(snap(), w, h, opts=UNI))
    assert "claude" in text and "ctx" in text


def test_full_layout_has_all_panels():
    text = plain(render(snap(), 120, 40, opts=UNI))
    for needle in ("HERDR / AGENT OPERATIONS", "LIVE", "Tasks", "Recent activity", "Timeline", "t1 owner:claude",
                   "by:qwen rev:opus", "[██████", "68%", "model ?", "planned", "q quit"):
        assert needle in text, needle


def test_compact_and_narrow_layouts():
    assert "Tasks" in plain(render(snap(), 80, 24, opts=UNI)) and "claude" in plain(render(snap(), 80, 24, opts=UNI))
    narrow = plain(render(snap(), 50, 20, opts=UNI))
    assert "AGENT OPS" in narrow and ">" in narrow and "claude" in narrow and "Tasks" not in narrow


def test_unknown_context_is_never_guessed():
    text = plain(render(snap([agent("a", ctx=None), agent("b", ctx={"used": 5, "limit": None, "source": None})], []), 120, 40, opts=UNI))
    assert "ctx ?" in text and "ctx 5" in text and "%" not in text.split("Recent")[0].replace("68%", "")
    assert tui.context_bar(None, 12, UNI) == "ctx ?"
    assert tui.context_bar({"used": 50, "limit": 100}, 14, UNI).endswith("] 50%")


def test_planned_and_accepted_edges_use_different_glyphs():
    focus_tasks = RenderOpts(pane="none")
    solid = map_area(render(snap([agent("a"), agent("b")], [edge("delegation", "a", "b", "accepted")]), 120, 40, opts=focus_tasks))
    dashed = map_area(render(snap([agent("a"), agent("b")], [edge("delegation", "a", "b", "planned")]), 120, 40, opts=focus_tasks))
    assert "━" in solid and "╍" not in solid and "╍" in dashed and "━" not in dashed
    kinds = map_area(render(snap(), 120, 40, opts=focus_tasks))
    for glyph in ("━", "╌", "▶", "⇒", "◇"):
        assert glyph in kinds


def test_ascii_fallback_has_no_unicode():
    text = "\n".join(render(snap(), 120, 40, opts=ASCII))
    assert all(ord(c) < 128 for c in text), {c for c in text if ord(c) >= 128}
    assert "[###" in text or "[##" in text


def test_control_characters_are_stripped_from_labels():
    evil = agent("x", label="bad\x1b[31m\x07name\x00\x9b", model="m\x1b]0;pwn\x07")
    out = "\n".join(render(snap([evil], []), 120, 40, opts=ASCII))
    assert "\x1b" not in out and "\x07" not in out and "\x00" not in out and "\x9b" not in out
    assert "bad[31mname" in out  # only the ESC itself is removed; no sequence survives as a control
    assert tui.clean("a\x1b[1mb\r\nc") == "a[1mbc"


def test_color_off_means_no_escape_codes_and_on_means_codes():
    assert "\x1b" not in "".join(render(snap(), 120, 40, opts=RenderOpts(color=False)))
    assert "\x1b[" in "".join(render(snap(), 120, 40, opts=RenderOpts(color=True)))


def test_detect_opts_honours_no_color_term_and_reduced_motion():
    env = {"TERM": "xterm-256color", "LANG": "en_US.UTF-8"}
    assert tui.detect_opts(env).color and tui.detect_opts(env).unicode and not tui.detect_opts(env).reduced_motion
    assert not tui.detect_opts({**env, "NO_COLOR": "1"}).color
    assert not tui.detect_opts({"TERM": "dumb", "LANG": "en_US.UTF-8"}).unicode
    assert not tui.detect_opts({"TERM": "xterm", "LANG": "C"}).unicode
    assert not tui.detect_opts(env, ascii_only=True).unicode
    assert tui.detect_opts({**env, "HERDR_REDUCED_MOTION": "1"}).reduced_motion
    assert tui.detect_opts(env, no_animation=True).reduced_motion


def test_pulses_move_when_animated_and_stay_when_reduced():
    s = snap([agent("a"), agent("b")], [edge("delegation", "a", "b")])
    moving = RenderOpts(reduced_motion=False)
    frames = {"".join(render(s, 120, 40, tick=t, opts=moving)) for t in range(6)}
    assert len(frames) > 1
    still = {"".join(render(s, 120, 40, tick=t, opts=RenderOpts(reduced_motion=True))) for t in range(6)}
    assert len(still) == 1 and "◉" in next(iter(still))


def test_modes_filter_edges():
    s = snap()
    comm = map_area(render(s, 120, 40, "communication", opts=RenderOpts(pane="none")))
    assert "━" not in comm and "═" not in comm and "─" in comm
    assert "[Replay]" not in plain(render(s, 120, 40, "bogus", opts=UNI))
    assert "filter:review" in plain(render(s, 120, 40, opts=RenderOpts(filter_kind="review")))
    assert "owns t1" in plain(render(s, 120, 40, "ownership", opts=UNI))


def test_overlays_render_inside_the_frame():
    for overlay, needle in (("help", "KEYS"), ("handoffs", "HANDOFFS"), ("timeline", "TIMELINE"), ("filter", "FILTER"),
                            ("inspect", "INSPECT claude")):
        lines = render(snap(), 80, 24, opts=RenderOpts(overlay=overlay))
        assert needle in plain(lines) and len(lines) == 24 and all(visible_width(l) <= 80 for l in lines)


def test_status_line_shows_degraded_offline_and_replay():
    assert "DEGRADED (reconnecting)" in plain(render(snap(), 120, 40, opts=RenderOpts(status="degraded", status_detail="reconnecting")))
    assert "OFFLINE" in plain(render({}, 80, 24, opts=RenderOpts(status="offline")))
    text = plain(render(snap(), 120, 40, "replay", opts=RenderOpts(status="replay", replay_pos=(2, 9), replay_paused=True)))
    assert "REPLAY" in text and "replay 3/9 paused" in text and "▼" in text


def test_empty_and_garbage_snapshots_do_not_crash():
    assert "no agents" in plain(render({}, 120, 40))
    lines = render({"agents": [{"node_id": "x"}], "edges": [{"kind": "delegation", "from": "x", "to": "zz"}]}, 120, 40)
    assert len(lines) == 40
    lines = render({"agents": "nope"}, 80, 24)   # a bad frame shows a message instead of raising
    assert "render error" in plain(lines)


def test_many_agents_grid_stays_in_bounds():
    agents = [agent(f"n{i}") for i in range(14)]
    for focus in (0, 7, 13):
        lines = render(snap(agents, []), 120, 40, focus=focus, opts=UNI)
        assert len(lines) == 40 and all(visible_width(l) <= 120 for l in lines)


# --------------------------------------------------------------------------------------- state machine
def test_navigation_and_panes():
    s = UIState()
    assert handle_key(s, "right", 4).focus == 1 and handle_key(s, "left", 4).focus == 3
    s = handle_key(s, "tab", 4)
    assert s.pane == "tasks"
    assert handle_key(s, "down", 4).scroll == 1 and handle_key(s, "down", 4).focus == 0
    for _ in range(3):
        s = handle_key(s, "tab", 4)
    assert s.pane == "map"


def test_overlay_keys_toggle_and_escape_closes():
    s = UIState()
    for key, name in (("?", "help"), ("h", "handoffs"), ("t", "timeline"), ("enter", "inspect")):
        on = handle_key(s, key, 2)
        assert on.overlay == name and handle_key(on, key, 2).overlay is None and handle_key(on, "esc", 2).overlay is None


def test_filter_and_mode_cycle():
    s = UIState()
    seen = []
    for _ in range(6):
        s = handle_key(s, "f", 2)
        seen.append(s.filter_kind)
    assert seen == list(tui.KINDS) + [None]
    s = UIState()
    modes = []
    for _ in range(5):
        s = handle_key(s, "m", 2)
        modes.append(s.mode)
    assert modes == ["communication", "handoffs", "ownership", "activity", "communication"]


def test_quit_and_replay_flow():
    assert handle_key(UIState(), "q", 1).effects == ("quit",)
    assert handle_key(UIState(), "ctrl-c", 1).effects == ("quit",)
    assert handle_key(UIState(), "r", 1, 0).replay_index is None   # nothing to replay
    s = handle_key(UIState(), "r", 1, 10)
    assert s.mode == "replay" and s.replay_index == 9 and s.effects == (("replay", 9),)
    s = handle_key(s, "[", 1, 10)
    assert s.replay_index == 8 and s.effects == (("replay", 8),)
    assert handle_key(s, "{", 1, 10).replay_index == 0 and handle_key(s, "}", 1, 10).replay_index == 9
    assert handle_key(s, ",", 1, 10).replay_index == 0
    assert not handle_key(s, " ", 1, 10).replay_paused
    live = handle_key(s, "l", 1, 10)
    assert live.replay_index is None and live.mode == "activity" and live.effects == ("live",)
    assert handle_key(UIState(), "[", 1, 10).replay_index is None     # scrub keys only act inside replay


def test_parse_keys():
    assert parse_keys(b"\x1b[A\x1b[B\x1b[C\x1b[D\t\rq") == ["up", "down", "right", "left", "tab", "enter", "q"]
    assert parse_keys(b"\x1b") == ["esc"] and parse_keys(b"\x03") == ["ctrl-c"] and parse_keys(b"\x1b[5~[") == ["["]


# ------------------------------------------------------------------------------- real control plane
OPERATOR = {"kind": "operator", "id": "key:op", "authenticated_by": "api_key"}


def ingest(repo, n, event_type, **fields):
    """Real ingest path, the same envelope tests/control_plane/test_agent_ops.py uses."""
    from herdr_engineering.control_plane.ingest import IngestService
    body = {"schema_version": 2, "event_id": f"in-{n}", "source": "pi", "source_sequence": n,
            "entity_type": "lifecycle", "entity_id": "a1", "event_type": event_type,
            "occurred_at": f"2026-11-12T10:00:{n:02d}Z", "visibility_scope": "org",
            "agent_id": "a1", "provenance": "observed", "payload": {}, **fields}
    IngestService(repo).ingest(body, OPERATOR)


class LiveServer:
    def __init__(self):
        from herdr_engineering.control_plane.api import ControlPlaneServer
        from herdr_engineering.control_plane.collab import CollabService, StaticTokenAuthenticator
        from herdr_engineering.control_plane.repo import ControlPlaneRepo
        self.repo = ControlPlaneRepo()
        auth = StaticTokenAuthenticator({"alice": hashlib.sha256(b"tok-alice-0123456789").hexdigest()})
        self.srv = ControlPlaneServer(self.repo, host="127.0.0.1", port=0, collab=CollabService(auth),
                                      api_keys={"op-key": "operator", "view-key": "viewer"},
                                      agent_ops_config={"enabled": True})
        self.srv.agent_ops_poll_seconds = 0.1
        self.thread = threading.Thread(target=self.srv.serve, daemon=True)
        self.thread.start()
        self.srv.wait_until_ready()
        self.url = f"http://127.0.0.1:{self.srv.port}"

    def stop(self):
        self.srv.shutdown()
        self.thread.join(timeout=5)


def test_end_to_end_renders_the_real_server_snapshot_and_replay():
    server = LiveServer()
    try:
        client = tui.Client(server.url, "view-key")
        # seed through the real ingest path used by test_agent_ops
        ingest(server.repo, 1, "session.started", agent_id="a1", session_id="s1", host_id="h1")
        ingest(server.repo, 2, "model.selected", agent_id="a1", session_id="s1", model_id="opus")
        ingest(server.repo, 3, "delegation.proposed", agent_id="a1", session_id="s1", task_id="t1",
               payload={"target_agent_id": "a2"})
        ingest(server.repo, 4, "delegation.accepted", agent_id="a1", session_id="s1", task_id="t1",
               payload={"target_agent_id": "a2"})
        snapshot = client.get("snapshot")
        assert snapshot["counts"]["agents"] == 2
        for w, h in ((120, 40), (80, 24), (50, 20)):
            text = plain(render(snapshot, w, h, opts=UNI))
            assert "opus" in text and "model ?" in text and "ctx ?" in text, (w, h)
        cursors = client.all_cursors()
        assert len(cursors) >= 4 and cursors == sorted(cursors)
        early = client.get("replay", until=cursors[1])
        assert early["counts"]["edges"] == 0 and snapshot["counts"]["edges"] == 1
        assert plain(render(early, 120, 40, "replay", opts=RenderOpts(status="replay", replay_pos=(1, len(cursors))))).count("REPLAY") >= 1
    finally:
        server.stop()


def test_client_reports_offline_and_stream_resumes_with_backoff():
    with pytest.raises(OSError):
        tui.Client("http://127.0.0.1:1", "k", timeout=0.5).get("snapshot")
    server = LiveServer()
    try:
        ingest(server.repo, 1, "session.started", agent_id="a1", session_id="s1")
        stop, seen, states = threading.Event(), [], []
        got = threading.Event()
        client = tui.Client(server.url, "view-key")
        t = threading.Thread(target=client.stream, args=("", stop, lambda c: (seen.append(c), got.set()), lambda *a: states.append(a[0])),
                             daemon=True)
        t.start()
        assert got.wait(5) and states[0] == "live"
        stop.set()
        t.join(timeout=5)
        bad = tui.Client("http://127.0.0.1:1", "k", timeout=0.5)
        stop2, states2 = threading.Event(), []
        t2 = threading.Thread(target=bad.stream, args=("", stop2, lambda c: None, lambda *a: states2.append(a[0])), daemon=True)
        t2.start()
        deadline = threading.Event()
        deadline.wait(0.5)
        stop2.set()
        t2.join(timeout=5)
        assert states2 and set(states2) == {"degraded"}
    finally:
        server.stop()


def test_opening_an_overlay_resets_scroll():
    s = handle_key(handle_key(UIState(), "tab", 2), "down", 2)
    assert s.scroll == 1
    assert handle_key(s, "enter", 2).scroll == 0 and handle_key(s, "?", 2).scroll == 0


def test_interactive_pane_in_a_real_pty_against_the_real_server():
    import fcntl
    import os
    import pty
    import select
    import struct
    import subprocess
    import sys
    import termios
    import time
    server = LiveServer()
    try:
        ingest(server.repo, 1, "session.started", agent_id="a1", session_id="s1")
        ingest(server.repo, 2, "delegation.accepted", agent_id="a1", session_id="s1", task_id="t1",
               payload={"target_agent_id": "a2"})
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        env = dict(os.environ, TERM="xterm-256color", LANG="en_US.UTF-8", HERDR_CP_URL=server.url, HERDR_CP_API_KEY="view-key")
        proc = subprocess.Popen([sys.executable, "-m", "herdr_engineering.cli", "agent-ops"], stdin=slave, stdout=slave,
                                stderr=slave, env=env)
        seen = b""

        def pump(seconds):
            nonlocal seen
            end = time.time() + seconds
            while time.time() < end:
                if select.select([master], [], [], 0.1)[0]:
                    seen += os.read(master, 65536)
        pump(2.0)
        for key in (b"h", b"\x1b", b"?", b"\x1b", b"r", b"[", b"l", b"q"):
            os.write(master, key)
            pump(0.5)
        assert proc.wait(timeout=5) == 0
        text = seen.decode("utf-8", "replace")
        for needle in ("[LIVE]", "HANDOFFS AND DELEGATIONS", "KEYS", "REPLAY", "model ?"):
            assert needle in text, needle
    finally:
        server.stop()
