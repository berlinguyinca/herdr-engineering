"""Agent Operations projection: deterministic fold, authorization, redaction, and the HTTP routes."""
import copy
import hashlib
import http.client
import json
import random
import threading

import pytest

from herdr_engineering.control_plane import agent_ops
from herdr_engineering.control_plane.agent_ops import AgentOpsService, Viewer, cursor_of, fold
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.collab import CollabService, Principal, StaticTokenAuthenticator
from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.repo import ControlPlaneRepo

OPERATOR = {"kind": "operator", "id": "key:op", "authenticated_by": "api_key"}
TOKENS = {"alice": "tok-alice-0123456789", "bob": "tok-bob-0123456789abc", "carol": "tok-carol-0123456789ab"}


def ev(n, event_type, *, agent="a1", source="pi", seq=None, at=None, **fields):
    stamp = at or f"2026-11-12T10:00:{n:02d}.000000Z"
    base = agent_ops._event(event_id=f"e{n}", source=source, sequence=n if seq is None else seq,
                            event_type=event_type, occurred_at=stamp, ingested_at=stamp, agent_id=agent,
                            visibility_scope="org", provenance="observed")
    base.update(fields)
    return base


# ----------------------------------------------------------------------------------- fold: ordering
def timeline():
    return [
        ev(1, "session.started", session_id="s1", host_id="h1"),
        ev(2, "model.selected", session_id="s1", model_id="opus"),
        ev(3, "model.switched", session_id="s1", model_id="sonnet"),
        ev(4, "context.metric", session_id="s1", payload={"context_used": 10, "context_limit": 100,
                                                          "context_source": "pi"}),
        ev(5, "tool.started", session_id="s1"),
        ev(6, "delegation.proposed", session_id="s1", task_id="t1", payload={"target_agent_id": "a2"}),
        ev(7, "delegation.accepted", session_id="s1", task_id="t1", payload={"target_agent_id": "a2"}),
    ]


def test_dedupe_by_event_id():
    events = timeline()
    assert fold(events + copy.deepcopy(events)) == fold(events)


def test_out_of_order_and_duplicate_delivery_gives_identical_snapshot():
    events = timeline()
    expected = fold(events)
    for seed in range(20):
        shuffled = events + events[:3]
        random.Random(seed).shuffle(shuffled)
        assert fold(shuffled) == expected


def test_replay_is_deterministic_and_up_to_limits_history():
    events = timeline()
    assert fold(events, cursor_of(events[2])) == fold(events, cursor_of(events[2]))
    early = fold(events, cursor_of(events[1]))
    (agent,) = [a for a in early["agents"] if a["node_id"] == "s1"]
    assert agent["model"] == "opus" and agent["context"] is None
    assert early["edges"] == []
    assert fold(events)["cursor"] == cursor_of(events[-1])


def test_per_source_sequence_wins_over_skewed_timestamps():
    late_clock = ev(1, "model.selected", session_id="s1", model_id="first", seq=1, at="2026-11-12T10:00:30.000000Z")
    early_clock = ev(2, "model.switched", session_id="s1", model_id="second", seq=2, at="2026-11-12T10:00:05.000000Z")
    (agent,) = fold([early_clock, late_clock])["agents"]
    assert agent["model"] == "second" and [c["to"] for c in agent["model_changes"]] == ["first", "second"]


# ------------------------------------------------------------------------------------- fold: semantics
def test_unknown_stays_unknown():
    snap = fold([ev(1, "tool.started", agent="a9")])
    (agent,) = snap["agents"]
    assert agent["model"] is None and agent["context"] is None and agent["host_id"] is None
    assert agent["provenance"] == "observed" and agent["state"] == "working"
    assert fold([ev(1, "session.started", agent="a9", provenance="unknown")])["agents"][0]["state"] == "idle"
    assert fold([ev(1, "room.joined", agent="a9", room_id="r1", visibility_scope="room")])["agents"][0]["state"] == "unknown"


def test_model_switch_updates_same_node_but_new_session_is_new_node():
    switch = [ev(1, "session.started", session_id="s1"), ev(2, "model.selected", session_id="s1", model_id="m1"),
              ev(3, "model.switched", session_id="s1", model_id="m2")]
    snap = fold(switch)
    assert [a["node_id"] for a in snap["agents"]] == ["s1"]
    assert snap["agents"][0]["model"] == "m2" and len(snap["agents"][0]["model_changes"]) == 2
    snap = fold(switch + [ev(4, "session.started", session_id="s2", parent_session_id="s1", model_id="m2")])
    assert [a["node_id"] for a in snap["agents"]] == ["s1", "s2"]
    assert snap["agents"][1]["parent_session_id"] == "s1"


def test_sessionless_node_adopts_its_session_and_edges_follow():
    events = [ev(1, "room.joined", agent="a1", room_id="r1", visibility_scope="room"),
              ev(2, "message.sent", agent="a1", room_id="r1", visibility_scope="room", payload={"target_agent_id": "a2"}),
              ev(3, "session.started", agent="a1", session_id="s1")]
    snap = fold(events)
    assert sorted(a["node_id"] for a in snap["agents"]) == ["a2", "s1"]
    assert snap["edges"][0]["from"] == "s1"


def test_context_needs_a_measured_numerator():
    snap = fold([ev(1, "context.metric", session_id="s1", payload={"context_limit": 100})])
    assert snap["agents"][0]["context"] is None
    snap = fold([ev(1, "context.metric", session_id="s1", payload={"context_used": 5})])
    assert snap["agents"][0]["context"] == {"used": 5, "limit": None, "source": None}


def test_delegation_is_distinct_from_ownership_contribution_and_review():
    events = [
        ev(1, "owner.transferred", agent="a1", task_id="t1", payload={"new_owner_agent_id": "a1"}),
        ev(2, "delegation.proposed", agent="a1", task_id="t1", payload={"target_agent_id": "a2"}),
        ev(3, "delegation.accepted", agent="a1", task_id="t1", payload={"target_agent_id": "a2"}),
        ev(4, "review.requested", agent="a1", task_id="t1", payload={"target_agent_id": "a3"}),
        ev(5, "artifact.created", agent="a4", task_id="t1"),
    ]
    snap = fold(events)
    (task,) = snap["tasks"]
    assert task["accountable_owner"] == "a1" and task["reviewer"] == "a3"
    assert task["contributors"] == ["a2", "a4"]
    kinds = {(e["kind"], e["to"], e["state"]) for e in snap["edges"]}
    assert kinds == {("ownership", "a1", "transferred"), ("delegation", "a2", "accepted"), ("review", "a3", "planned")}
    owner = fold(events + [ev(6, "delegation.accepted", agent="a1", task_id="t1", payload={"target_agent_id": "a5"})])
    assert owner["tasks"][0]["accountable_owner"] == "a1"       # delegating never moves ownership
    moved = fold(events + [ev(6, "owner.transferred", agent="a1", task_id="t1", payload={"new_owner_agent_id": "a2"})])
    assert moved["tasks"][0]["accountable_owner"] == "a2"


def test_edge_without_both_endpoints_is_never_invented():
    assert fold([ev(1, "message.sent", agent="a1")])["edges"] == []
    assert fold([ev(1, "delegation.proposed", agent=None, payload={"target_agent_id": "a2"})])["edges"] == []


