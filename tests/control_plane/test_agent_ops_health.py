"""Agent Operations slices 08+09: context health, drift heuristics, host/fleet overview, reconnect."""
import copy
import hashlib
import json
import random

import pytest

from herdr_engineering.control_plane import agent_ops
from herdr_engineering.control_plane.agent_ops import AgentOpsService, Viewer, fold
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
    return type("Stack", (), dict(repo=repo, collab=collab, users=users, rooms=rooms,
                                  svc=AgentOpsService(repo, collab)))


def agent(snap, node_id):
    (found,) = [a for a in snap["agents"] if a["node_id"] == node_id]
    return found


def ctx(n, used, limit, agent_id="a1", **kw):
    return ev(n, "context.metric", agent=agent_id, session_id=kw.pop("session_id", agent_id),
              payload={"context_used": used, "context_limit": limit, "context_source": "pi"}, **kw)


# ------------------------------------------------------------------------------------------ context health
def test_context_levels_and_thresholds():
    cases = [(10, "ok"), (69, "ok"), (70, "elevated"), (89, "elevated"), (90, "high"), (99, "high"),
             (100, "critical"), (150, "critical")]
    for used, level in cases:
        health = agent(fold([ctx(1, used, 100)]), "a1")["context_health"]
        assert health["level"] == level and health["fraction"] == used / 100 and health["measured"] is True
        assert health["source"] == "pi"


def test_unknown_context_stays_unknown_never_zero():
    for events in ([ev(1, "tool.started")], [ev(1, "context.metric", payload={"context_limit": 100})],
                   [ev(1, "context.metric", payload={"context_used": 5})],
                   [ev(1, "context.metric", payload={"context_used": 5, "context_limit": 0})]):
        health = agent(fold(events), "a1")["context_health"]
        assert health["fraction"] is None and health["level"] == "unknown" and health["measured"] is False


def test_aggregate_averages_only_measured_agents():
    events = [ctx(1, 50, 100, "a1"), ctx(2, 90, 100, "a2"), ev(3, "tool.started", agent="a3", session_id="a3")]
    snap = fold(events + [ev(4 + i, "room.joined", agent=x, session_id=x, room_id="r1", visibility_scope="org")
                          for i, x in enumerate(("a1", "a2", "a3"))])
    fleet = snap["health"]["context"]
    assert (fleet["measured_count"], fleet["total"], fleet["average_fraction"], fleet["level"]) == (2, 3, 0.7, "elevated")
    (room,) = snap["rooms"]
    assert room["context_health"] == fleet
    nothing = fold([ev(1, "tool.started")])["health"]["context"]
    assert nothing["average_fraction"] is None and nothing["level"] == "unknown" and nothing["measured_count"] == 0


def test_old_keys_stay_stable():
    (a,) = fold([ctx(1, 10, 100)])["agents"]
    assert a["context"] == {"used": 10, "limit": 100, "source": "pi"} and a["drift_warnings"] == 0


# ------------------------------------------------------------------------------------------------- drift
def test_reported_drift_warning_has_provenance_evidence_and_heuristic_flag():
    events = [ev(1, "drift.warning", session_id="s1", task_id="t1", provenance="inferred", sanitized_label="/x/y.py"),
              ev(2, "drift.warning", session_id="s1", task_id="t1", provenance="derived")]
    snap = fold(events)
    a = agent(snap, "s1")
    assert a["drift_warnings"] == 2
    (d,) = a["drift"]
    assert d["kind"] == "reported_drift_warning" and d["provenance"] == "inferred"
    assert d["evidence_event_ids"] == ["e1", "e2"] and d["heuristic"] is True and "not a proven" in d["note"]
    assert snap["tasks"][0]["drift"] == [d]
    assert "/x/y.py" not in json.dumps(snap["agents"][0]["drift"])
    only_derived = fold(events[1:])["agents"][0]["drift"][0]
    assert only_derived["provenance"] == "derived"
    assert fold([ev(1, "tool.started")])["agents"][0]["drift"] == []


def test_repeated_tool_failures_need_a_streak_and_reset_on_success():
    fails = [ev(n, "tool.failed", session_id="s1", task_id="t1") for n in (1, 2)]
    assert agent(fold(fails), "s1")["drift"] == []
    streak = fails + [ev(3, "tool.failed", session_id="s1", task_id="t1")]
    (d,) = agent(fold(streak), "s1")["drift"]
    assert d["kind"] == "repeated_tool_failures" and d["provenance"] == "derived"
    assert d["evidence_event_ids"] == ["e1", "e2", "e3"] and d["task_id"] == "t1"
    recovered = streak + [ev(4, "tool.ended", session_id="s1")]
    assert agent(fold(recovered), "s1")["drift"] == []


