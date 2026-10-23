"""Acceptance scenario 6 (Herdr side): replay after restart and dropped events, duplicate events, expired token, stale
lease, unauthorized room traversal and guest execution attempts. Fail closed, no fake progress, no leaked information.

Everything runs against REAL control-plane processes on loopback (tests/e2e/harness.py), configured through generated
YAML. Restart/replay uses a throwaway PostgreSQL (tests/e2e/pg.py) because the in-memory repo forgets everything on a
crash by design. Every negative assertion is paired with a positive control (same caller / same route succeeds in the
allowed case), and every refusal is followed by a check that no state moved.

One test waits ~95 s of REAL time (marked `slow` in its name): invite TTL (min 60 s), steering proposal / delivery TTL
(min 30 s) and review heartbeat staleness (90 s) are all enforced by the server's own clock in a separate process, so
they cannot be shortened or monkeypatched; a single shared wait covers all of them.
"""
from __future__ import annotations

import json
import time
from urllib.parse import quote

import pytest

from tests.e2e import harness, pg
from tests.e2e.harness import Plane, sha40

VOLATILE = {"age_seconds", "last_seen_at", "server_time", "status", "freshness_window_seconds"}


# ------------------------------------------------------------------------------------------------ fixtures
@pytest.fixture(scope="module")
def postgres(tmp_path_factory, acceptance=None):
    cfg = harness.load_acceptance()
    bins = pg.find_bins(cfg)
    if bins is None:
        pytest.skip("no PostgreSQL server binaries under acceptance.yaml postgres.bin_dirs")
    try:
        import asyncpg  # noqa: F401
    except ImportError:
        pytest.fail("asyncpg is a declared dependency (pyproject.toml) and is missing from this python")
    db = pg.ThrowawayPostgres(bins, tmp_path_factory.mktemp("pg")).start()
    yield db
    db.close()


def _plane(tmp, name, **kw) -> Plane:
    cfg = harness.load_acceptance()
    return Plane(tmp, name=name, request_timeout=float(cfg["request_timeout_seconds"]),
                 startup_timeout=float(cfg["startup_timeout_seconds"]), **kw)


def _pinned(env: dict) -> dict:
    # a re-delivery must be byte-identical, so the timestamp may not follow the wall clock
    return {**env, "occurred_at": "2026-01-01T00:00:00.000Z"}


def node_put(plane: Plane, key: str, source: str, seq: int, nid: str, room: str | None = None, *, state="running",
             event_id: str | None = None, scope: str | None = None, attrs: dict | None = None):
    payload = {"node_type": nid.split(":")[0], "node_id": nid, "state": state, **({"attrs": attrs} if attrs else {})}
    ids = {"room_id": room} if room else {}
    return plane.ingest(key, _pinned(plane.envelope(source=source, seq=seq, entity_id=nid, event_type="node.upsert",
                                                    payload=payload, scope=scope or ("room" if room else "org"),
                                                    event_id=event_id or f"{source}-ev{seq}", **ids)))


def edge_put(plane, key, source, seq, etype, a, b, room=None, scope=None):
    ids = {"room_id": room} if room else {}
    return plane.ingest(key, _pinned(plane.envelope(source=source, seq=seq, entity_id=a, event_type="edge.add",
                                                    payload={"edge_type": etype, "from_id": a, "to_id": b},
                                                    scope=scope or ("room" if room else "org"), event_id=f"{source}-ev{seq}", **ids)))


def strip(o):
    if isinstance(o, dict):
        return {k: strip(v) for k, v in o.items() if k not in VOLATILE}
    if isinstance(o, list):
        return [strip(v) for v in o]
    return o


def graph_snapshot(plane: Plane, ids: list[str]) -> dict:
    out = {}
    for nid in ids:
        r = plane.node(nid)
        out[nid] = (r.status, strip(r.body))
    return out