def test_drift_is_counted_not_asserted():
    snap = fold([ev(1, "drift.warning", agent="a1", provenance="inferred")])
    assert snap["agents"][0]["drift_warnings"] == 1 and snap["agents"][0]["provenance"] == "inferred"


def test_snapshot_is_bounded():
    many = [ev(i % 60, "tool.started", agent=f"a{i}", source=f"s{i}", seq=0) | {"event_id": f"x{i}"}
            for i in range(agent_ops.MAX_AGENTS + 20)]
    snap = fold(many)
    assert len(snap["agents"]) == agent_ops.MAX_AGENTS and snap["truncated"] is True


# --------------------------------------------------------------------------- sources, redaction, authorization
def ingest(repo, n, event_type, **fields):
    body = {"schema_version": 2, "event_id": f"in-{n}", "source": "pi", "source_sequence": n,
            "entity_type": "lifecycle", "entity_id": "a1", "event_type": event_type,
            "occurred_at": f"2026-11-12T10:00:{n:02d}Z", "visibility_scope": "org",
            "agent_id": "a1", "provenance": "observed", "payload": {}, **fields}
    IngestService(repo).ingest(body, OPERATOR)


def test_envelope_events_are_projected_and_unknown_types_ignored():
    repo = ControlPlaneRepo()
    ingest(repo, 1, "model.selected", session_id="s1", model_id="m1", sanitized_label="plan /home/x/y.py")
    ingest(repo, 2, "something.else", session_id="s1")
    svc = AgentOpsService(repo)
    snap = svc.snapshot(Viewer(operator=True))
    assert snap["agents"][0]["model"] == "m1" and snap["counts"]["events"] == 1
    assert "/home" not in json.dumps(snap)


def test_projection_never_emits_content_or_paths():
    repo = ControlPlaneRepo()
    ingest(repo, 1, "tool.failed", session_id="s1", sanitized_label="failed in /Users/me/secret.py",
           payload={"prompt": "SECRET PROMPT", "error": "boom at /etc/passwd", "command": "rm -rf /",
                    "context_used": 3})
    svc = AgentOpsService(repo)
    dump = json.dumps([svc.snapshot(Viewer(operator=True)), svc.events(Viewer(operator=True)),
                       svc.replay(Viewer(operator=True))])
    for leak in ("SECRET PROMPT", "/etc/passwd", "rm -rf", "/Users/me"):
        assert leak not in dump


def test_scope_fails_closed_without_room_or_operator():
    repo = ControlPlaneRepo()
    ingest(repo, 1, "tool.started", session_id="s1", visibility_scope="private")
    ingest(repo, 2, "tool.started", session_id="s2", agent_id="a2", visibility_scope="org")
    svc = AgentOpsService(repo)
    assert [a["node_id"] for a in svc.snapshot(Viewer())["agents"]] == ["s2"]
    assert [a["node_id"] for a in svc.snapshot(Viewer(operator=True))["agents"]] == ["s1", "s2"]


@pytest.fixture
def stack(acknowledged_sink):
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    auth = StaticTokenAuthenticator({u: hashlib.sha256(t.encode()).hexdigest() for u, t in TOKENS.items()})
    collab = CollabService(auth, sink=acknowledged_sink)
    users = {name: Principal("user", name, name) for name in TOKENS}
    rooms = {}
    for name in ("alice", "bob"):               # carol owns no room: she is only ever a guest
        project = collab.create_project(users[name], {"name": f"p-{name}"})
        rooms[name] = collab.create_room(users[name], project["project_id"], {"name": f"r-{name}"})["room_id"]
        assert collab.post_message(users[name], rooms[name], {"text": f"hi from {name} SECRETTEXT",
                                                              "idempotency_key": f"k-{name}"})[0] in (200, 201)
    return type("Stack", (), dict(repo=repo, collab=collab, users=users, rooms=rooms,
                                  svc=AgentOpsService(repo, collab)))


def test_no_cross_room_leak_including_counts(stack):
    ingest(stack.repo, 1, "tool.started", session_id="sa", agent_id="agent-a", room_id=stack.rooms["alice"],
           visibility_scope="room")
    ingest(stack.repo, 2, "tool.started", session_id="sb", agent_id="agent-b", room_id=stack.rooms["bob"],
           visibility_scope="room")
    alice, bob = stack.users["alice"], stack.users["bob"]
    snap = stack.svc.snapshot(stack.svc.viewer(alice), alice)
    dump = json.dumps(snap)
    assert stack.rooms["bob"] not in dump and "agent-b" not in dump and "bob" not in dump
    assert "SECRETTEXT" not in dump
    assert {r["room_id"] for r in snap["rooms"]} == {stack.rooms["alice"]}
    assert snap["counts"]["rooms"] == 1 and snap["counts"]["agents"] == 1
    other = stack.svc.snapshot(stack.svc.viewer(bob), bob)
    assert stack.rooms["alice"] not in json.dumps(other) and "agent-a" not in json.dumps(other)
    for page in (stack.svc.events(stack.svc.viewer(alice), alice), stack.svc.replay(stack.svc.viewer(alice), alice)):
        assert stack.rooms["bob"] not in json.dumps(page)


def test_api_key_without_a_collab_identity_reads_no_room(stack):
    ingest(stack.repo, 1, "tool.started", session_id="sa", agent_id="agent-a", room_id=stack.rooms["alice"],
           visibility_scope="room")
    snap = stack.svc.snapshot(stack.svc.viewer(None, operator=True))
    assert snap["agents"] == [] and snap["rooms"] == []


def test_replay_resumes_from_cursor_without_loss(stack):
    alice = stack.users["alice"]
    for n in range(1, 6):
        ingest(stack.repo, n, "tool.started", session_id=f"s{n}", agent_id=f"x{n}", visibility_scope="org")
    viewer = stack.svc.viewer(alice)
    first = stack.svc.events(viewer, alice, None, 2)
    seen = list(first["events"])
    cursor = first["next_cursor"]
    while True:
        page = stack.svc.events(viewer, alice, cursor, 2)
        seen += page["events"]
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
    assert [e["session_id"] for e in seen if e["session_id"]] == [f"s{n}" for n in range(1, 6)]


# ----------------------------------------------------------------------------------------------- handoffs
def test_handoff_edge_comes_only_from_a_recorded_handoff(stack):
    registry = stack.svc.handoffs = __import__("herdr_engineering.control_plane.handoffs",
                                               fromlist=["HandoffRegistry"]).HandoffRegistry(stack.repo)
    registry.create({"handoff_id": "ho1", "source_harness": "pi", "target_harness": "claude", "goal": "SECRET GOAL",
                     "source_agent_id": "a1", "source_session_id": "s1", "target_agent_id": "a2",
                     "target_session_id": "s2", "room_id": stack.rooms["alice"]}, {"id": "alice"})
    alice, bob = stack.users["alice"], stack.users["bob"]
    snap = stack.svc.snapshot(stack.svc.viewer(alice), alice)
    (edge,) = [e for e in snap["edges"] if e["kind"] == "handoff"]
    assert (edge["from"], edge["to"], edge["state"], edge["handoff_id"]) == ("s1", "s2", "planned", "ho1")
    assert "SECRET GOAL" not in json.dumps(snap)
    assert stack.svc.snapshot(stack.svc.viewer(bob), bob)["edges"] == []
    registry.transition("ho1", "claimed", {"id": "x"})
    registry.transition("ho1", "accepted", {"id": "x"})
    snap = stack.svc.snapshot(stack.svc.viewer(alice), alice)
    assert [e["state"] for e in snap["edges"] if e["kind"] == "handoff"] == ["accepted"]


