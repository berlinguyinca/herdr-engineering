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
TOKENS = {"alice": "tok-alice-0123456789", "bob": "tok-bob-0123456789abc"}


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
    for name in TOKENS:
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