# ================================================================================================ A. restart / replay
class TestRestartReplay:
    """Two ingest keys (two 'hosts'), duplicates and a dropped sequence; SIGKILL; restart on the same database."""

    def test_replay_after_kill_9_restores_graph_and_source_state_and_invents_nothing(self, tmp_path, postgres):
        plane = _plane(tmp_path, "durable", keys={"hosta": "ingest", "hostb": "ingest"}, users=["alice", "bob"],
                       pg_dsn=postgres.dsn).start()
        try:
            _, room = plane.make_room("alice", "durable", "r")
            plane.add_member("alice", room, "bob", "viewer")
            # host A: seq 0, 1, (2 dropped), 3 ; a duplicate of seq 1 by event_id
            assert node_put(plane, "hosta", "hosta", 0, "agent:lead", state="running").status == 200
            assert node_put(plane, "hosta", "hosta", 1, "attempt:lead.g1", state="running").status == 200
            dup = node_put(plane, "hosta", "hosta", 1, "attempt:lead.g1", state="running")
            assert dup.status == 200 and dup["entity"]["envelope"]["duplicate"] is True
            assert node_put(plane, "hosta", "hosta", 3, "agent:child", state="running").status == 200
            assert edge_put(plane, "hosta", "hosta", 4, "spawn", "agent:lead", "agent:child").status == 200
            # host B: independent source, no gap
            assert node_put(plane, "hostb", "hostb", 0, "agent:other", state="blocked").status == 200

            src_before = plane.sources()
            assert src_before["hosta"]["last_sequence"] == 4
            assert [(g["from"], g["to"]) for g in src_before["hosta"]["gaps"]] == [(2, 2)]
            assert src_before["hostb"]["gaps"] == [] and src_before["hostb"]["last_sequence"] == 0
            ids = ["agent:lead", "attempt:lead.g1", "agent:child", "agent:other"]
            graph_before = graph_snapshot(plane, ids)
            assert all(s == 200 for s, _ in graph_before.values())
            edges_before = plane.edges("agent:lead")
            assert ("spawn", "agent:lead", "agent:child") in edges_before
            members_before = plane.c("GET", f"/rooms/{room}", user="alice").body

            plane.kill()                       # SIGKILL: no shutdown path runs
            assert not plane.alive()
            plane.start()                      # same database, new process, new port

            src_after = plane.sources()
            assert strip(src_after) == strip(src_before)
            assert [(g["from"], g["to"]) for g in src_after["hosta"]["gaps"]] == [(2, 2)], "the gap must survive the crash"
            assert graph_snapshot(plane, ids) == graph_before
            assert plane.edges("agent:lead") == edges_before
            # NO fake progress: nothing that was never sent exists (including the dropped seq's would-be effects)
            for never in ("agent:ghost", "attempt:lead.g2", "agent:dropped"):
                assert plane.node(never).status == 404
            assert plane.node("agent:child").body["node"]["state"] == "running"
            # collaboration state replays too, and rights did not widen
            assert plane.c("GET", f"/rooms/{room}", user="alice").status == 200
            assert plane.c("GET", f"/rooms/{room}", user="bob").status == 200
            assert plane.c("POST", f"/rooms/{room}/members", {"user_id": "mallory", "role": "owner"}, user="bob").status in (403, 404)
            assert plane.c("GET", f"/rooms/{room}", user="alice").body["name"] == members_before["name"]

            # re-delivery of everything already seen is acknowledged as duplicate and changes nothing
            for seq, nid in ((0, "agent:lead"), (1, "attempt:lead.g1")):
                r = node_put(plane, "hosta", "hosta", seq, nid, state="running")
                assert r.status == 200 and r["entity"]["envelope"]["duplicate"] is True, r.body
            assert strip(plane.sources()) == strip(src_before)
            # the late-arriving dropped event fills the gap; only then does the gap close and nothing else moves
            assert node_put(plane, "hosta", "hosta", 2, "agent:late", state="running").status == 200
            src_filled = plane.sources()
            assert src_filled["hosta"]["gaps"] == [] and src_filled["hosta"]["last_sequence"] == 4
            assert plane.node("agent:late").status == 200
            assert graph_snapshot(plane, ids) == graph_before
            # a continued stream works after restart
            assert node_put(plane, "hosta", "hosta", 5, "agent:next", state="running").status == 200
            assert plane.sources()["hosta"]["last_sequence"] == 5
        finally:
            plane.stop()

    def test_a_second_crash_restore_is_idempotent(self, tmp_path, postgres):
        plane = _plane(tmp_path, "twice", keys={"w": "ingest"}, pg_dsn=postgres.dsn).start()
        try:
            assert node_put(plane, "w", "twice", 0, "agent:t0").status == 200
            assert node_put(plane, "w", "twice", 2, "agent:t2").status == 200          # dropped seq 1
            snap = (strip(plane.sources()), graph_snapshot(plane, ["agent:t0", "agent:t2"]))
            for _ in range(2):
                plane.kill()
                plane.start()
                assert (strip(plane.sources()), graph_snapshot(plane, ["agent:t0", "agent:t2"])) == snap
            assert plane.node("agent:t1").status == 404
        finally:
            plane.stop()

    def test_unavailable_store_is_never_acknowledged_and_the_retry_lands_once(self, tmp_path):
        """Fail closed on the WRITE path: when the durable store is down the server must not say 200 for an event it
        did not keep (that would be fake progress the emitter could never replay)."""
        cfg = harness.load_acceptance()
        bins = pg.find_bins(cfg)
        if bins is None:
            pytest.skip("no PostgreSQL server binaries under acceptance.yaml postgres.bin_dirs")
        db = pg.ThrowawayPostgres(bins, tmp_path / "pgx").start()
        plane = _plane(tmp_path, "storedown", keys={"w": "ingest"}, pg_dsn=db.dsn).start()
        try:
            assert node_put(plane, "w", "sd", 0, "agent:before").status == 200
            db.stop()
            during = node_put(plane, "w", "sd", 1, "agent:during")
            assert during.status >= 500, f"event acknowledged while the durable store was down: {during.status} {during.body}"
            assert "postgres" not in json.dumps(during.body).lower() and "asyncpg" not in json.dumps(during.body).lower()
            db.start_existing()
            time.sleep(0.5)
            retry = node_put(plane, "w", "sd", 1, "agent:during")
            assert retry.status == 200 and retry["entity"]["envelope"]["duplicate"] is False
            again = node_put(plane, "w", "sd", 1, "agent:during")
            assert again["entity"]["envelope"]["duplicate"] is True
            assert plane.sources()["sd"]["last_sequence"] == 1 and plane.sources()["sd"]["gaps"] == []
        finally:
            plane.stop()
            db.close()


# ================================================================================================ shared cast
@pytest.fixture(scope="module")
def cast(tmp_path_factory, postgres):
    """alice owns rooms A and B (project p); bob contributor and carol viewer and dave guest in A; erin contributor in B;
    mallory in none. Lineage for both rooms is written by one scoped ingest key (`writer`)."""
    tmp = tmp_path_factory.mktemp("s6cast")
    plane = _plane(tmp, "cast", keys={"writer": "ingest", "other": "ingest"},
                   users=["alice", "bob", "carol", "dave", "erin", "mallory", "frank", "zed"],
                   scopes={"writer": {"rooms": ["*"], "missions": ["*"]}}, pg_dsn=postgres.dsn,
                   collab={"steering": {"enabled": True, "proposal_ttl_seconds": 30, "delivery_ttl_seconds": 30}})
    plane.start()
    plane.verify_features("alice")
    pj, A = plane.make_room("alice", "p", "A")
    B = plane.c("POST", f"/projects/{pj}/rooms", {"name": "B"}, user="alice")["room_id"]
    for u, role in (("bob", "contributor"), ("carol", "viewer"), ("dave", "guest"), ("frank", "contributor")):
        plane.add_member("alice", A, u, role)
    plane.add_member("alice", B, "erin", "contributor")

    class Cast:
        pass
    c = Cast()
    c.plane, c.A, c.B, c.project = plane, A, B, pj
    c.seq = 0

    def put(nid, room, **kw):
        c.seq += 1
        r = node_put(plane, "writer", "castw", c.seq, nid, room, **kw)
        assert r.status == 200, r.body
        return r
    c.put = put
    put("agent:a-main", A)
    put("attempt:a-1", A)
    put("agent:b-secret", B)
    put("attempt:b-1", B)
    yield c
    plane.stop()