# --------------------------------------------------------------------------------------------------- HTTP
class Server:
    def __init__(self, repo, collab, **kw):
        self.srv = ControlPlaneServer(repo, host="127.0.0.1", port=0, collab=collab,
                                      api_keys={"op-key": "operator", "view-key": "viewer"}, **kw)
        self.thread = threading.Thread(target=self.srv.serve, daemon=True)
        self.thread.start()
        self.srv.wait_until_ready()

    def get(self, path, *, user=None, key="view-key"):
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=5)
        headers = {"X-API-Key": key}
        if user:
            headers["Authorization"] = f"Bearer {TOKENS[user]}"
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp.status, json.loads(raw) if raw else None

    def stop(self):
        self.srv.shutdown()
        self.thread.join(timeout=5)


def test_capability_off_means_404_and_no_capability(stack):
    server = Server(stack.repo, stack.collab)
    try:
        for path in ("snapshot", "events", "replay", "stream"):
            assert server.get(f"/api/v1/agent-ops/{path}")[0] == 404
        assert "agent_ops_v1" not in server.get("/api/v1/health")[1]["capabilities"]
    finally:
        server.stop()


def test_routes_are_room_scoped_over_http(stack):
    ingest(stack.repo, 1, "tool.started", session_id="sa", agent_id="agent-a", room_id=stack.rooms["alice"],
           visibility_scope="room")
    ingest(stack.repo, 2, "tool.started", session_id="sb", agent_id="agent-b", room_id=stack.rooms["bob"],
           visibility_scope="room")
    server = Server(stack.repo, stack.collab, agent_ops_config={"enabled": True})
    try:
        assert "agent_ops_v1" in server.get("/api/v1/health")[1]["capabilities"]
        status, snap = server.get("/api/v1/agent-ops/snapshot", user="alice")
        assert status == 200 and "agent-b" not in json.dumps(snap) and snap["counts"]["rooms"] == 1
        assert [a["node_id"] for a in snap["agents"]] == ["sa"]
        status, page = server.get("/api/v1/agent-ops/events?limit=1", user="alice")
        assert status == 200 and len(page["events"]) == 1 and "next_cursor" in page
        status, replay = server.get(f"/api/v1/agent-ops/replay?until={snap['cursor']}", user="alice")
        assert status == 200 and replay == snap
        assert server.get("/api/v1/agent-ops/snapshot")[1]["agents"] == []          # API key alone: no room
        assert server.get("/api/v1/agent-ops/snapshot", user="alice", key="bad")[0] == 401
        assert server.get("/api/v1/agent-ops/nope")[0] == 404
    finally:
        server.stop()


def test_stream_resumes_by_cursor_and_is_room_scoped(stack):
    ingest(stack.repo, 1, "tool.started", session_id="sa", agent_id="agent-a", room_id=stack.rooms["alice"],
           visibility_scope="room")
    ingest(stack.repo, 2, "tool.started", session_id="sb", agent_id="agent-b", room_id=stack.rooms["bob"],
           visibility_scope="room")
    server = Server(stack.repo, stack.collab, agent_ops_config={"enabled": True})
    server.srv.agent_ops_poll_seconds = 0.05
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.srv.port, timeout=5)
        conn.request("GET", "/api/v1/agent-ops/stream", headers={"X-API-Key": "view-key",
                                                                  "Authorization": f"Bearer {TOKENS['alice']}"})
        resp = conn.getresponse()
        assert resp.status == 200
        frames, cursor = [], None
        while not any(f.get("session_id") == "sa" for f in frames):
            line = resp.fp.readline().decode().strip()
            if line.startswith("id:"):
                cursor = line[3:].strip()
            if line.startswith("data:") and "StreamConnected" not in line and '"event_id"' in line:
                frames.append(json.loads(line[5:]))
        conn.close()
        assert cursor == cursor_of(frames[-1]) and frames[-1]["session_id"] == "sa"
        assert all(f["room_id"] != stack.rooms["bob"] for f in frames)
        _, resumed = server.get(f"/api/v1/agent-ops/events?cursor={cursor}", user="alice")
        assert resumed["events"] == []
    finally:
        server.stop()


# ------------------------------------------------------------------------------ review fixes (backend)
def make_registry(stack):
    from herdr_engineering.control_plane.handoffs import HandoffRegistry
    stack.svc.handoffs = HandoffRegistry(stack.repo)
    return stack.svc.handoffs


def new_handoff(registry, stack, hid):
    registry.create({"handoff_id": hid, "source_harness": "pi", "target_harness": "claude", "goal": "g",
                     "source_agent_id": "a1", "source_session_id": "s1", "target_agent_id": "a2",
                     "target_session_id": "s2", "room_id": stack.rooms["alice"]}, {"id": "alice"})


@pytest.mark.parametrize("path,expected", [
    (("claimed", "accepted", "in_progress", "failed"), "failed"),
    (("expired",), "expired"),
    (("cancelled",), "cancelled"),
    (("claimed", "rejected"), "rejected"),
    (("claimed", "accepted", "in_progress", "completed"), "completed"),
])
def test_registry_terminal_handoff_states_fold_without_error(stack, path, expected):
    registry = make_registry(stack)
    new_handoff(registry, stack, "ho1")
    for state in path:
        registry.transition("ho1", state, {"id": "x"})
    alice = stack.users["alice"]
    snap = stack.svc.snapshot(stack.svc.viewer(alice), alice)
    (edge,) = [e for e in snap["edges"] if e["kind"] == "handoff"]
    assert edge["state"] == expected
    replay = stack.svc.replay(stack.svc.viewer(alice), alice)
    assert replay == snap
    for node in snap["agents"]:
        for drift in node["drift"]:
            assert None not in drift["evidence_event_ids"]


def test_failed_handoff_drift_omits_missing_interrupter(stack):
    registry = make_registry(stack)
    new_handoff(registry, stack, "ho1")
    for state in ("claimed", "accepted", "in_progress", "failed"):
        registry.transition("ho1", state, {"id": "x"})
    alice = stack.users["alice"]
    snap = stack.svc.snapshot(stack.svc.viewer(alice), alice)
    kinds = {d["kind"] for n in snap["agents"] for d in n["drift"]}
    assert kinds == {"handoff_failed"}
    assert "interrupted_by" not in [e for e in snap["edges"] if e["kind"] == "handoff"][0]


def test_two_emitters_sharing_an_event_id_do_not_erase_each_other():
    one = ev(1, "model.selected", agent="a1", session_id="s1", model_id="m1", source="pi")
    two = ev(1, "model.selected", agent="a2", session_id="s2", model_id="m2", source="pi")
    assert one["event_id"] == two["event_id"] and cursor_of(one) != cursor_of(two)
    snap = fold([one, two])
    assert {a["node_id"]: a["model"] for a in snap["agents"]} == {"s1": "m1", "s2": "m2"}
    assert snap["counts"]["events"] == 2
    assert fold([two, one, copy.deepcopy(one)]) == snap