# --------------------------------------------------------------------------------------------- fleet
def fleet_timeline():
    return [
        ev(1, "session.started", agent="a1", session_id="s1", host_id="h1"),
        ev(2, "tool.started", agent="a1", session_id="s1", host_id="h1"),
        ev(3, "session.started", agent="a2", session_id="s2", host_id="h1"),
        ev(4, "session.started", agent="a3", session_id="s3", host_id="h2"),
        ev(5, "agent.disconnected", agent=None, host_id="h1"),
    ]


def host(snap, host_id):
    (found,) = [h for h in snap["hosts"] if h["host_id"] == host_id]
    return found


def test_hosts_from_observed_events_with_unknown_connectivity():
    snap = fold(fleet_timeline()[:4])
    h1 = host(snap, "h1")
    assert h1["agents"] == ["s1", "s2"] and h1["connected"] is None and h1["state"] == "unknown"
    assert h1["reconnects"] == 0 and h1["last_seen"] == "2026-11-12T10:00:03.000000Z"
    assert snap["counts"]["hosts"] == 2 and fold([ev(1, "tool.started")])["hosts"] == []


def test_host_disconnect_marks_agents_disconnected_not_removed_and_reconnect_restores():
    down = fold(fleet_timeline())
    assert host(down, "h1")["connected"] is False and host(down, "h1")["state"] == "disconnected"
    assert agent(down, "s1")["state"] == "disconnected" and agent(down, "s2")["state"] == "disconnected"
    assert agent(down, "s3")["state"] == "idle" and host(down, "h2")["connected"] is None
    assert down["counts"]["agents"] == 3
    up = fold(fleet_timeline() + [ev(6, "agent.reconnected", agent=None, host_id="h1")])
    h1 = host(up, "h1")
    assert (h1["connected"], h1["state"], h1["reconnects"]) == (True, "connected", 1)
    assert agent(up, "s1")["state"] == "working" and agent(up, "s2")["state"] == "idle"
    assert agent(up, "s1")["last_event_at"] == "2026-11-12T10:00:02.000000Z"   # history intact
    assert not any(k.startswith("_") for k in agent(up, "s1"))


def test_agent_level_disconnect_leaves_host_connectivity_alone():
    snap = fold([ev(1, "session.started", session_id="s1", host_id="h1"),
                 ev(2, "agent.disconnected", session_id="s1", host_id="h1")])
    assert agent(snap, "s1")["state"] == "disconnected" and host(snap, "h1")["connected"] is None
    back = fold([ev(1, "session.started", session_id="s1", host_id="h1"),
                 ev(2, "agent.disconnected", session_id="s1", host_id="h1"),
                 ev(3, "agent.reconnected", session_id="s1", host_id="h1")])
    assert agent(back, "s1")["state"] == "idle"


def test_events_after_host_disconnect_do_not_revive_but_restore_latest_state():
    events = fleet_timeline() + [ev(6, "tool.ended", agent="a1", session_id="s1", host_id="h1"),
                                 ev(7, "tool.started", agent="a2", session_id="s2", host_id="h1")]
    assert agent(fold(events), "s1")["state"] == "disconnected"
    up = fold(events + [ev(8, "agent.reconnected", agent=None, host_id="h1")])
    assert agent(up, "s1")["state"] == "idle" and agent(up, "s2")["state"] == "working"


def test_duplicate_and_out_of_order_disconnect_reconnect_are_deterministic():
    events = fleet_timeline() + [ev(6, "agent.reconnected", agent=None, host_id="h1"),
                                 ev(7, "agent.disconnected", agent=None, host_id="h1"),
                                 ev(8, "agent.reconnected", agent=None, host_id="h1")]
    expected = fold(events)
    assert host(expected, "h1")["reconnects"] == 2 and host(expected, "h1")["state"] == "connected"
    for seed in range(25):
        shuffled = events + events[3:7]
        random.Random(seed).shuffle(shuffled)
        assert fold(shuffled) == expected
    # a repeated disconnect/reconnect (distinct event ids) is idempotent
    noisy = fleet_timeline() + [ev(6, "agent.disconnected", agent=None, host_id="h1"),
                                ev(7, "agent.reconnected", agent=None, host_id="h1"),
                                ev(8, "agent.reconnected", agent=None, host_id="h1")]
    assert host(fold(noisy), "h1")["reconnects"] == 1


def test_replay_up_to_cursor_reproduces_historical_hosts():
    events = fleet_timeline() + [ev(6, "agent.reconnected", agent=None, host_id="h1")]
    mid = fold(events, agent_ops.cursor_of(events[4]))
    assert host(mid, "h1")["state"] == "disconnected" and agent(mid, "s1")["state"] == "disconnected"
    early = fold(events, agent_ops.cursor_of(events[3]))
    assert host(early, "h1")["state"] == "unknown" and agent(early, "s1")["state"] == "working"
    assert fold(events, agent_ops.cursor_of(events[4])) == mid