# ================================================================================================ D. traversal
ROOM_PATHS = ["", "/state", "/events", "/flow", "/attention", "/comments", "/steering", "/progress", "/tasks",
              "/agents", "/workspaces", "/missions", "/xlinks", "/analytics", "/consultations", "/externals",
              "/forks?node_id=agent%3Ab-secret"]


class TestRoomTraversal:
    def test_positive_controls_each_reader_sees_exactly_their_own_room(self, cast):
        p = cast.plane
        for who, room, own, other in (("bob", cast.A, "agent:a-main", "agent:b-secret"),
                                      ("erin", cast.B, "agent:b-secret", "agent:a-main"),
                                      ("alice", cast.A, "agent:a-main", None), ("alice", cast.B, "agent:b-secret", None)):
            assert p.c("GET", f"/rooms/{room}", user=who).status == 200
            assert p.c("GET", f"/graph/nodes/{quote(own, safe='')}", user=who).status == 200
        listing = p.c("GET", f"/graph/nodes?node_type=agent&room_id={cast.A}", user="bob")
        assert listing.status == 200 and [n["id"] for n in listing["nodes"]] == ["agent:a-main"]

    def test_member_of_one_room_gets_one_indistinguishable_404_for_every_route_of_the_other(self, cast):
        p = cast.plane
        missing_room = "room_01zzzzzzzzzzzzzzzzzzzzzzzz"
        for path in ROOM_PATHS:
            hidden = p.c("GET", f"/rooms/{cast.B}{path}", user="bob")
            absent = p.c("GET", f"/rooms/{missing_room}{path}", user="bob")
            assert hidden.status == 404 and (hidden.status, hidden.body) == (absent.status, absent.body), path
            sneaky = p.c("GET", f"/rooms/{cast.A}{path}".replace("/forks?node_id=agent%3Ab-secret", "/forks?node_id=agent%3Ab-secret"), user="bob")
            assert "b-secret" not in json.dumps(sneaky.body), f"room A route {path} leaked room B content"
        # the same routes DO answer for the room's own member (so the 404s are authorization, not a missing route)
        for path in ROOM_PATHS[:12]:
            assert p.c("GET", f"/rooms/{cast.B}{path}", user="erin").status == 200, path

    def test_guessing_node_ids_cannot_reach_other_rooms_graph(self, cast):
        p = cast.plane
        q = lambda nid: quote(nid, safe="")  # noqa: E731
        for route in ("/graph/nodes/{}", "/graph/nodes/{}/neighborhood?depth=2", "/graph/nodes/{}/subgraph?depth=1"):
            secret = p.c("GET", route.format(q("agent:b-secret")), user="bob")
            nothing = p.c("GET", route.format(q("agent:never-existed")), user="bob")
            assert secret.status == 404 and (secret.status, secret.body) == (nothing.status, nothing.body), route
            assert p.c("GET", route.format(q("agent:b-secret")), user="erin").status == 200, route
        edge = "/graph/edge?edge_type=spawn&from_id=agent%3Ab-secret&to_id=attempt%3Ab-1"
        assert p.c("GET", edge, user="bob").status == 404
        # listing is always one room's view; asking for the other room, or for none, reveals nothing
        assert p.c("GET", f"/graph/nodes?node_type=agent&room_id={cast.B}", user="bob").status == 404
        assert p.c("GET", f"/graph/nodes?node_type=attempt&room_id={cast.B}", user="mallory").status == 404
        assert p.c("GET", "/graph/nodes?node_type=agent", user="bob").status == 400
        # the keyed fleet API: room scope is operator-only, an ingest/viewer key cannot read it either
        assert p.node("agent:b-secret", key="viewer").status == 404
        assert p.node("agent:b-secret", key="other").status == 404
        assert p.node("agent:b-secret", key="operator").status == 200

    def test_cross_link_to_another_rooms_node_is_a_404_not_a_leak(self, cast):
        p = cast.plane
        body = {"from": {"kind": "node", "node_id": "agent:a-main"}, "to": {"kind": "node", "node_id": "agent:b-secret"},
                "idempotency_key": "x-link-1"}
        hidden = p.c("POST", f"/rooms/{cast.A}/xlinks", body, user="bob")
        absent = p.c("POST", f"/rooms/{cast.A}/xlinks", {**body, "to": {"kind": "node", "node_id": "agent:nope"}, "idempotency_key": "x-link-2"}, user="bob")
        assert hidden.status == 404 and (hidden.status, hidden.body) == (absent.status, absent.body)
        # positive control: two visible nodes get PAST the visibility gate (refused later for a different, honest reason)
        ok_gate = p.c("POST", f"/rooms/{cast.A}/xlinks", {"from": {"kind": "node", "node_id": "agent:a-main"},
                                                       "to": {"kind": "node", "node_id": "attempt:a-1"}, "idempotency_key": "x-link-3"}, user="bob")
        assert ok_gate.status == 422 and ok_gate.get("code") == "no_workspace", ok_gate.body
        assert p.c("GET", f"/rooms/{cast.A}/xlinks", user="bob")["links"] == []

    def test_comment_and_steering_on_another_rooms_node_are_refused_without_effect(self, cast):
        p = cast.plane
        mark = len(p.c("GET", f"/rooms/{cast.B}/comments", user="erin")["comments"])
        for room, nid in ((cast.B, "agent:b-secret"), (cast.A, "agent:b-secret")):
            r = p.c("POST", f"/rooms/{room}/comments", {"anchor": {"kind": "node", "node_id": nid}, "text": "intrusion",
                                                        "idempotency_key": "intr-" + room[-4:]}, user="bob")
            assert r.status == 404, (room, r.body)
        r = p.c("POST", f"/rooms/{cast.A}/comments", {"anchor": {"kind": "node", "node_id": "agent:a-main"}, "text": "legit",
                                                      "idempotency_key": "legit-1"}, user="bob")
        assert r.status == 201, r.body                                           # positive control
        assert len(p.c("GET", f"/rooms/{cast.B}/comments", user="erin")["comments"]) == mark

    def test_event_stream_of_another_room_is_refused_before_streaming(self, cast):
        p = cast.plane
        r = p.c("GET", f"/rooms/{cast.B}/events/stream", user="bob")
        assert r.status == 404 and "text/event-stream" not in r.headers.get("content-type", "")
        assert p.c("GET", f"/rooms/{cast.B}/events/stream", user="mallory").status == 404

    def test_non_members_guests_and_agents_see_no_lineage(self, cast):
        p = cast.plane
        for who in ("mallory", "dave"):                                 # outsider and guest
            assert p.c("GET", f"/graph/nodes/{quote('agent:a-main', safe='')}", user=who).status == 404
            assert p.c("GET", f"/graph/nodes?node_type=agent&room_id={cast.A}", user=who).status == 404
            assert p.c("GET", f"/rooms/{cast.A}/flow", user=who).status in (403, 404)
        assert p.c("GET", f"/graph/nodes/{quote('agent:a-main', safe='')}", user="bob").status == 200
        assert p.c("GET", f"/graph/nodes/{quote('agent:a-main', safe='')}", user="carol").status == 200   # viewer reads

    def test_unauthenticated_and_forged_credentials_get_the_same_401(self, cast):
        p = cast.plane
        bodies = []
        for kw in ({}, {"user": "not-a-token"}, {"agent": ("hca_" + "a" * 43, "dev-x")}, {"headers": {"Authorization": "Basic Zm9vOmJhcg=="}}):
            r = p.c("GET", f"/rooms/{cast.A}", **kw)
            assert r.status == 401, (kw, r.status)
            bodies.append(r.body)
        assert all(b == bodies[0] for b in bodies)

    def test_membership_revoked_mid_session_takes_effect_on_the_next_read(self, cast):
        p = cast.plane
        node = f"/graph/nodes/{quote('agent:a-main', safe='')}"
        sse_like = f"/rooms/{cast.A}/state"
        assert p.c("GET", node, user="frank").status == 200 and p.c("GET", sse_like, user="frank").status == 200
        before_seq = p.sources()["castw"]["last_sequence"]
        assert p.c("POST", f"/rooms/{cast.A}/members/frank/remove", {}, user="alice").status == 200
        for path in (node, sse_like, f"/rooms/{cast.A}", f"/rooms/{cast.A}/flow", f"/graph/nodes?node_type=agent&room_id={cast.A}"):
            assert p.c("GET", path, user="frank").status in (403, 404), path
        assert p.c("GET", node, user="bob").status == 200                              # others unaffected
        assert p.sources()["castw"]["last_sequence"] == before_seq

    def test_role_downgrade_to_guest_takes_effect_on_the_next_read(self, cast):
        p = cast.plane
        node = f"/graph/nodes/{quote('attempt:a-1', safe='')}"
        assert p.c("GET", node, user="carol").status == 200
        # the member list is the authority: removing and re-adding as a guest is the downgrade path
        assert p.c("POST", f"/rooms/{cast.A}/members/carol/remove", {}, user="alice").status == 200
        p.add_member("alice", cast.A, "carol", "guest")
        assert p.c("GET", node, user="carol").status == 404
        assert p.c("GET", node, user="bob").status == 200