def test_sse_unsafe_event_id_is_refused_at_ingest():
    repo = ControlPlaneRepo()
    for bad in ("x\r\nevent: forged", "x\nid: 1", "a\x00b", "has space", "y" * 129):
        with pytest.raises(ValueError):
            ingest(repo, 1, "tool.started", session_id="s1", event_id=bad)
    ingest(repo, 2, "tool.started", session_id="s1", event_id="ok.id:1~a-b_c")


def test_stream_never_writes_a_forged_frame_from_a_stored_unsafe_id(stack):
    ingest(stack.repo, 1, "tool.started", session_id="sa", agent_id="a1", visibility_scope="org")
    for stream in stack.repo._events.values():       # a row stored before the ingest constraint existed
        stream[-1]["event_id"] = "evil\r\nevent: forged\r\ndata: {}"
    ingest(stack.repo, 2, "tool.started", session_id="sb", agent_id="a2", visibility_scope="org")
    server = Server(stack.repo, stack.collab, agent_ops_config={"enabled": True})
    server.srv.agent_ops_poll_seconds = 0.05
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.srv.port, timeout=5)
        conn.request("GET", "/api/v1/agent-ops/stream", headers={"X-API-Key": "view-key",
                                                                  "Authorization": f"Bearer {TOKENS['alice']}"})
        resp = conn.getresponse()
        lines, seen = [], False
        while not seen:
            line = resp.fp.readline().decode().rstrip("\r\n")
            lines.append(line)
            seen = '"session_id": "sb"' in line
        conn.close()
        assert not any(line.startswith("event: forged") or line == "data: {}" for line in lines)
        assert all(line == "" or line.startswith(("id:", "data:", "event:", ":")) for line in lines)
    finally:
        server.stop()


def stream_conn(server, user):
    conn = http.client.HTTPConnection("127.0.0.1", server.srv.port, timeout=5)
    conn.request("GET", "/api/v1/agent-ops/stream", headers={"X-API-Key": "view-key",
                                                              "Authorization": f"Bearer {TOKENS[user]}"})
    return conn, conn.getresponse()


def test_stream_ends_when_the_credential_is_revoked_mid_stream(stack):
    ingest(stack.repo, 1, "tool.started", session_id="sa", agent_id="a1", visibility_scope="org")
    server = Server(stack.repo, stack.collab, agent_ops_config={"enabled": True})
    server.srv.agent_ops_poll_seconds = 0.05
    server.srv.agent_ops_reauth_seconds = 0.1
    try:
        conn, resp = stream_conn(server, "alice")
        assert resp.status == 200
        while '"session_id": "sa"' not in resp.fp.readline().decode():
            pass
        stack.collab.auth.disable("alice")
        rest = resp.read().decode()                  # the server closes the stream, so read() returns
        assert "event: revoked" in rest
        conn.close()
    finally:
        server.stop()


def test_concurrent_streams_are_capped(stack):
    server = Server(stack.repo, stack.collab, agent_ops_config={"enabled": True})
    server.srv.agent_ops_poll_seconds = 0.05
    server.srv.agent_ops_streams = threading.BoundedSemaphore(1)
    try:
        first, resp = stream_conn(server, "alice")
        assert resp.status == 200
        second, resp2 = stream_conn(server, "bob")
        assert resp2.status == 503
        first.close(), second.close()
    finally:
        server.stop()


def test_streaming_reads_are_cached_briefly_per_viewer(stack):
    ingest(stack.repo, 1, "tool.started", session_id="s1", agent_id="a1", visibility_scope="org")
    svc, viewer = stack.svc, Viewer(operator=True)
    assert len(svc.events(viewer, None, None, 10, cached=True)["events"]) == 1
    ingest(stack.repo, 2, "tool.started", session_id="s2", agent_id="a2", visibility_scope="org")
    assert len(svc.events(viewer, None, None, 10, cached=True)["events"]) == 1      # within the TTL
    assert len(svc.events(viewer, None, None, 10)["events"]) == 2                   # uncached reads are always fresh
    svc.CACHE_TTL = 0.0
    assert len(svc.events(viewer, None, None, 10, cached=True)["events"]) == 2


def test_envelope_tail_keeps_last_known_model_context_and_state(monkeypatch):
    monkeypatch.setattr(agent_ops, "_ENVELOPE_TAIL", 5)
    repo = ControlPlaneRepo()
    ingest(repo, 1, "model.selected", session_id="s1", model_id="opus-x")
    ingest(repo, 2, "context.metric", session_id="s1", payload={"context_used": 40, "context_limit": 100})
    for n in range(3, 20):
        ingest(repo, n, "tool.started" if n % 2 else "tool.ended", session_id="s1")
    (agent,) = AgentOpsService(repo).snapshot(Viewer(operator=True))["agents"]
    assert agent["model"] == "opus-x" and agent["context"]["used"] == 40


def test_max_events_window_keeps_last_known_model(monkeypatch):
    monkeypatch.setattr(agent_ops, "MAX_EVENTS", 5)
    events = [ev(1, "model.selected", session_id="s1", model_id="opus-x")] + [
        ev(n, "tool.started" if n % 2 else "tool.ended", session_id="s1") for n in range(2, 20)]
    (agent,) = fold(agent_ops.authorized_events(events, Viewer(operator=True)))["agents"]
    assert agent["model"] == "opus-x"


def test_room_events_read_from_the_tail(stack, monkeypatch):
    monkeypatch.setattr(agent_ops, "ROOM_EVENTS_PER_ROOM", 4)
    alice = stack.users["alice"]
    for n in range(12):
        stack.collab.post_message(alice, stack.rooms["alice"], {"text": f"m{n}", "idempotency_key": f"t-{n}"})
    events = agent_ops.room_events(stack.collab, alice, stack.rooms["alice"])
    assert len(events) == 4
    newest = stack.collab.read_tail(alice, stack.rooms["alice"], 1)["events"][0]["event_id"]
    assert events[-1]["event_id"] == newest


def test_guest_sees_room_chat_sources_only_not_envelope_or_handoff_events(stack):
    bob, alice = stack.users["bob"], stack.users["alice"]
    stack.collab.add_member(bob, stack.rooms["bob"], {"user_id": "alice", "role": "guest"})
    stack.collab.post_message(bob, stack.rooms["bob"], {"text": "after the guest joined", "idempotency_key": "g1"})
    ingest(stack.repo, 1, "tool.started", session_id="sb", agent_id="agent-b", room_id=stack.rooms["bob"],
           visibility_scope="room")
    registry = make_registry(stack)
    registry.create({"handoff_id": "hob", "source_harness": "pi", "target_harness": "claude", "goal": "g",
                     "source_agent_id": "ha1", "source_session_id": "hs1", "target_agent_id": "ha2",
                     "target_session_id": "hs2", "room_id": stack.rooms["bob"]}, {"id": "bob"})
    guest = stack.svc.snapshot(stack.svc.viewer(alice), alice)
    dump = json.dumps(guest)
    assert "agent-b" not in dump and "hs1" not in dump and guest["edges"] == []
    assert stack.rooms["bob"] in dump                      # her own view of the room's chat is still there
    member = stack.svc.snapshot(stack.svc.viewer(bob), bob)
    assert "agent-b" in json.dumps(member) and [e for e in member["edges"] if e["kind"] == "handoff"]