# ----------------------------------------------------------------------------------- crash mid-handoff
def handoff_events(status_after=None):
    base = [ev(1, "session.started", agent="a1", session_id="s1", host_id="h1"),
            ev(2, "session.started", agent="a2", session_id="s2", host_id="h2"),
            ev(3, "handoff.state", agent="a1", session_id="s1", handoff_id="ho1", status="accepted", task_id="t1",
               source="handoff:ho1", payload={"target_agent_id": "a2", "target_session_id": "s2"})]
    return base


def handoff_edge(snap):
    (edge,) = [e for e in snap["edges"] if e["kind"] == "handoff"]
    return edge


def test_agent_crash_mid_handoff_is_stalled_not_accepted():
    assert handoff_edge(fold(handoff_events()))["state"] == "accepted"
    snap = fold(handoff_events() + [ev(4, "agent.disconnected", agent="a2", session_id="s2", host_id="h2")])
    edge = handoff_edge(snap)
    assert edge["state"] == "stalled" and edge["interrupted_by"] == "e4"
    kinds = {a["node_id"]: [d["kind"] for d in a["drift"]] for a in snap["agents"]}
    assert kinds["s1"] == kinds["s2"] == ["handoff_stalled"]
    assert snap["tasks"][0]["drift"] and all(d["heuristic"] for d in snap["tasks"][0]["drift"])


def test_session_end_mid_handoff_is_failed_and_host_down_stalls_it():
    failed = fold(handoff_events() + [ev(4, "session.ended", agent="a1", session_id="s1", host_id="h1")])
    assert handoff_edge(failed)["state"] == "failed"
    stalled = fold(handoff_events() + [ev(4, "agent.disconnected", agent=None, host_id="h2")])
    assert handoff_edge(stalled)["state"] == "stalled" and agent(stalled, "s2")["state"] == "disconnected"


def test_completed_handoff_is_not_rewritten_by_a_later_crash():
    done = handoff_events() + [ev(4, "handoff.state", agent="a1", session_id="s1", handoff_id="ho1",
                                  status="completed", source="handoff:ho1",
                                  payload={"target_agent_id": "a2", "target_session_id": "s2"}),
                               ev(5, "agent.disconnected", agent="a2", session_id="s2", host_id="h2")]
    assert handoff_edge(fold(done))["state"] == "completed"


# ------------------------------------------------------------------------------------- authorization
def ingest(repo, n, event_type, **fields):
    body = {"schema_version": 2, "event_id": f"in-{n}", "source": "pi", "source_sequence": n,
            "entity_type": "lifecycle", "entity_id": "a1", "event_type": event_type,
            "occurred_at": f"2026-11-12T10:00:{n:02d}Z", "visibility_scope": "org",
            "agent_id": "a1", "provenance": "observed", "payload": {}, **fields}
    IngestService(repo).ingest(body, OPERATOR)


def test_host_and_fleet_info_do_not_leak_across_rooms(stack):
    ingest(stack.repo, 1, "session.started", session_id="sb", agent_id="agent-b", host_id="host-secret",
           room_id=stack.rooms["bob"], visibility_scope="room",
           payload={"context_used": 99, "context_limit": 100})
    ingest(stack.repo, 2, "context.metric", session_id="sb", agent_id="agent-b", host_id="host-secret",
           room_id=stack.rooms["bob"], visibility_scope="room",
           payload={"context_used": 99, "context_limit": 100})
    ingest(stack.repo, 3, "drift.warning", session_id="sb", agent_id="agent-b", host_id="host-secret",
           room_id=stack.rooms["bob"], visibility_scope="room")
    alice, bob = stack.users["alice"], stack.users["bob"]
    snap = stack.svc.snapshot(stack.svc.viewer(alice), alice)
    assert snap["hosts"] == [] and snap["counts"]["hosts"] == 0 and snap["health"]["context"]["total"] == 0
    assert "host-secret" not in json.dumps(snap) and "agent-b" not in json.dumps(snap)
    own = stack.svc.snapshot(stack.svc.viewer(bob), bob)
    assert [h["host_id"] for h in own["hosts"]] == ["host-secret"]
    assert own["health"]["context"]["measured_count"] == 1
    assert stack.svc.snapshot(stack.svc.viewer(None, operator=True))["hosts"] == []


def test_http_ingested_host_events_fold_end_to_end():
    repo = ControlPlaneRepo()
    ingest(repo, 1, "session.started", session_id="s1", host_id="h1")
    ingest(repo, 2, "agent.disconnected", agent_id=None, entity_id="h1", host_id="h1")
    snap = AgentOpsService(repo).snapshot(Viewer(operator=True))
    assert host(snap, "h1")["state"] == "disconnected" and agent(snap, "s1")["state"] == "disconnected"
    assert copy.deepcopy(snap) == AgentOpsService(repo).snapshot(Viewer(operator=True))