# ================================================================================================ E. guest execution
class TestGuestExecution:
    @pytest.fixture(scope="class")
    def agents(self, cast):
        """weak: read-only agent owned by bob; strong: read/post/instruct/execute agent owned by bob. Both approved by alice."""
        p = cast.plane
        out = {}
        for name, actions in (("weak", ["read"]), ("strong", ["read", "post", "instruct", "execute"])):
            info = p.c("POST", "/agents/connect", {"harness_type": "claude-code", "name": name + "-bot"}, user="bob")
            assert info.status == 201, info.body
            g = p.c("POST", f"/rooms/{cast.A}/agents", {"agent_id": info["agent_id"], "actions": actions}, user="bob")
            assert g.status == 201, g.body
            if g["status"] == "pending":
                d = p.c("POST", f"/rooms/{cast.A}/agent-requests/{g['request_id']}/decide", {"decision": "approve"}, user="alice")
                assert d.status == 200, d.body
            out[name] = (info["credential"], info["device_id"], info["agent_id"])
        return out

    def test_positive_controls_contributor_and_strong_agent_can_do_what_guest_and_weak_agent_cannot(self, cast, agents):
        p = cast.plane
        t = p.c("POST", f"/rooms/{cast.A}/tasks", {"target_agent_id": agents["strong"][2], "text": "run the unit tests",
                                                    "idempotency_key": "t-ok-1"}, user="bob")
        assert t.status == 201, t.body
        m = p.c("POST", f"/rooms/{cast.A}/messages", {"text": "hello room", "idempotency_key": "m-ok-1"}, user="bob")
        assert m.status in (200, 201), m.body
        m2 = p.c("POST", f"/rooms/{cast.A}/messages", {"text": "agent hello", "idempotency_key": "m-ok-2"},
                 agent=agents["strong"][:2])
        assert m2.status in (200, 201), m2.body

    @pytest.fixture(scope="class")
    def deleg_target(self, cast):
        """An external agent invited WITH proposals: the only kind of agent a delegation can address."""
        p = cast.plane
        inv = p.c("POST", f"/rooms/{cast.A}/externals/invites", {"label": "proposer", "proposals": True}, user="alice")
        assert inv.status == 201, inv.body
        out = p.c("POST", "/agents/join", {"token": inv["code"]})
        assert out.status == 201, out.body
        return out["agent_id"]

    def _actions(self, cast, agents, deleg_target, tag, ask="alice"):
        """Every execution-flavoured action, with a body valid enough that the ALLOWED caller succeeds."""
        p, room = cast.plane, cast.A
        fence = p.c("GET", f"/rooms/{room}/steering/target?node_id=attempt%3Aa-1", user="alice")["fence"]
        third = p.c("POST", "/agents/connect", {"harness_type": "claude-code", "name": "third-" + tag}, user="bob")["agent_id"]
        return {
            "task": ("POST", f"/rooms/{room}/tasks", {"target_agent_id": agents["strong"][2], "text": "run tests " + tag, "idempotency_key": "t-" + tag}),
            "message": ("POST", f"/rooms/{room}/messages", {"text": "do it " + tag, "idempotency_key": "m-" + tag}),
            "steer": ("POST", f"/rooms/{room}/steering", {"action": "pause", "node_id": "attempt:a-1", "expected_fence": fence, "idempotency_key": "s-" + tag}),
            "confirm": ("POST", f"/rooms/{room}/steering/steer_nope/confirm", {"digest": "d", "expected_fence": fence}),
            "delegation": ("POST", f"/rooms/{room}/delegations", {"target_agent_id": deleg_target, "text": "propose " + tag, "idempotency_key": "d-" + tag,
                                                                      "scope": {"questions": [{"id": "q1", "text": "why did it retry?"}]},
                                                                      "budget": {}, "output_kinds": ["analysis"]}),
            "comment": ("POST", f"/rooms/{room}/comments", {"anchor": {"kind": "node", "node_id": "attempt:a-1"}, "text": "note " + tag, "idempotency_key": "c-" + tag}),
            "consult": ("POST", f"/rooms/{room}/consultations", {"target": {"kind": "user", "id": ask}, "subject": {"kind": "node", "node_id": "attempt:a-1"},
                                                                  "question": "q " + tag, "idempotency_key": "q-" + tag}),
            "grant-agent": ("POST", f"/rooms/{room}/agents", {"agent_id": third, "actions": ["read"]}),
            "workspace-link": ("POST", f"/rooms/{room}/workspaces", {"host": "e2e-host", "workspace_id": "ws-" + tag, "grants": ["status"]}),
            "member": ("POST", f"/rooms/{room}/members", {"user_id": "zed", "role": "viewer"}),
        }

    def test_guest_cannot_post_task_steer_confirm_delegate_or_authorize_a_terminal(self, cast, agents, deleg_target):
        p, room = cast.plane, cast.A
        # POSITIVE CONTROLS: the owner (everything) and a contributor (task, message, steer, comment, consult) succeed with the
        # very same bodies the guest is refused with
        owner_ok = {k: p.c(m, path, b, user="alice") for k, (m, path, b) in self._actions(cast, agents, deleg_target, "own", ask="bob").items() if k not in ("confirm", "grant-agent")}
        assert {k: r.status for k, r in owner_ok.items()} == {k: r.status for k, r in owner_ok.items() if r.status in (200, 201)}, \
            {k: (r.status, r.body) for k, r in owner_ok.items() if r.status not in (200, 201)}
        contrib = {k: p.c(m, path, b, user="bob") for k, (m, path, b) in self._actions(cast, agents, deleg_target, "ctb").items()
                   if k in ("task", "message", "steer", "comment", "consult", "grant-agent")}
        assert all(r.status in (200, 201) for r in contrib.values()), {k: (r.status, r.body) for k, r in contrib.items()}
        # snapshot AFTER the positives, then the refused attempts
        tasks_before = p.c("GET", f"/rooms/{room}/tasks", user="alice")["tasks"]
        steer_before = p.c("GET", f"/rooms/{room}/steering", user="alice")["steering"]
        comments_before = p.c("GET", f"/rooms/{room}/comments", user="alice")["comments"]
        msgs_before = p.c("GET", f"/rooms/{room}/events", user="alice")["events"]
        links_before = p.c("GET", f"/rooms/{room}/workspaces", user="alice")["links"]
        src_before = p.sources()
        for who in ("dave", "mallory"):
            for name, (method, path, body) in self._actions(cast, agents, deleg_target, "bad" + who).items():
                r = p.c(method, path, body, user=who)
                assert r.status in (403, 404), (who, name, r.status, r.body)
        assert p.c("GET", f"/rooms/{room}/tasks", user="alice")["tasks"] == tasks_before
        assert p.c("GET", f"/rooms/{room}/steering", user="alice")["steering"] == steer_before
        assert p.c("GET", f"/rooms/{room}/comments", user="alice")["comments"] == comments_before
        assert p.c("GET", f"/rooms/{room}/events", user="alice")["events"] == msgs_before, "a refused attempt wrote to the room log"
        assert p.c("GET", f"/rooms/{room}/workspaces", user="alice")["links"] == links_before
        assert strip(p.sources()) == strip(src_before)

    def test_a_read_only_agent_cannot_instruct_post_or_execute(self, cast, agents):
        p = cast.plane
        weak = agents["weak"][:2]
        tasks_before = p.c("GET", f"/rooms/{cast.A}/tasks", user="alice")["tasks"]
        assert p.c("GET", f"/rooms/{cast.A}/events", agent=weak).status == 200                      # positive: it may read
        refused = [
            p.c("POST", f"/rooms/{cast.A}/tasks", {"target_agent_id": agents["strong"][2], "text": "x", "idempotency_key": "w-t"}, agent=weak),
            p.c("POST", f"/rooms/{cast.A}/messages", {"text": "x", "idempotency_key": "w-m"}, agent=weak),
            p.c("POST", f"/rooms/{cast.A}/steering", {"action": "pause", "node_id": "attempt:a-1", "expected_fence": "x", "idempotency_key": "w-s"}, agent=weak),
            p.c("POST", f"/rooms/{cast.A}/comments", {"anchor": {"kind": "node", "node_id": "attempt:a-1"}, "text": "x", "idempotency_key": "w-c"}, agent=weak),
        ]
        assert [r.status for r in refused] == [403, 403, 403, 403] or all(r.status in (403, 404) for r in refused), [r.body for r in refused]
        assert p.c("GET", f"/rooms/{cast.A}/tasks", user="alice")["tasks"] == tasks_before
        # an agent never reaches another room it was not granted
        assert p.c("GET", f"/rooms/{cast.B}/events", agent=weak).status in (403, 404)
        assert p.c("GET", f"/rooms/{cast.B}/events", agent=agents["strong"][:2]).status in (403, 404)

    def test_terminal_and_workspace_access_is_not_implied_by_room_access(self, cast):
        p = cast.plane
        lk = p.c("POST", f"/rooms/{cast.A}/workspaces", {"host": "e2e-host", "workspace_id": "ws-1", "grants": ["status"]}, user="alice")
        assert lk.status == 201, lk.body
        link = lk["link_id"]
        assert p.c("POST", f"/rooms/{cast.A}/workspaces/{link}/authorize", {"action": "status"}, user="bob").status == 200   # control
        for who in ("alice", "bob", "carol", "dave", "mallory"):
            r = p.c("POST", f"/rooms/{cast.A}/workspaces/{link}/authorize", {"action": "terminal"}, user=who)
            assert r.status in (403, 404), (who, r.status, r.body)
        assert p.c("POST", f"/rooms/{cast.A}/workspaces/{link}/authorize", {"action": "status"}, user="dave").status == 403
        # the link id is room-bound: B's member cannot use A's link
        assert p.c("POST", f"/rooms/{cast.B}/workspaces/{link}/authorize", {"action": "status"}, user="erin").status == 404

    def test_forged_identity_fields_are_rejected_not_obeyed(self, cast):
        p = cast.plane
        r = p.c("POST", f"/rooms/{cast.A}/messages", {"text": "x", "idempotency_key": "forge-1", "user_id": "alice", "role": "owner"}, user="dave")
        assert r.status in (400, 403, 404)
        r = p.c("POST", f"/rooms/{cast.A}/messages", {"text": "x", "idempotency_key": "forge-2", "user_id": "alice"}, user="bob")
        assert r.status == 400                                                        # even a member cannot claim another identity