def test_guest_only_principal_sees_only_room_sources_of_guest_rooms_on_every_route(stack):
    bob = stack.users["bob"]
    stack.collab.add_member(bob, stack.rooms["bob"], {"user_id": "carol", "role": "guest"})
    stack.collab.post_message(bob, stack.rooms["bob"], {"text": "after the guest joined", "idempotency_key": "g1"})
    ingest(stack.repo, 1, "tool.started", session_id="so", agent_id="org-agent", visibility_scope="org")
    ingest(stack.repo, 2, "tool.started", session_id="sb", agent_id="agent-b", room_id=stack.rooms["bob"],
           visibility_scope="room")
    ingest(stack.repo, 3, "tool.started", session_id="sa", agent_id="agent-a", room_id=stack.rooms["alice"],
           visibility_scope="room")
    server = Server(stack.repo, stack.collab, agent_ops_config={"enabled": True})
    server.srv.agent_ops_poll_seconds = 0.05
    try:
        for key in ("view-key", "op-key"):
            status, snap = server.get("/api/v1/agent-ops/snapshot", user="carol", key=key)
            assert status == 200
            _, page = server.get("/api/v1/agent-ops/events?limit=200", user="carol", key=key)
            _, replay = server.get(f"/api/v1/agent-ops/replay?until={snap['cursor']}", user="carol", key=key)
            for body in (snap, page, replay):
                dump = json.dumps(body)
                assert "org-agent" not in dump and "agent-b" not in dump and "agent-a" not in dump
                assert stack.rooms["alice"] not in dump and "r-alice" not in dump
            assert snap["rooms"] and {r["room_id"] for r in snap["rooms"]} == {stack.rooms["bob"]}
            assert page["events"] and all(e["source"] == f"room:{stack.rooms['bob']}" for e in page["events"])
            assert not any(a["agent_id"] in ("org-agent", "agent-b") for a in snap["agents"])
        conn = http.client.HTTPConnection("127.0.0.1", server.srv.port, timeout=5)
        conn.request("GET", "/api/v1/agent-ops/stream", headers={"X-API-Key": "view-key",
                                                                  "Authorization": f"Bearer {TOKENS['carol']}"})
        resp = conn.getresponse()
        assert resp.status == 200
        seen = []
        while not any(f.get("event_type") == "message.sent" for f in seen):
            line = resp.fp.readline().decode().strip()
            if line.startswith("data:") and '"event_id"' in line:
                seen.append(json.loads(line[5:]))
        conn.close()
        assert all(f["source"] == f"room:{stack.rooms['bob']}" for f in seen)
    finally:
        server.stop()


def test_room_id_is_ignored_unless_the_scope_is_room(stack):
    ingest(stack.repo, 1, "tool.started", session_id="sa", agent_id="agent-a", room_id=stack.rooms["alice"],
           visibility_scope="org")
    bob = stack.users["bob"]
    snap = stack.svc.snapshot(stack.svc.viewer(bob), bob)
    assert stack.rooms["alice"] not in json.dumps(snap)
    page = stack.svc.events(stack.svc.viewer(bob), bob)["events"]
    assert [e["room_id"] for e in page if not e["source"].startswith("room:")] == [None]


def test_finished_task_is_not_drawn_as_an_accepted_delegation():
    base = dict(session_id="s1", task_id="t1", payload={"target_agent_id": "a2"})
    done = fold([ev(1, "delegation.accepted", status="completed", **base)])
    failed = fold([ev(1, "delegation.accepted", status="failed", **base)])
    live = fold([ev(1, "delegation.accepted", status="in_progress", **base)])
    assert [e["state"] for e in done["edges"]] == ["completed"]
    assert [e["state"] for e in failed["edges"]] == ["failed"]
    assert [e["state"] for e in live["edges"]] == ["accepted"]
    assert done["tasks"][0]["contributors"] == [] and live["tasks"][0]["contributors"] != []


def test_private_scope_is_visible_to_the_operator_only():
    private = ev(1, "tool.started", session_id="s1", visibility_scope="private")
    assert agent_ops.visible(private, Viewer(operator=True)) and not agent_ops.visible(private, Viewer())


# ------------------------------------------------------------------------- opaque server cursors (fix-2)
def _scrub_repo():
    repo = ControlPlaneRepo()
    ingest(repo, 1, "tool.started", session_id="s-alpha", agent_id="alpha", event_id="e1")
    ingest(repo, 2, "tool.started", session_id="s-bravo", agent_id="bravo", event_id="e2")
    ingest(repo, 3, "tool.started", session_id="s-charlie", agent_id="charlie", event_id="z3")
    return repo


def test_every_event_row_carries_the_server_cursor_and_scrubbing_with_it_is_exact():
    svc = AgentOpsService(_scrub_repo())
    viewer = Viewer(operator=True)
    rows = svc.events(viewer, None, None, 100)["events"]
    assert all(row["cursor"] == cursor_of(row) for row in rows)
    for index, row in enumerate(rows):
        snap = svc.replay(viewer, None, row["cursor"])
        assert sorted(a["agent_id"] for a in snap["agents"]) == sorted(r["agent_id"] for r in rows[:index + 1])


def test_stream_frames_and_events_route_carry_the_same_cursor_and_replay_accepts_it(stack):
    repo = _scrub_repo()
    server = Server(repo, stack.collab, agent_ops_config={"enabled": True})
    server.srv.agent_ops_poll_seconds = 0.05
    try:
        page = server.get("/api/v1/agent-ops/events?limit=100")[1]
        cursors = [e["cursor"] for e in page["events"]]
        assert len(cursors) == 3
        for index, cursor in enumerate(cursors):
            replay = server.get(f"/api/v1/agent-ops/replay?until={cursor}")[1]
            assert replay["counts"]["agents"] == index + 1
        conn = http.client.HTTPConnection("127.0.0.1", server.srv.port, timeout=5)
        conn.request("GET", "/api/v1/agent-ops/stream", headers={"X-API-Key": "view-key"})
        resp = conn.getresponse()
        frames = []
        while len(frames) < 3:
            line = resp.fp.readline().decode().strip()
            if line.startswith("data:") and '"event_id"' in line:
                frames.append(json.loads(line[5:]))
        conn.close()
        assert [f["cursor"] for f in frames] == cursors
    finally:
        server.stop()


# ------------------------------------------------------------------------------ ingest trust (fix-2)
def test_v1_ingest_cannot_forge_the_agent_ops_block():
    repo = ControlPlaneRepo()
    forged = {"_envelope": {"agent_ops": {"session_id": "forged", "sanitized_label": "/Users/bob/x",
                                          "provenance": "observed"}}}
    for version in (None, 1):
        body = {"entity_type": "lifecycle", "entity_id": "a1", "event_type": "tool.started",
                "payload": dict(forged), "source": "pi"}
        if version:
            body["schema_version"] = version
        IngestService(repo).ingest(body, OPERATOR)
    assert agent_ops.envelope_events(repo) == []
    for stream in repo._events.values():
        assert all("_envelope" not in e["payload"] for e in stream)


