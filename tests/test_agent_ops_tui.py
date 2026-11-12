"""Terminal Agent Operations map: pure render, key state machine, and the real control-plane server end to end."""
import hashlib
import http.client
import json
import re
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
        self.collab = CollabService(auth)
        self.srv = ControlPlaneServer(self.repo, host="127.0.0.1", port=0, collab=self.collab,
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
            assert "opus" in text and "model ?" in text and "ctx unknown" in text, (w, h)
        cursors = client.all_cursors()
        assert len(cursors) >= 4 and cursors == sorted(cursors)
        early = client.get("replay", until=cursors[1])
        assert early["counts"]["edges"] == 0 and snapshot["counts"]["edges"] == 1
        assert plain(render(early, 120, 40, "replay", opts=RenderOpts(status="replay", replay_pos=(1, len(cursors))))).count("REPLAY") >= 1
    finally:
        server.stop()


def test_scrubbing_with_client_obtained_cursors_is_exact_even_when_event_ids_do_not_sort_by_arrival():
    server = LiveServer()
    try:
        ingest(server.repo, 1, "tool.started", agent_id="alpha", session_id="s-alpha", event_id="e1")
        ingest(server.repo, 2, "tool.started", agent_id="bravo", session_id="s-bravo", event_id="e2")
        ingest(server.repo, 3, "tool.started", agent_id="charlie", session_id="s-charlie", event_id="z3")
        client = tui.Client(server.url, "view-key")
        cursors = client.all_cursors()
        assert len(cursors) == 3
        for index, cursor in enumerate(cursors):
            assert client.get("replay", until=cursor)["counts"]["agents"] == index + 1
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


def test_agents_panel_spells_out_attention_and_long_models():
    data = snap([agent("opus", state="attention", model="claude-opus")], [])
    text = plain(render(data, 120, 40, opts=UNI))
    assert "attention" in text and "claude-opu" in text and "attn" not in text.split("Agents")[1]


def test_ring_layout_surrounds_a_hub_and_labels_keep_the_kind():
    names = ["top", "left", "right", "bottom"]
    data = snap([agent(n) for n in names], [edge("delegation", "top", "right", "accepted", task="api-impl-long")])
    lines = plain(render(data, 120, 40, opts=RenderOpts(pane="none"))).splitlines()
    text = "\n".join(lines)
    assert "ops hub" in text and "4 agents" in text
    pos = [next((r, l.index(n)) for r, l in enumerate(lines) if re.search(rf"[●◆○◇] ▸?{n}\b", l)) for n in names]
    assert len({p[0] for p in pos}) >= 3 and len({p[1] for p in pos}) >= 3   # top / sides / bottom, not corner-pinned
    assert "delegate" in text and "deleg:" not in text


def test_focus_is_visible_without_colour_and_in_reverse_video_with_it():
    data = snap([agent("a"), agent("b"), agent("c")], [])
    assert "▸a" in plain(render(data, 120, 40, focus=0, opts=UNI)) and "▸b" not in plain(render(data, 120, 40, focus=0, opts=UNI))
    assert "\x1b[" in "\n".join(render(data, 120, 40, focus=1, opts=RenderOpts(color=True)))
    assert "7" in "".join(re.findall(r"\x1b\[([0-9;]*)m", "\n".join(render(data, 120, 40, focus=1, opts=RenderOpts(color=True)))))


def test_pulse_moves_with_the_tick_and_holds_still_under_reduced_motion():
    data = snap([agent("a"), agent("b"), agent("c")], [edge("delegation", "a", "b", "accepted")])
    frames = {plain(render(data, 120, 40, tick=t, opts=RenderOpts(pane="none", reduced_motion=False))) for t in range(4)}
    still = {plain(render(data, 120, 40, tick=t, opts=RenderOpts(pane="none", reduced_motion=True))) for t in range(4)}
    assert len(frames) > 1 and len(still) == 1


def test_real_room_becomes_the_hub_header():
    from herdr_engineering.control_plane.collab import Principal
    server = LiveServer()
    try:
        alice = Principal("user", "alice", "alice")
        project = server.collab.create_project(alice, {"name": "proj"})
        room = server.collab.create_room(alice, project["project_id"], {"name": "war-room"})["room_id"]
        for n, who in enumerate(("a1", "a2", "a3"), 1):
            ingest(server.repo, n, "session.started", agent_id=who, session_id=who, room_id=room, visibility_scope="room")
        ingest(server.repo, 9, "message.sent", agent_id="a1", session_id="a1", room_id=room, visibility_scope="room",
               payload={"target_agent_id": "a2"})
        snapshot = tui.Client(server.url, "view-key", "tok-alice-0123456789").get("snapshot")
        assert [r["room_id"] for r in snapshot["rooms"]] == [room]
        text = plain(render(snapshot, 120, 40, opts=UNI))
        assert "3 members" in text and f"room …{room[-6:]}" in text
        assert not tui.Client(server.url, "view-key").get("snapshot")["rooms"]
    finally:
        server.stop()


# ---------------------------------------------------------------- N4/N5: adversarial text never breaks layout
BIDI = "‪‫‬‭‮⁦⁧⁨⁩‎‏؜"
ZERO = "​‌‍⁠﻿­"
EVIL = ["‮evil‬" + ZERO + "name", "日本語エージェント名前長い", "🙂🙂🙂🙂🙂🙂🙂🙂🙂🙂🙂", "a" + ZERO * 5 + BIDI + "b", "é́́x"]


def test_clean_strips_bidi_and_zero_width_and_counts_cells():
    assert tui.clean("a" + BIDI + ZERO + "b") == "ab"
    assert tui.clean("日本語", 40) == "日本語" and tui.cell_width("日本語") == 6
    assert tui.cell_width(tui.clean("日本語エージェント" * 3, 10)) <= 10
    assert tui.clean("日本語エージェント" * 3, 10).endswith("…")
    assert tui.clean("🙂" * 30, 9) == "🙂" * 4 + "…"          # never half an emoji: 4 x 2 cells + ellipsis = 9
    assert tui.clean("x" * 50, 10) == "x" * 9 + "…"


def test_truncation_happens_after_sanitising_so_no_sequence_is_split():
    text = "ab\x1b[31mcd\x1b[0mefghij"
    out = tui.clean(text, 8)
    assert "\x1b" not in out and tui.cell_width(out) <= 8
    assert tui.clean("a‮" + "b" * 20, 5) == "abbb…"        # the control chars do not eat the budget


@pytest.mark.parametrize("w,h", [(120, 40), (80, 24), (50, 20)])
def test_adversarial_labels_never_break_the_layout(w, h):
    agents = [agent(f"n{i}", label=label, model=label, ctx={"used": 50, "limit": 100, "source": None}) for i, label in enumerate(EVIL)]
    data = snap(agents, [edge("delegation", "n0", "n1"), edge("handoff", "n2", "n3", task=EVIL[1])])
    data["tasks"][0]["task_id"] = EVIL[0]
    for opts in (UNI, ASCII, RenderOpts(color=True)):
        lines = render(data, w, h, opts=opts)
        assert len(lines) == h
        text = plain(lines)
        assert not any(c in text for c in BIDI + ZERO)
        assert all(visible_width(l) <= w for l in lines)
        if w >= 100:   # framed rows keep their right border in one column
            assert {visible_width(l) for l in lines[2:-1]} == {w}


def test_canvas_wide_glyph_occupies_two_cells_and_is_never_split():
    cv = tui.Canvas(6, 1)
    cv.text(0, 0, "日本語", limit=5)
    assert cv.lines(False)[0] == "日本"                       # a third wide glyph would need cells 4-5 > limit 5
    cv = tui.Canvas(3, 1)
    cv.text(2, 0, "日")
    assert visible_width(cv.lines(False)[0]) <= 3 and "日" not in cv.lines(False)[0]
    cv = tui.Canvas(4, 1)
    cv.text(0, 0, "日日")
    cv.put(1, 0, "x")                                           # overwriting half a glyph blanks the other half
    assert cv.lines(False)[0] == " x日"


# ---------------------------------------------------------------- N6: the token must not have to live in `ps`
def _args(**kw):
    import argparse
    return argparse.Namespace(**{"token": None, "token_file": None, **kw})


def test_token_sources_and_precedence(tmp_path):
    f = tmp_path / "tok"
    f.write_text("  from-file\n")
    assert tui.resolve_token(_args(token_file=str(f), token="argv"), {"HERDR_AGENT_OPS_TOKEN": "env"}) == ("from-file", False)
    assert tui.resolve_token(_args(token="argv"), {"HERDR_AGENT_OPS_TOKEN": "env"}) == ("argv", True)
    assert tui.resolve_token(_args(), {"HERDR_AGENT_OPS_TOKEN": "env", "HERDR_COLLAB_TOKEN": "c"}) == ("env", False)
    assert tui.resolve_token(_args(), {"HERDR_COLLAB_TOKEN": "c"}) == ("c", False)
    assert tui.resolve_token(_args(), {}) == ("", False)


def test_token_file_unreadable_is_a_clear_error(tmp_path):
    with pytest.raises(SystemExit) as exc:
        tui.resolve_token(_args(token_file=str(tmp_path / "missing")), {})
    assert "token-file" in str(exc.value)


def test_cli_warns_about_argv_token_but_not_env_or_file(tmp_path):
    import subprocess
    import sys
    server = LiveServer()
    try:
        tokfile = tmp_path / "tok"
        tokfile.write_text("tok-alice-0123456789\n")
        base = [sys.executable, "-m", "herdr_engineering.cli", "agent-ops", "--once", "--url", server.url, "--api-key", "view-key"]
        env = {k: v for k, v in __import__("os").environ.items() if not k.startswith("HERDR_")}
        argv = subprocess.run(base + ["--token", "tok-alice-0123456789"], env=env, capture_output=True, text=True, timeout=30)
        viaenv = subprocess.run(base, env={**env, "HERDR_AGENT_OPS_TOKEN": "tok-alice-0123456789"}, capture_output=True, text=True, timeout=30)
        viafile = subprocess.run(base + ["--token-file", str(tokfile)], env=env, capture_output=True, text=True, timeout=30)
        assert argv.returncode == viaenv.returncode == viafile.returncode == 0
        assert "visible in the process list" in argv.stderr and "tok-alice" not in argv.stderr
        assert viaenv.stderr == "" and viafile.stderr == ""
    finally:
        server.stop()


# ---------------------------------------------------------------- health, drift, hosts, stalled/failed handoffs
def health(fraction):
    return {"fraction": fraction, "level": "unknown" if fraction is None else "ok", "source": None, "measured": fraction is not None}


def aggregate(avg, measured, total):
    return {"measured_count": measured, "total": total, "average_fraction": avg, "level": "unknown" if avg is None else "ok"}


def drift(kind="repeated_tool_failures", task="t1"):
    return {"kind": kind, "provenance": "derived", "evidence_event_ids": ["e1"], "task_id": task, "heuristic": True,
            "note": "Heuristic signal derived from event metadata; not a proven policy violation."}


def health_snap():
    agents = [agent("calm", ctx={"used": 40, "limit": 100, "source": None}), agent("warm", ctx={"used": 72, "limit": 100, "source": None}),
              agent("hot", ctx={"used": 95, "limit": 100, "source": None}), agent("blank", ctx=None)]
    for a, f in zip(agents, (0.4, 0.72, 0.95, None)):
        a["context_health"] = health(f)
        a["drift"] = []
    agents[2]["drift"] = [drift()]
    data = snap(agents, [edge("handoff", "calm", "warm", "stalled", "t1"), edge("handoff", "hot", "blank", "failed", "t2"),
                         edge("handoff", "calm", "hot", "accepted", "t3")])
    data["tasks"][0]["drift"] = [drift()]
    data["hosts"] = [{"host_id": "mac-01", "agents": ["calm", "warm"], "state": "connected", "connected": True, "last_seen": None,
                      "reconnects": 0, "context_health": aggregate(0.56, 2, 2)},
                     {"host_id": "dev-02", "agents": ["hot"], "state": "disconnected", "connected": False, "last_seen": None,
                      "reconnects": 1, "context_health": aggregate(0.95, 1, 1)},
                     {"host_id": "lab-03", "agents": ["blank"], "state": "unknown", "connected": None, "last_seen": None,
                      "reconnects": 0, "context_health": aggregate(None, 0, 1)}]
    data["health"] = {"context": aggregate(0.69, 3, 4)}
    data["counts"]["hosts"] = 3
    return data


@pytest.mark.parametrize("w,h", [(120, 40), (80, 24)])
def test_health_features_render_in_both_layouts(w, h):
    for opts in (UNI, ASCII, RenderOpts(color=True)):
        lines = render(health_snap(), w, h, opts=opts)
        assert len(lines) == h and all(visible_width(l) <= w for l in lines)
    text = plain(render(health_snap(), w, h, opts=UNI))
    assert re.search(r"hosts 3:.*mac-01", text) and "dev-02" in text
    assert "ctx 69%" in text and "ctx unknown" in text            # fleet average and an unmeasured agent/host
    assert "0%" not in text.replace("40%", "").replace("70%", "")  # unknown is never drawn as zero
    assert "~drift?" in text                                       # heuristic, never asserted as fact
    assert "STALLED" in text and "FAILED" in text


def test_hosts_row_keeps_unknown_unknown():
    text = plain(render(health_snap(), 120, 40, opts=UNI))
    row = next(l for l in text.splitlines() if l.startswith("hosts") or " hosts " in l)
    assert "lab-03 ? ctx unknown" in row and "dev-02 × ctx 95%" in row and "mac-01 ● ctx 56%" in row


def test_context_badge_thresholds_and_colours():
    coloured = "\n".join(render(health_snap(), 120, 40, opts=RenderOpts(color=True)))
    assert re.search(r"\x1b\[38;5;78m\[[█░]+\] 40%\x1b", coloured)                 # green
    assert re.search(r"\x1b\[38;5;215m\[[█░]+\] 72%!\x1b", coloured)               # amber from 70
    assert re.search(r"\x1b\[38;5;203m\[[█░]+\] 95%!!\x1b", coloured)              # red from 90
    assert "ctx unknown" in plain(render(health_snap(), 120, 40, opts=UNI))
    assert "\x1b" not in "\n".join(render(health_snap(), 120, 40, opts=UNI))      # NO_COLOR path stays escape-free


def test_stalled_and_failed_handoffs_are_distinct_from_accepted_and_planned():
    data = health_snap()
    text = plain(render(data, 120, 40, mode="handoffs", opts=UNI))
    assert "handoff STALLED" in text and "handoff FAILED" in text and "✗" in text
    ascii_text = plain(render(data, 120, 40, mode="handoffs", opts=ASCII))
    assert "STALLED" in ascii_text and "FAILED" in ascii_text and "x" in ascii_text
    red = "\n".join(render(data, 120, 40, mode="handoffs", opts=RenderOpts(color=True)))
    assert re.search(r"\x1b\[38;5;203mhandoff STALLED", red) and re.search(r"\x1b\[38;5;203mhandoff FAILED", red)


def test_drift_is_marked_heuristic_in_tasks_and_inspect():
    data = health_snap()
    assert "~drift? repeated_tool_failures" in plain(render(data, 120, 40, opts=UNI))
    inspect = plain(render(data, 120, 40, focus=2, opts=RenderOpts(overlay="inspect")))
    assert "heuristic" in inspect and "repeated_tool_failures" in inspect and "ctx health" in inspect


def test_legacy_snapshot_without_new_fields_still_renders():
    text = plain(render(snap(), 120, 40, opts=UNI))
    assert "hosts" not in text and "~drift?" not in text and "STALLED" not in text


def test_real_server_snapshot_with_hosts_renders_end_to_end():
    server = LiveServer()
    try:
        ingest(server.repo, 1, "session.started", agent_id="a1", session_id="s1", host_id="mac-01")
        ingest(server.repo, 2, "context.metric", agent_id="a1", session_id="s1", payload={"context_used": 91, "context_limit": 100})
        ingest(server.repo, 3, "session.started", agent_id="a2", session_id="s2", host_id="dev-02")
        snapshot = tui.Client(server.url, "view-key").get("snapshot")
        for w, h in ((120, 40), (80, 24)):
            text = plain(render(snapshot, w, h, opts=UNI))
            assert "hosts 2:" in text and "mac-01" in text and "ctx unknown" in text
        assert re.search(r"\] 91%!!", plain(render(snapshot, 120, 40, opts=UNI)))
    finally:
        server.stop()


def test_hosts_row_shortens_instead_of_dropping_hosts_in_a_narrow_pane():
    row = plain([tui.hosts_row(health_snap(), 80, UNI)])
    assert tui.cell_width(row) <= 80 and all(h in row for h in ("mac-01", "dev-02", "lab-03"))
    assert "lab-03 ? ?" in row           # unknown host state and unknown context both stay "?"