# ================================================================================================ B. credentials that die at once
class TestDeadCredentials:
    def _enroll(self, cast, label, **kw):
        p = cast.plane
        inv = p.c("POST", f"/rooms/{cast.A}/externals/invites", {"label": label, "lineage": True, **kw}, user="alice")
        assert inv.status == 201, inv.body
        out = p.c("POST", "/agents/join", {"token": inv["code"]})
        assert out.status == 201, out.body
        return inv, out, (out["credential"], out["device_id"])

    def test_single_use_invite_second_redeem_is_the_guessed_code_401(self, cast):
        p = cast.plane
        inv, out, _ = self._enroll(cast, "once")
        again = p.c("POST", "/agents/join", {"token": inv["code"]})
        guess = p.c("POST", "/agents/join", {"token": "hcj_" + "x" * 32})
        assert again.status == 401 and (again.status, again.body) == (guess.status, guess.body)

    def test_revoked_agent_loses_heartbeat_and_lineage_at_once_with_the_same_401_and_graph_is_untouched(self, cast):
        p = cast.plane
        _, out, cred = self._enroll(cast, "revokee")
        node = "agent:" + out["agent_id"]
        ev = {"event_id": "rv-0", "source_sequence": 0, "event_type": "node.upsert",
              "payload": {"node_type": "agent", "node_id": node, "state": "running"}}
        assert p.c("POST", "/agents/external/heartbeat", {"status": "working"}, agent=cred).status == 200   # control
        assert p.c("POST", "/agents/external/lineage", ev, agent=cred).status == 200                       # control
        state_before = p.node(node).body["node"]["state"]
        assert p.c("POST", f"/rooms/{cast.A}/externals/{out['agent_id']}/revoke", {}, user="alice").status == 200
        junk = p.c("POST", "/agents/external/heartbeat", {"status": "idle"}, agent=("hca_" + "y" * 43, "dev-z"))
        hb = p.c("POST", "/agents/external/heartbeat", {"status": "idle"}, agent=cred)
        li = p.c("POST", "/agents/external/lineage", {**ev, "event_id": "rv-1", "source_sequence": 1, "event_type": "node.state",
                                                      "payload": {"node_id": node, "state": "done"}}, agent=cred)
        assert hb.status == li.status == junk.status == 401 and hb.body == li.body == junk.body
        assert p.node(node).body["node"]["state"] == state_before == "running", "a revoked agent moved the graph"
        # an ordinary agent credential cannot stand in for it either
        assert p.c("GET", f"/rooms/{cast.A}/events", agent=cred).status == 401

    def test_unknown_device_header_alone_is_not_enough(self, cast):
        p = cast.plane
        _, out, (cred, dev) = self._enroll(cast, "devbound")
        for dev_hdr in (None, "", "remote-" + "0" * 16):
            h = {"Authorization": "Agent " + cred, **({"X-Herdr-Device": dev_hdr} if dev_hdr is not None else {})}
            assert p.c("POST", "/agents/external/heartbeat", {"status": "idle"}, headers=h).status == 401
        assert p.c("POST", "/agents/external/heartbeat", {"status": "idle"}, agent=(cred, dev)).status == 200