def test_a_second_key_cannot_preempt_a_genuine_event_identity():
    victim = {"kind": "agent", "id": "key:victim", "authenticated_by": "api_key"}
    attacker = {"kind": "agent", "id": "key:attacker", "authenticated_by": "api_key"}
    repo = ControlPlaneRepo()
    body = {"schema_version": 2, "event_id": "e1", "source": "pi", "source_sequence": 1, "entity_type": "lifecycle",
            "entity_id": "a1", "event_type": "tool.started", "visibility_scope": "org", "agent_id": "a1",
            "provenance": "observed", "session_id": "s1", "payload": {}}
    IngestService(repo).ingest({**body, "sanitized_label": "zzz victim label"}, victim)
    IngestService(repo).ingest({**body, "sanitized_label": "aaa attacker"}, attacker)
    events = agent_ops.envelope_events(repo)
    assert len(events) == 2 and len({cursor_of(e) for e in events}) == 2
    assert len({agent_ops._identity(e) for e in events}) == 2
    assert "key:victim" not in json.dumps(events) and "key:attacker" not in json.dumps(events)
    (agent,) = fold(events)["agents"]
    assert agent["label"] == "zzz victim label"          # the earlier-ingested genuine event is not displaced


# --------------------------------------------------------------------------------- stream cap (fix-2)
def test_concurrent_streams_are_capped_per_principal(stack):
    server = Server(stack.repo, stack.collab, agent_ops_config={"enabled": True, "streams_per_principal": 2})
    server.srv.agent_ops_poll_seconds = 0.05
    try:
        a1, r1 = stream_conn(server, "alice")
        a2, r2 = stream_conn(server, "alice")
        a3, r3 = stream_conn(server, "alice")
        assert (r1.status, r2.status, r3.status) == (200, 200, 503)
        b1, rb = stream_conn(server, "bob")                 # another principal is unaffected
        assert rb.status == 200
        for conn in (a1, a2, a3, b1):
            conn.close()
    finally:
        server.stop()


# ------------------------------------------------------------------------------ cursor validation (fix-4)
def _bad_cursors(repo):
    row = AgentOpsService(repo).events(Viewer(operator=True), None, None, 1)["events"][0]
    return {"old-format": f"{row['ingested_at']}~{row['event_id']}", "garbage": "garbage!!", "empty-parts": "~",
            "huge": "x" * 20000, "nul": "a\x00b", "bad-time": "2026-13-45T99:00:00.000000Z~pi~a1~~e1",
            "bad-actor": f"{row['ingested_at']}~pi~a1~nothex~e1"}


def test_cursor_validator_accepts_every_server_cursor_and_nothing_else():
    repo = _scrub_repo()
    for row in AgentOpsService(repo).events(Viewer(operator=True), None, None, 100)["events"]:
        assert agent_ops.valid_cursor(row["cursor"])
    assert not any(agent_ops.valid_cursor(c) for c in _bad_cursors(repo).values())


def test_events_and_replay_answer_400_for_an_invalid_cursor_and_stream_resets(stack):
    repo = _scrub_repo()
    server = Server(repo, stack.collab, agent_ops_config={"enabled": True})
    server.srv.agent_ops_poll_seconds = 0.05
    try:
        from urllib.parse import quote
        for name, cur in _bad_cursors(repo).items():
            assert server.get(f"/api/v1/agent-ops/events?cursor={quote(cur)}")[0] == 400, name
            status, body = server.get(f"/api/v1/agent-ops/replay?until={quote(cur)}")
            assert status == 400 and "cursor" in body["error"], name
        good = server.get("/api/v1/agent-ops/events?limit=100")[1]["events"][0]["cursor"]
        assert server.get(f"/api/v1/agent-ops/events?cursor={quote(good)}")[0] == 200
        snapshot_cursor = server.get("/api/v1/agent-ops/snapshot")[1]["cursor"]
        conn = http.client.HTTPConnection("127.0.0.1", server.srv.port, timeout=5)
        conn.request("GET", "/api/v1/agent-ops/stream",
                     headers={"X-API-Key": "view-key", "Last-Event-ID": _bad_cursors(repo)["old-format"]})
        resp = conn.getresponse()
        assert resp.status == 200
        lines = [resp.fp.readline().decode().strip() for _ in range(2)]
        assert lines[0] == "event: connected"
        assert json.loads(lines[1][5:]) == {"cursor": None, "reset": True}
        # starts from the current snapshot cursor: nothing already folded in is replayed
        ingest(repo, 9, "tool.started", session_id="s-delta", agent_id="delta", event_id="e9")
        frames = []
        while not frames:
            line = resp.fp.readline().decode().strip()
            if line.startswith("data:") and '"event_id"' in line:
                frames.append(json.loads(line[5:]))
        conn.close()
        assert frames[0]["event_id"] == "e9" and frames[0]["cursor"] > snapshot_cursor
    finally:
        server.stop()


# ------------------------------------------------------------------------------ stream cap config, open mode (fix-4)
def test_per_principal_stream_cap_defaults_to_four_and_is_configurable(stack):
    server = Server(stack.repo, stack.collab, agent_ops_config={"enabled": True})
    assert server.srv.agent_ops_streams_per_principal == 4
    server.stop()
    server = Server(stack.repo, stack.collab, agent_ops_config={"enabled": True, "streams_per_principal": 1})
    try:
        assert server.srv.agent_ops_streams_per_principal == 1
        a1, r1 = stream_conn(server, "alice")
        a2, r2 = stream_conn(server, "alice")
        assert (r1.status, r2.status) == (200, 503)
        a1.close()
        a2.close()
    finally:
        server.stop()


def test_open_mode_streams_share_one_bucket_whatever_the_api_key_header_says():
    srv = ControlPlaneServer(ControlPlaneRepo(), host="127.0.0.1", port=0, agent_ops_config={"enabled": True})
    thread = threading.Thread(target=srv.serve, daemon=True)
    thread.start()
    srv.wait_until_ready()
    srv.agent_ops_poll_seconds = 0.05
    conns = []
    try:
        statuses = []
        for n in range(6):                       # a fresh X-API-Key each time must not mint a fresh bucket
            conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
            conn.request("GET", "/api/v1/agent-ops/stream", headers={"X-API-Key": f"anything-{n}"})
            statuses.append(conn.getresponse().status)
            conns.append(conn)
        assert statuses == [200, 200, 200, 200, 503, 503]
    finally:
        for conn in conns:
            conn.close()
        srv.shutdown()
        thread.join(timeout=5)


def test_snapshot_rooms_carry_the_collab_room_name_only_when_readable(stack):
    alice = stack.users["alice"]
    ingest(stack.repo, 1, "tool.started", session_id="sa", agent_id="agent-a", room_id=stack.rooms["alice"],
           visibility_scope="room")
    snap = stack.svc.snapshot(stack.svc.viewer(alice), alice)
    assert [(r["room_id"], r["name"]) for r in snap["rooms"]] == [(stack.rooms["alice"], "r-alice")]
    assert stack.svc.replay(stack.svc.viewer(alice), alice)["rooms"][0]["name"] == "r-alice"
    # no collab identity: the room id is not readable, so no name is attached (null, never the real name)
    ingest(stack.repo, 2, "tool.started", session_id="sb", agent_id="agent-b", room_id="room-unknown", visibility_scope="room")
    op = AgentOpsService(stack.repo).snapshot(Viewer(operator=True, rooms=frozenset({"room-unknown"})))
    assert all(r["name"] is None for r in op["rooms"])


# ------------------------------------------------------------------------------ cursors the server issues are accepted (round 4)
def test_every_issued_cursor_at_max_field_lengths_is_valid_and_round_trips_over_http(stack):
    from urllib.parse import quote
    server = Server(stack.repo, stack.collab, agent_ops_config={"enabled": True})
    try:
        long_id = "h" * 160
        for hid, agent in (("ho-tab", "tab\tagent"), ("ho-tilde", "~" * 160), (long_id, "s" * 160),
                           ("ho-ok", "alice-agent")):
            server.srv.handoffs.create({"handoff_id": hid, "source_harness": "pi", "target_harness": "claude",
                                        "goal": "g", "source_agent_id": agent, "source_session_id": "x" * 160,
                                        "target_agent_id": "t" * 160, "target_session_id": "s2",
                                        "room_id": stack.rooms["alice"]}, {"id": "alice"})
        ingest(stack.repo, 1, "tool.started", event_id="e" * 128, source="s" * 32, agent_id="g" * 160,
               session_id="z" * 160)
        events = agent_ops.handoff_events(server.srv.handoffs) + agent_ops.envelope_events(stack.repo)
        assert len(events) >= 5
        for e in events:
            cursor = cursor_of(e)
            assert agent_ops.valid_cursor(cursor), cursor[:80]
        status, page = server.get("/api/v1/agent-ops/events?limit=100", key="op-key")
        assert status == 200 and page["events"]
        for row in page["events"]:
            assert row["cursor"] == cursor_of(row) and agent_ops.valid_cursor(row["cursor"])
            for path in ("events?cursor=", "replay?until="):
                status, _ = server.get(f"/api/v1/agent-ops/{path}{quote(row['cursor'], safe='')}", key="op-key")
                assert status == 200, (path, row["event_id"][:40], status)
    finally:
        server.stop()


# ------------------------------------------------------------------ lapsed guests fail closed (no org fallback)
def _read_stream(server, user, seconds=1.2):
    """Raw text of an agent-ops stream as ``user`` until ``seconds`` pass (the server keeps it open)."""
    import time
    conn = http.client.HTTPConnection("127.0.0.1", server.srv.port, timeout=0.3)
    conn.request("GET", "/api/v1/agent-ops/stream", headers={"X-API-Key": "view-key",
                                                              "Authorization": f"Bearer {TOKENS[user]}"})
    resp = conn.getresponse()
    assert resp.status == 200
    chunks, end = [], time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            line = resp.fp.readline()
        except OSError:
            continue
        if line:
            chunks.append(line.decode())
    conn.close()
    return "".join(chunks)


@pytest.mark.parametrize("how", ["revoked", "expired"])
def test_lapsed_guest_sees_nothing_on_every_route(stack, how):
    """A guest whose room access ends still holds a valid user token. They must not fall back to org-scoped
    visibility: no org agent, no room data, on snapshot, events, replay or stream."""
    import time
    bob = stack.users["bob"]
    expiry = time.time() + 1.0 if how == "expired" else None
    body = {"user_id": "carol", "role": "guest", **({"expires_at": expiry} if expiry else {})}
    stack.collab.add_member(bob, stack.rooms["bob"], body)
    stack.collab.post_message(bob, stack.rooms["bob"], {"text": "while a guest", "idempotency_key": "g0"})
    ingest(stack.repo, 1, "tool.started", session_id="so", agent_id="org-agent", visibility_scope="org")
    ingest(stack.repo, 2, "tool.started", session_id="sb", agent_id="agent-b", room_id=stack.rooms["bob"],
           visibility_scope="room")
    server = Server(stack.repo, stack.collab, agent_ops_config={"enabled": True})
    server.srv.agent_ops_poll_seconds = 0.05
    try:
        _, before = server.get("/api/v1/agent-ops/snapshot", user="carol")
        assert {r["room_id"] for r in before["rooms"]} == {stack.rooms["bob"]}      # live guest: room only
        assert "org-agent" not in json.dumps(before)
        if how == "revoked":
            stack.collab.remove_member(bob, stack.rooms["bob"], "carol")
        else:
            time.sleep(max(0.0, expiry - time.time()) + 0.2)
        assert stack.collab.readable_room_ids(stack.users["carol"]) == []            # access really is gone
        stack.collab.post_message(bob, stack.rooms["bob"], {"text": "AFTER LAPSE SECRETTEXT", "idempotency_key": "z"})
        for key in ("view-key", "op-key"):
            status, snap = server.get("/api/v1/agent-ops/snapshot", user="carol", key=key)
            assert status == 200
            _, page = server.get("/api/v1/agent-ops/events?limit=200", user="carol", key=key)
            _, replay = server.get(f"/api/v1/agent-ops/replay?until={snap['cursor']}", user="carol", key=key)
            for body in (snap, page, replay):
                dump = json.dumps(body)
                assert "org-agent" not in dump and "agent-b" not in dump and "SECRETTEXT" not in dump
                assert stack.rooms["bob"] not in dump and stack.rooms["alice"] not in dump
            assert snap["agents"] == [] and snap["rooms"] == [] and page["events"] == []
        text = _read_stream(server, "carol")
        assert "org-agent" not in text and "agent-b" not in text and "SECRETTEXT" not in text
        assert '"event_id"' not in text
    finally:
        server.stop()


def test_user_who_never_was_a_guest_keeps_org_visibility(stack):
    ingest(stack.repo, 1, "tool.started", session_id="so", agent_id="org-agent", visibility_scope="org")
    carol = stack.users["carol"]
    snap = stack.svc.snapshot(stack.svc.viewer(carol), carol)
    assert [a["agent_id"] for a in snap["agents"]] == ["org-agent"]


def test_same_api_key_different_sessions_stay_separate_nodes(stack):
    """Shared-key trust model: a key authenticates, the session id identifies. Same key, three sessions, three nodes."""
    ingest(stack.repo, 1, "tool.started", session_id="sess-1", agent_id="agent-one")
    ingest(stack.repo, 2, "tool.started", session_id="sess-2", agent_id="agent-two")
    ingest(stack.repo, 3, "tool.started", session_id="sess-3", agent_id="agent-one")   # same agent id, new session
    snap = stack.svc.snapshot(Viewer(operator=True))
    assert sorted(a["node_id"] for a in snap["agents"]) == ["sess-1", "sess-2", "sess-3"]
    assert {a["node_id"]: a["session_id"] for a in snap["agents"]} == {s: s for s in ("sess-1", "sess-2", "sess-3")}