# ================================================================================================ C/B. credentials and stale leases (one shared wait)
class TestExpiryAndStaleness:
    """Everything here is judged by the SERVER'S clock in a separate process. t0 set-up, one real sleep, then checks."""

    @pytest.fixture(scope="class")
    def timeline(self, cast):
        p = cast.plane
        t = type("T", (), {})()
        t.plane = p
        # --- external agent invites: one short-lived left unredeemed, one redeemed now (positive control), one for staleness
        short = p.c("POST", f"/rooms/{cast.A}/externals/invites", {"label": "short", "ttl_seconds": 60}, user="alice")
        assert short.status == 201, short.body
        t.expiring_code = short["code"]
        live = p.c("POST", f"/rooms/{cast.A}/externals/invites", {"label": "live", "ttl_seconds": 600, "lineage": True}, user="alice")
        redeemed = p.c("POST", "/agents/join", {"token": live["code"]})
        assert redeemed.status == 201, redeemed.body
        t.ext = (redeemed["credential"], redeemed["device_id"])
        t.ext_id = redeemed["agent_id"]
        assert p.c("POST", "/agents/external/heartbeat", {"status": "working", "activity": "t0"}, agent=t.ext).status == 200
        t.roster0 = {r["agent_id"]: r for r in p.c("GET", f"/rooms/{cast.A}/externals", user="alice")["externals"]}[t.ext_id]
        assert t.roster0["presence"]["state"] == "online"
        silent_inv = p.c("POST", f"/rooms/{cast.A}/externals/invites", {"label": "silent", "ttl_seconds": 600}, user="alice")
        silent = p.c("POST", "/agents/join", {"token": silent_inv["code"]})
        assert silent.status == 201
        t.silent_id = silent["agent_id"]
        t.silent = (silent["credential"], silent["device_id"])
        assert p.c("POST", "/agents/external/heartbeat", {"status": "working", "activity": "last words"}, agent=t.silent).status == 200
        # --- steering: node owned by `writer` in room A; three proposals
        p_node = "attempt:s6-steer"
        cast.put(p_node, cast.A)

        def propose(key, action="pause"):
            tg = p.c("GET", f"/rooms/{cast.A}/steering/target?node_id={quote(p_node, safe='')}", user="bob")
            assert tg.status == 200 and tg["steering_enabled"] is True, tg.body
            r = p.c("POST", f"/rooms/{cast.A}/steering", {"action": action, "node_id": p_node, "expected_fence": tg["fence"],
                                                         "idempotency_key": key}, user="bob")
            assert r.status == 201, r.body
            return r["steering"], tg["fence"]

        def confirm(st, fence):
            r = p.c("POST", f"/rooms/{cast.A}/steering/{st['steering_id']}/confirm", {"digest": st["digest"], "expected_fence": fence}, user="alice")
            return r
        t.node, t.propose, t.confirm = p_node, propose, confirm
        # positive control: propose -> confirm -> delivered to the pinned key -> ack, all in time
        st, fence = propose("s6-ok", "pause")
        assert confirm(st, fence).status == 200
        pend = p.get("/api/v1/steering/pending", key="writer")
        assert pend.status == 200 and [s["steering_id"] for s in pend["steering"]] == [st["steering_id"]], pend.body
        ack = p.post(f"/api/v1/steering/{st['steering_id']}/ack", {"outcome": "applied"}, key="writer")
        assert ack.status == 200 and ack["status"] == "acked", ack.body
        # (the acked pause did not change the node: only the emitter does that)
        assert p.node(p_node).body["node"]["state"] == "running"
        # leases that will go stale: a proposal nobody confirms, and one confirmed but never fetched/acked
        t.unconfirmed, t.fence_u = propose("s6-unconfirmed", "resume")
        t.undelivered, f2 = propose("s6-undelivered", "stop_after_step")
        assert confirm(t.undelivered, f2).status == 200
        # --- Plannotator review receipt (durable store): launched now, never heartbeated again
        rid = "rev_s6stale"
        reg = p.post("/api/v1/reviews", {"review_id": rid, "kind": "plan", "plan": "# plan\nstep", "instance_id": "inst-s6",
                                         "revision": "r1", "review_url": "http://127.0.0.1:9/review"}, key="operator",
                     headers={"Content-Type": "application/json"})
        assert reg.status == 201, reg.body
        t.review = rid
        t.review_digest = reg["plan_digest"]
        assert reg["stale"] is False
        t.t0 = time.time()
        time.sleep(max(0.0, 93.0))     # slow: > 90 s presence/heartbeat staleness, > 60 s invite TTL, > 30 s TTLs
        return t

    # ---- expired tokens
    def test_expired_invite_is_a_401_identical_to_a_guessed_code(self, cast, timeline):
        p = cast.plane
        expired = p.c("POST", "/agents/join", {"token": timeline.expiring_code})
        guessed = p.c("POST", "/agents/join", {"token": "hcj_" + "q" * 32})
        assert expired.status == 401 and (expired.status, expired.body) == (guessed.status, guessed.body)
        # positive control: a fresh invite redeems
        fresh = p.c("POST", f"/rooms/{cast.A}/externals/invites", {"label": "fresh"}, user="alice")
        assert p.c("POST", "/agents/join", {"token": fresh["code"]}).status == 201

    def test_credential_is_bound_to_its_device_and_a_wrong_device_is_the_same_401(self, cast, timeline):
        p = cast.plane
        cred, dev = timeline.ext
        good = p.c("POST", "/agents/external/heartbeat", {"status": "idle"}, agent=(cred, dev))
        assert good.status == 200
        wrong = p.c("POST", "/agents/external/heartbeat", {"status": "idle"}, agent=(cred, "other-device"))
        nodev = p.c("POST", "/agents/external/heartbeat", {"status": "idle"}, headers={"Authorization": "Agent " + cred})
        junk = p.c("POST", "/agents/external/heartbeat", {"status": "idle"}, agent=("hca_" + "z" * 43, dev))
        assert wrong.status == nodev.status == junk.status == 401
        assert wrong.body == nodev.body == junk.body

    # ---- stale presence
    def test_silent_external_agent_is_marked_stale_not_live_and_nothing_is_invented(self, cast, timeline):
        p = cast.plane
        rows = {r["agent_id"]: r for r in p.c("GET", f"/rooms/{cast.A}/externals", user="alice")["externals"]}
        silent = rows[timeline.silent_id]
        assert silent["presence"]["state"] == "stale", silent
        assert silent["presence"].get("activity", "last words") == "last words"        # last report kept, not rewritten
        assert silent["state"] == "active"                                           # enrolment is not presence
        # positive control: a heartbeat makes it online again
        assert p.c("POST", "/agents/external/heartbeat", {"status": "idle"}, agent=timeline.silent).status == 200
        rows = {r["agent_id"]: r for r in p.c("GET", f"/rooms/{cast.A}/externals", user="alice")["externals"]}
        assert rows[timeline.silent_id]["presence"]["state"] == "online"

    # ---- stale leases
    def test_unconfirmed_proposal_expired_and_cannot_be_confirmed_late(self, cast, timeline):
        p = cast.plane
        pend_before = p.get("/api/v1/steering/pending", key="writer")["steering"]
        r = timeline.confirm(timeline.unconfirmed, timeline.fence_u)
        assert r.status == 409 and r.get("code") == "expired", r.body
        view = [s for s in p.c("GET", f"/rooms/{cast.A}/steering", user="alice")["steering"] if s["steering_id"] == timeline.unconfirmed["steering_id"]]
        assert view and view[0]["status"] == "expired"
        assert p.get("/api/v1/steering/pending", key="writer")["steering"] == pend_before == []
        assert p.node(timeline.node).body["node"]["state"] == "running"

    def test_confirmed_but_undelivered_request_is_never_delivered_or_acked_after_its_window(self, cast, timeline):
        p = cast.plane
        assert p.get("/api/v1/steering/pending", key="writer")["steering"] == []
        r = p.post(f"/api/v1/steering/{timeline.undelivered['steering_id']}/ack", {"outcome": "applied"}, key="writer")
        assert r.status == 410 and r.get("code") == "expired", r.body
        row = [s for s in p.c("GET", f"/rooms/{cast.A}/steering", user="alice")["steering"] if s["steering_id"] == timeline.undelivered["steering_id"]][0]
        assert row["status"] == "expired" and not row.get("ack")
        # an unrelated key cannot ack it either, and learns nothing
        other = p.post(f"/api/v1/steering/{timeline.undelivered['steering_id']}/ack", {"outcome": "applied"}, key="other")
        nothing = p.post("/api/v1/steering/steer_does_not_exist/ack", {"outcome": "applied"}, key="other")
        assert other.status == 404 and (other.status, other.body) == (nothing.status, nothing.body)
        # positive control: a FRESH request in the same room still flows end to end after the wait
        st, fence = timeline.propose("s6-after", "resume")
        assert timeline.confirm(st, fence).status == 200
        assert [s["steering_id"] for s in p.get("/api/v1/steering/pending", key="writer")["steering"]] == [st["steering_id"]]

    def test_review_receipt_without_heartbeat_is_flagged_stale_and_fabricates_no_decision(self, cast, timeline):
        p = cast.plane
        rec = p.get(f"/api/v1/reviews/{timeline.review}", key="viewer")
        assert rec.status == 200 and rec["stale"] is True and rec["heartbeat_age_seconds"] >= 90
        assert rec["status"] == "awaiting_review" and rec["decision"] is None and rec["receipt"] is None
        hb = {"instance_id": "inst-s6", "revision": "r1", "plan_digest": timeline.review_digest}
        bad = [p.post(f"/api/v1/reviews/{timeline.review}/heartbeat", {**hb, "instance_id": "someone-else"}, key="operator"),
               p.post(f"/api/v1/reviews/{timeline.review}/heartbeat", {**hb, "plan_digest": "0" * 64}, key="operator"),
               p.post(f"/api/v1/reviews/{timeline.review}/result", {**hb, "revision": "r2", "result": {"decision": "approved"}}, key="operator")]
        assert all(b.status == 400 for b in bad), [b.body for b in bad]
        still = p.get(f"/api/v1/reviews/{timeline.review}", key="viewer")
        assert still["stale"] is True and still["decision"] is None and still["status"] == "awaiting_review"
        # a viewer / ingest key cannot report a heartbeat or an approval at all
        assert p.post(f"/api/v1/reviews/{timeline.review}/result", {**hb, "result": {"decision": "approved"}}, key="viewer").status == 403
        assert p.post(f"/api/v1/reviews/{timeline.review}/result", {**hb, "result": {"decision": "approved"}}, key="writer").status == 403
        assert p.get(f"/api/v1/reviews/{timeline.review}", key="viewer")["decision"] is None
        # positive control: the launcher's own bound heartbeat revives it
        ok = p.post(f"/api/v1/reviews/{timeline.review}/heartbeat", hb, key="operator")
        assert ok.status == 200 and ok["stale"] is False