# ------------------------------------------------- "lapsed guest" means the LATEST membership was a guest one (S3)
def test_guest_readded_as_contributor_is_no_longer_a_lapsed_guest(stack):
    """Rule: lapsed guest = the person's most recent room membership was a guest one. Carol was a guest, is re-added
    as a contributor (latest membership is non-guest) and then removed: she is NOT a lapsed guest, so she sees org
    scope again. A later guest add makes her one again."""
    bob, carol = stack.users["bob"], stack.users["carol"]
    ingest(stack.repo, 1, "tool.started", session_id="so", agent_id="org-agent", visibility_scope="org")

    def sees_org():
        return [a["agent_id"] for a in stack.svc.snapshot(stack.svc.viewer(carol), carol)["agents"]] == ["org-agent"]
    stack.collab.add_member(bob, stack.rooms["bob"], {"user_id": "carol", "role": "guest"})
    stack.collab.remove_member(bob, stack.rooms["bob"], "carol")
    assert not sees_org()                                                    # lapsed guest: nothing
    stack.collab.add_member(bob, stack.rooms["bob"], {"user_id": "carol", "role": "contributor"})
    stack.collab.remove_member(bob, stack.rooms["bob"], "carol")
    assert sees_org()                                                        # latest membership was non-guest
    stack.collab.add_member(bob, stack.rooms["bob"], {"user_id": "carol", "role": "guest"})
    stack.collab.remove_member(bob, stack.rooms["bob"], "carol")
    assert not sees_org()


def test_ever_guests_is_rebuilt_identically_from_the_log(stack):
    bob = stack.users["bob"]
    stack.collab.add_member(bob, stack.rooms["bob"], {"user_id": "carol", "role": "guest"})
    assert stack.collab._ever_guests == {"carol"}
    stack.collab.remove_member(bob, stack.rooms["bob"], "carol")
    stack.collab.add_member(bob, stack.rooms["bob"], {"user_id": "carol", "role": "contributor"})
    assert stack.collab._ever_guests == set()
    stack.collab.remove_member(bob, stack.rooms["bob"], "carol")
    stack.collab.add_member(bob, stack.rooms["bob"], {"user_id": "carol", "role": "guest"})
    fresh = CollabService(stack.collab.auth, sink=None)
    fresh.hydrate(stack.collab._log)
    assert fresh._ever_guests == stack.collab._ever_guests == {"carol"}


def test_agent_of_an_owner_with_a_room_of_her_own_is_not_denied_for_having_once_been_a_guest(stack):
    """alice owns a room and was once a guest in bob's: her enrolled agent (no grants) sees what a normal agent sees."""
    alice, bob = stack.users["alice"], stack.users["bob"]
    stack.collab.add_member(bob, stack.rooms["bob"], {"user_id": "alice", "role": "guest"})
    stack.collab.remove_member(bob, stack.rooms["bob"], "alice")
    info = stack.collab.connect_agent(alice, {"harness_type": "claude-code"})
    agent = stack.collab.authenticate(f"Agent {info['credential']}", info["device_id"])
    ingest(stack.repo, 1, "tool.started", session_id="so", agent_id="org-agent", visibility_scope="org")
    viewer = stack.svc.viewer(agent)
    assert not viewer.guest_only
    assert [a["agent_id"] for a in stack.svc.snapshot(viewer, agent)["agents"]] == ["org-agent"]


def test_agent_of_a_lapsed_guest_with_no_room_of_her_own_still_sees_nothing(stack):
    bob, carol = stack.users["bob"], stack.users["carol"]
    stack.collab.add_member(bob, stack.rooms["bob"], {"user_id": "carol", "role": "guest"})
    stack.collab.remove_member(bob, stack.rooms["bob"], "carol")
    info = stack.collab.connect_agent(carol, {"harness_type": "claude-code"})
    agent = stack.collab.authenticate(f"Agent {info['credential']}", info["device_id"])
    assert stack.svc.viewer(agent).guest_only


# ------------------------------------------------- an external agent is treated like a guest (S2)
def _get_raw(server, path, authorization, device, key):
    conn = http.client.HTTPConnection("127.0.0.1", server.srv.port, timeout=5)
    conn.request("GET", path, headers={"X-API-Key": key, "Authorization": authorization, "X-Herdr-Device": device})
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    return resp.status, json.loads(raw) if raw else None


def _enroll_external(stack, room):
    inv = stack.collab.create_external_invite(stack.users["alice"], room, {"label": "Dana"})
    out = stack.collab.redeem_join_token({"token": inv["code"]})
    return out, stack.collab.authenticate(f"Agent {out['credential']}", out["device_id"])


def test_external_agent_sees_only_its_room_chat_with_either_key(stack):
    import time
    out, _ = _enroll_external(stack, stack.rooms["alice"])
    ingest(stack.repo, 1, "tool.started", session_id="so", agent_id="org-agent", visibility_scope="org")
    ingest(stack.repo, 2, "tool.started", session_id="sp", agent_id="priv-agent", visibility_scope="private")
    ingest(stack.repo, 3, "tool.started", session_id="sa", agent_id="agent-a", room_id=stack.rooms["alice"],
           visibility_scope="room")
    server = Server(stack.repo, stack.collab, agent_ops_config={"enabled": True})
    try:
        auth, dev = f"Agent {out['credential']}", out["device_id"]
        for key in ("view-key", "op-key"):
            status, snap = _get_raw(server, "/api/v1/agent-ops/snapshot", auth, dev, key)
            assert status == 200, key
            _, page = _get_raw(server, "/api/v1/agent-ops/events?limit=200", auth, dev, key)
            _, replay = _get_raw(server, f"/api/v1/agent-ops/replay?until={snap['cursor']}", auth, dev, key)
            for body in (snap, page, replay):
                dump = json.dumps(body)
                assert "org-agent" not in dump and "priv-agent" not in dump and "agent-a" not in dump, key
            assert snap["agents"] == []
            assert all(e["source"].startswith("room:") for e in page["events"]), key
        conn = http.client.HTTPConnection("127.0.0.1", server.srv.port, timeout=0.3)
        conn.request("GET", "/api/v1/agent-ops/stream",
                     headers={"X-API-Key": "op-key", "Authorization": auth, "X-Herdr-Device": dev})
        resp = conn.getresponse()
        assert resp.status == 200
        text, end = "", time.monotonic() + 1.0
        while time.monotonic() < end:
            try:
                line = resp.fp.readline()
            except OSError:
                continue
            text += line.decode() if line else ""
        conn.close()
        assert "org-agent" not in text and "priv-agent" not in text and "agent-a" not in text
    finally:
        server.stop()


def test_external_agent_whose_room_binding_lapsed_sees_nothing(stack):
    out, agent = _enroll_external(stack, stack.rooms["alice"])
    ingest(stack.repo, 1, "tool.started", session_id="so", agent_id="org-agent", visibility_scope="org")
    assert stack.svc.viewer(agent).guest_only
    stack.collab._now = lambda: out["expires_at"] + 10          # the binding's hard expiry has passed
    viewer = stack.svc.viewer(agent)
    assert viewer.rooms == frozenset() and viewer.guest_only
    assert stack.svc.snapshot(viewer, agent)["agents"] == []
    assert stack.svc.events(viewer, agent)["events"] == []


def test_service_follows_the_live_server_collab(stack):
    """`collab` may be swapped after startup: the service must read the same object `_agent_ops_caller` does."""
    server = Server(stack.repo, None, agent_ops_config={"enabled": True})
    try:
        assert server.srv.agent_ops.collab is None
        server.srv.collab = stack.collab
        assert server.srv.agent_ops.collab is stack.collab
    finally:
        server.stop()
