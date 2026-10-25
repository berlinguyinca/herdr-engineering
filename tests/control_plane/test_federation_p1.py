"""ADR-0008 phase P1: the peer principal, the per-principal visibility clamp and the reserved `peer:` namespace.

No network code exists in this phase; everything here drives the ingest path and the graph in process. The
refusals all raise the same PermissionError, so every guard test also has a positive control that the SAME
request succeeds when only that guard is out of the way (see the mutation notes in the PR).
"""
from __future__ import annotations

import pytest

from herdr_engineering.control_plane import federation as fed
from herdr_engineering.control_plane.envelope import normalize_envelope
from herdr_engineering.control_plane.graph import GraphProjection
from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.repo import ControlPlaneRepo

ROOM = "room_" + "0123456789abcdefghjkmnpqrs"            # the peer's one mirror room
OTHER_ROOM = "room_" + "sqrnmkjhgfedcba9876543210z"[:26]
PUB = "ab" * 32                                            # a raw public key as the YAML pins it (P2: hex)
FP = fed.key_fingerprint_of(PUB)                           # the FULL 64-hex fingerprint the principal carries
PEER = fed.peer_principal("acme", FP)
OP = {"kind": "operator", "id": "key:op", "authenticated_by": "api_key"}
OPEN = {"kind": "operator", "id": "open-listener", "authenticated_by": "open_listener"}
AGENT = {"kind": "agent", "id": "key:agent", "authenticated_by": "api_key"}
COLLAB = {"kind": "collab", "id": "collab:room:" + ROOM, "authenticated_by": "collab"}
EXTERNAL = {"kind": "external", "id": "agent_x", "authenticated_by": "collab"}
ORIGIN = {"server": "acme.example", "event_id": "o-1", "actor": "alice", "sequence": 7}


class _Anyone:
    """lineage_key_scopes test double: every non-peer key may address any room (the peer path never reads it)."""

    @staticmethod
    def get(_id, default=None):
        return {"rooms": ("*",), "missions": ("*",)}


def cfg(enabled=True, peer_enabled=True, room=ROOM, pub=PUB, peer="acme"):
    return {"enabled": enabled, "peers": {peer: {"enabled": peer_enabled, "public_key": pub, "mirror_room": room}}}


class World:
    def __init__(self, section=None, **svc_kw):
        self.section = cfg() if section is None else section
        self.repo = ControlPlaneRepo()
        self.svc = IngestService(self.repo, key_scopes=_Anyone(), **svc_kw)
        self.svc.peer_source = lambda: self.section
        self.n = 0

    def body(self, event_type, payload, scope="org", origin=ORIGIN, **extra):
        self.n += 1
        body = {"schema_version": 2, "event_id": f"e-{self.n}", "source": "mirror", "source_sequence": self.n,
                "entity_type": "lineage", "entity_id": payload.get("node_id") or payload.get("from_id"),
                "event_type": event_type, "payload": payload, "visibility_scope": scope,
                "occurred_at": "2026-10-25T10:00:00Z", **extra}
        if origin is not None:
            body["origin"] = origin
        return body

    def node(self, node_id, who=PEER, scope="org", **extra):
        return self.svc.ingest(self.body("node.upsert", {"node_type": node_id.split(":")[0], "node_id": node_id,
                                                         "label": "x"}, scope, origin=None if who is not PEER else ORIGIN,
                                         **extra), who)

    def edge(self, etype, a, b, who=PEER, attrs=None, scope="org"):
        p = {"edge_type": etype, "from_id": a, "to_id": b, **({"attrs": attrs} if attrs else {})}
        return self.svc.ingest(self.body("edge.add", p, scope, origin=ORIGIN if who is PEER else None), who)


def refused(fn, *a, **kw):
    with pytest.raises(PermissionError):
        fn(*a, **kw)


@pytest.fixture()
def w():
    return World()


def stored_meta(world, node_id):
    rows = world.repo.stream("lineage", node_id)
    assert len(rows) == 1
    return rows[0], rows[0]["payload"]["_envelope"]


# ===================================================================== (a) the clamp
@pytest.mark.parametrize("asked", ["org", "private", "mission", "room", "bogus", None])
def test_a_peer_event_is_rewritten_to_room_in_the_mirror_room_whatever_it_asked(w, asked):
    w.node("peer:acme.task-1", scope=asked) if asked else w.svc.ingest(
        {k: v for k, v in w.body("node.upsert", {"node_type": "peer", "node_id": "peer:acme.task-1"}).items()
         if k != "visibility_scope"}, PEER)
    ev, meta = stored_meta(w, "peer:acme.task-1")
    assert ev["visibility_scope"] == "room" and ev["room_id"] == ROOM
    assert meta.get("claimed_visibility_scope") == asked


def test_a_peer_cannot_choose_its_room_or_any_local_id(w):
    w.node("peer:acme.t", room_id=OTHER_ROOM, mission_id="m-9", workspace_id="ws-1", org_id="default",
           project_id="p", task_id="t", agent_id="a")
    ev, meta = stored_meta(w, "peer:acme.t")
    assert ev["room_id"] == ROOM
    assert all(ev.get(k) is None for k in ("mission_id", "workspace_id", "org_id", "project_id", "task_id", "agent_id"))
    assert meta["claimed_ids"]["room_id"] == OTHER_ROOM and meta["claimed_ids"]["mission_id"] == "m-9"


def test_origin_is_kept_only_as_claimed_provenance(w):
    w.node("peer:acme.t")
    ev, meta = stored_meta(w, "peer:acme.t")
    assert meta["claimed_origin"] == {"origin_server": "acme.example", "origin_actor": "alice",
                                      "origin_event_id": "o-1", "origin_sequence": 7}
    assert ev["actor"]["kind"] == "peer" and ev["actor"]["id"] == PEER["id"] and "alice" not in str(ev["actor"])


@pytest.mark.parametrize("origin", [None, {}, {"server": "s"}, {"event_id": "e"}, {"server": "s", "event_id": "e", "x": 1},
                                    {"server": "s", "event_id": 5}, {"server": "s", "event_id": "e", "sequence": -1}])
def test_a_peer_event_without_valid_origin_is_refused_and_stores_nothing(w, origin):
    with pytest.raises(ValueError):
        w.svc.ingest(w.body("node.upsert", {"node_type": "peer", "node_id": "peer:acme.t"}, origin=origin), PEER)
    assert w.repo.stream("lineage", "peer:acme.t") == []


def test_a_replayed_peer_event_is_a_duplicate_not_a_conflict(w):
    body = w.body("node.upsert", {"node_type": "peer", "node_id": "peer:acme.t", "label": "x"})
    assert w.svc.ingest(body, PEER)["envelope"]["duplicate"] is False
    assert w.svc.ingest(body, PEER)["envelope"]["duplicate"] is True


def test_the_clamp_runs_in_normalize_so_it_cannot_be_forgotten_by_a_caller():
    body = {"schema_version": 2, "event_id": "e", "source": "mirror", "source_sequence": 0, "visibility_scope": "org",
            "origin": ORIGIN}
    refused(normalize_envelope, body, PEER, "2026-10-25T00:00:00Z")                          # no clamp passed
    refused(normalize_envelope, body, PEER, "2026-10-25T00:00:00Z",
            clamp=fed.Clamp("other", "room", ROOM))                                          # a clamp of another kind
    refused(normalize_envelope, {k: v for k, v in body.items() if k != "schema_version"}, PEER,
            "2026-10-25T00:00:00Z", clamp=fed.clamp_for_principal(PEER, bind_room_id=ROOM))  # v1 body
    env = normalize_envelope(body, PEER, "2026-10-25T00:00:00Z", clamp=fed.clamp_for_principal(PEER, bind_room_id=ROOM))
    assert env["visibility_scope"] == "room" and env["room_id"] == ROOM


def test_the_clamp_api_p3_will_use():
    assert fed.clamp_for_principal(AGENT, bind_room_id=None) is None          # no clamp for an ordinary key
    assert fed.clamp_for_principal(OP, bind_room_id=ROOM) is None
    assert fed.clamp_for_principal(PEER, bind_room_id=ROOM) == fed.Clamp("peer", "room", ROOM)
    for bad in (None, ""):
        refused(fed.clamp_for_principal, PEER, bind_room_id=bad)              # never a pass-through
    assert fed.CLAMP_SCOPES == {"peer": "room"}


def test_an_ordinary_key_is_not_clamped(w):
    w.svc.ingest(w.body("node.upsert", {"node_type": "task", "node_id": "task:t", "label": "x"}, scope="org",
                        origin=None), AGENT)
    ev = w.repo.stream("lineage", "task:t")[0]
    assert ev["visibility_scope"] == "org" and "claimed_origin" not in ev["payload"]["_envelope"]


# ===================================================================== (b) the peer principal
@pytest.mark.parametrize("bad", [
    {**PEER, "authenticated_by": "api_key"}, {**PEER, "id": "peer:acme"}, {**PEER, "id": "key:abcdef012345"},
    {**PEER, "id": "peer:ac.me:" + "ab" * 6}, {**PEER, "kind": "operator"}, {**PEER, "kind": "agent"}])
def test_a_malformed_or_forged_peer_actor_is_refused(w, bad):
    refused(w.node, "peer:acme.t", who=bad) if bad["kind"] == "peer" else None
    if bad["kind"] != "peer":
        # a non-peer kind wearing a peer id is just a local principal: it still cannot write the reserved namespace
        refused(w.node, "peer:acme.t", who=bad)


def test_peer_user_never_ingests(w):
    refused(w.node, "peer:acme.t", who={"kind": "peer-user", "id": "peer:acme:" + "ab" * 6, "authenticated_by": "peer_key"})


@pytest.mark.parametrize("entity_type,event_type", [("host", "host.heartbeat"), ("session", "session.started"),
                                                    ("mission", "mission.started"), ("task", "task.note"),
                                                    ("service", "service.registered"), ("note", "note.added")])
def test_a_peer_may_only_write_lineage(w, entity_type, event_type):
    body = {"schema_version": 2, "event_id": "e1", "source": "herdr", "source_sequence": 1, "entity_type": entity_type,
            "entity_id": "x", "event_type": event_type, "payload": {}, "origin": ORIGIN}
    refused(w.svc.ingest, body, PEER)
    assert w.repo.stream(entity_type, "x") == []


def test_a_peer_cannot_use_the_v1_path(w):
    refused(w.svc.ingest, {"entity_type": "task", "entity_id": "x", "event_type": "task.note", "payload": {}}, PEER)
    refused(w.svc.ingest, {"entity_type": "lineage", "entity_id": "peer:acme.t", "event_type": "node.upsert",
                           "payload": {"node_type": "peer", "node_id": "peer:acme.t"}, "schema_version": 1}, PEER)


def test_a_peer_is_checked_like_a_non_operator_in_both_lineage_checks(w):
    # (reserved-node hook set, so the operator carve-out is what would let a wrongly-trusted peer through)
    w.svc.reserved_node = lambda n: n.startswith("artifact:ext.")
    w.svc.service_node = lambda n: n.startswith("task:hcn-delegation.")
    ok = {"actor": PEER, "visibility_scope": "room", "room_id": ROOM}
    w.svc._check_lineage_scope(ok)
    w.svc._check_reserved_nodes(ok, "node.upsert", {"node_id": "peer:acme.t"})
    refused(w.svc._check_reserved_nodes, ok, "node.upsert", {"node_id": "task:hcn-delegation.1"})
    refused(w.svc._check_reserved_nodes, ok, "node.upsert", {"node_id": "artifact:ext.1"})
    refused(w.svc._check_reserved_nodes, ok, "edge.add", {"from_id": "artifact:ext.1", "to_id": "peer:acme.t"})
    # ...and the same writes pass for an authenticated operator, so the refusals above are the peer rule
    op = {"actor": OP, "visibility_scope": "room", "room_id": ROOM}
    w.svc._check_reserved_nodes(op, "node.upsert", {"node_id": "task:hcn-delegation.1"})


@pytest.mark.parametrize("scope,room", [("org", ROOM), ("private", ROOM), ("mission", ROOM), ("room", OTHER_ROOM),
                                        ("room", None), ("room", "*")])
def test_a_peer_must_be_in_room_scope_in_its_own_mirror_room(w, scope, room):
    refused(w.svc._check_lineage_scope, {"actor": PEER, "visibility_scope": scope, "room_id": room})
    w.svc._check_lineage_scope({"actor": PEER, "visibility_scope": "room", "room_id": ROOM})     # control


def test_a_wildcard_key_scope_never_opens_a_peer(w):
    """key_scopes is _Anyone here (every id is '*'): a peer is still limited to its YAML mirror room."""
    refused(w.svc._check_lineage_scope, {"actor": PEER, "visibility_scope": "room", "room_id": OTHER_ROOM})
    w.svc.key_scopes = {PEER["id"]: {"rooms": frozenset({"*"}), "missions": frozenset({"*"})}}
    refused(w.svc._check_lineage_scope, {"actor": PEER, "visibility_scope": "room", "room_id": OTHER_ROOM})


def test_peer_enabled_requires_federation_the_peer_and_the_pinned_key(w):
    w.node("peer:acme.ok")
    for section in (None, {}, cfg(enabled=False), cfg(peer_enabled=False), cfg(pub="cd" * 32), cfg(peer="other"),
                    cfg(enabled="yes"), {"enabled": True, "peers": {"acme": {"enabled": True}}}, cfg(room="*")):
        w.section = section
        refused(w.node, "peer:acme.later")
    w.section = cfg()
    w.node("peer:acme.later")                                              # control: back on, same request passes


def test_peer_config_is_hot_and_any_failure_fails_closed(w):
    w.node("peer:acme.a")
    def boom():
        raise RuntimeError("fleet config unreadable")
    w.svc.peer_source = boom
    refused(w.node, "peer:acme.b")
    w.svc.peer_source = None
    refused(w.node, "peer:acme.b")                                          # no federation wired at all
    w.svc.peer_source = lambda: cfg()
    w.node("peer:acme.b")


def test_removing_the_peer_in_yaml_refuses_its_next_event(w):
    w.node("peer:acme.a")
    w.section = {"enabled": True, "peers": {}}
    refused(w.node, "peer:acme.b")


# ===================================================================== YAML loader
def test_loader_defaults_to_off():
    assert fed.parse_federation(None) is fed.OFF and fed.OFF.enabled is False
    assert fed.parse_federation({}).enabled is False


@pytest.mark.parametrize("section", [
    "yes", [], {"enabled": "true"}, {"enabled": 1}, {"enabled": True, "surprise": 1}, {"peers": []},
    {"peers": {"acme": "x"}}, {"peers": {"*": {"enabled": True, "public_key": PUB, "mirror_room": ROOM}}},
    {"peers": {"a.b": {"enabled": True, "public_key": PUB, "mirror_room": ROOM}}},
    {"peers": {"a:b": {"enabled": True, "public_key": PUB, "mirror_room": ROOM}}},
    {"peers": {"Acme": {"enabled": True, "public_key": PUB, "mirror_room": ROOM}}},
    {"peers": {"acme": {"enabled": True, "public_key": PUB, "mirror_room": "*"}}},
    {"peers": {"acme": {"enabled": True, "public_key": PUB, "mirror_room": ["*"]}}},
    {"peers": {"acme": {"enabled": True, "public_key": PUB, "mirror_room": ROOM + "*"}}},
    {"peers": {"acme": {"enabled": True, "public_key": PUB, "mirror_room": "room_short"}}},
    {"peers": {"acme": {"enabled": True, "mirror_room": ROOM}}},                                       # no key at all
    {"peers": {"acme": {"enabled": True, "key_fingerprint": "zz", "mirror_room": ROOM}}},
    {"peers": {"acme": {"enabled": "on", "public_key": PUB, "mirror_room": ROOM}}},
    {"peers": {"acme": {"enabled": True, "public_key": PUB, "mirror_room": ROOM, "rooms": ["*"]}}},
    {"peers": {"acme": {"enabled": True, "public_key": PUB, "mirror_room": ROOM, "missions": ["*"]}}},
])
def test_loader_rejects_wildcards_and_anything_malformed(section):
    with pytest.raises(fed.FederationConfigError):
        fed.parse_federation(section)
    assert fed.policy_from_source(lambda: section) is fed.OFF                 # hot read: invalid means OFF


def test_loader_accepts_a_good_section_and_defaults_each_peer_off():
    pol = fed.parse_federation({"enabled": True, "peers": {"acme": {"key_fingerprint": FP, "mirror_room": ROOM}}})
    assert pol.enabled and pol.peer("acme").enabled is False and pol.peer("acme").mirror_room == ROOM
    assert pol.peer("nobody") is None


def test_api_keys_can_never_be_a_peer_kind():
    for role in ("peer", "peer-user", "external", "collab"):
        assert fed.kind_for_role(role) == "unknown"
    assert fed.kind_for_role("operator") == "operator" and fed.kind_for_role("agent") == "agent"


# ===================================================================== (c) the reserved namespace
@pytest.mark.parametrize("who", [OP, OPEN, AGENT, COLLAB, EXTERNAL,
                                 {"kind": "agent", "id": "peer:acme:" + "ab" * 6, "authenticated_by": "api_key"}])
def test_nobody_local_can_write_the_peer_namespace(who, w):
    refused(w.node, "peer:acme.t", who=who)
    refused(w.node, "peer:other.t", who=who)
    w.node("task:local", who=who)                                           # control: the same actor writes elsewhere
    assert w.repo.stream("lineage", "peer:acme.t") == []


@pytest.mark.parametrize("who", [OP, OPEN, AGENT, COLLAB])
def test_nobody_local_can_edge_into_or_out_of_the_peer_namespace(who, w):
    w.node("peer:acme.t")
    refused(w.edge, "dependency", "task:local", "peer:acme.t", who=who)
    refused(w.edge, "dependency", "peer:acme.t", "task:local", who=who)
    refused(w.edge, "cross_link", "task:a", "peer:acme.t", who=who)
    w.node("task:a", who=who)
    w.node("task:b", who=who)
    w.edge("dependency", "task:a", "task:b", who=who)                       # control


def test_the_guard_holds_without_a_collab_service_wired(w):
    assert w.svc.reserved_node is None and w.svc.service_node is None
    refused(w.node, "peer:acme.t", who=OP)


def test_a_peer_writes_only_inside_its_own_namespace_on_every_endpoint(w):
    w.node("peer:acme.a")
    w.node("peer:acme.b")
    w.edge("dependency", "peer:acme.a", "peer:acme.b")                      # control: inside, both ends
    for bad in ("task:local", "peer:other.x", "peer:ac.x", "peer:acmex.x", "peer:acme"):
        refused(w.node, bad)
    refused(w.edge, "dependency", "peer:acme.a", "task:local")
    refused(w.edge, "dependency", "task:local", "peer:acme.a")
    refused(w.edge, "dependency", "peer:acme.a", "peer:other.x")
    refused(w.edge, "supersession", "peer:acme.a", "task:local")


def test_a_peer_cannot_remove_or_state_a_local_node(w):
    for ev, p in (("node.state", {"node_id": "task:local", "state": "done"}),
                  ("edge.remove", {"edge_type": "dependency", "from_id": "peer:acme.a", "to_id": "task:local",
                                   "reason": "x"})):
        refused(w.svc.ingest, w.body(ev, p), PEER)


def test_the_prefix_cannot_be_dodged_by_case(w):
    from herdr_engineering.control_plane.graph import validate_lineage
    with pytest.raises(ValueError):
        validate_lineage("node.upsert", {"node_type": "peer", "node_id": "Peer:acme.t"})
    assert fed.is_peer_node("PEER:acme.t") and fed.is_peer_node("peer:x") and not fed.is_peer_node("task:peer:x")


# ===================================================================== (d) a mirrored edge never makes a local gate stale
OPERATOR = {"kind": "operator", "id": "key:op", "authenticated_by": "api_key"}
LOCAL = {"kind": "agent", "id": "key:local", "authenticated_by": "api_key"}
VIEWER = {"kind": "viewer", "id": "key:view", "authenticated_by": "api_key"}


def _stale(repo, subjects=("task:A", "task:B")):
    out = {}
    for sub in subjects:
        for e in repo.graph.neighborhood(VIEWER, sub, depth=1)["edges"]:
            if e["edge_type"] == "validated" and e["to_id"] == sub:
                out[e["from_id"]] = e["stale"]
    return out


def _local_world(w):
    """Task A depends on attempt a1; each task has a validating gate at c0 (the T16 fork world)."""
    for t in ("A", "B"):
        for nid in (f"gate:{t}", f"task:{t}"):
            w.node(nid, who=LOCAL)
        w.edge("validated", f"gate:{t}", f"task:{t}", who=LOCAL, attrs={"commit": "c0"})
    w.node("attempt:a1", who=LOCAL)
    w.edge("dependency", "task:A", "attempt:a1", who=LOCAL)
    assert _stale(w.repo) == {"gate:A": False, "gate:B": False}


def test_positive_control_a_local_fork_decision_does_make_the_gate_stale(w):
    _local_world(w)
    w.node("approach:fk", who=LOCAL)
    w.edge("fork", "attempt:a1", "approach:fk", who=LOCAL)
    w.edge("supersession", "approach:fk", "attempt:a1", who=LOCAL)
    assert _stale(w.repo) == {"gate:A": True, "gate:B": False}


def test_a_peer_cannot_even_try_to_mark_a_local_gate_stale_through_ingest(w):
    _local_world(w)
    w.node("peer:acme.approach-fk")
    for a, b, et in (("peer:acme.approach-fk", "attempt:a1", "supersession"), ("peer:acme.approach-fk", "task:A", "supersession"),
                     ("task:A", "peer:acme.approach-fk", "dependency"), ("gate:A", "task:A", "validated")):
        refused(w.edge, et, a, b, attrs={"commit": "c9"} if et == "validated" else None)
    refused(w.node, "approach:fk")                                          # no local-looking decision node either
    assert _stale(w.repo) == {"gate:A": False, "gate:B": False}


def _raw(world, event_id, event_type, payload, actor):
    """Straight into the projection: no ingest guard, as if a peer-written edge had been stored some other way."""
    world.n += 1
    world.repo.graph.apply({"event_id": event_id, "source": "raw", "source_sequence": world.n, "event_type": event_type,
                            "payload": payload, "visibility_scope": "org", "actor": actor, "lineage_seq": 1000 + world.n,
                            "ingest_timestamp": "2026-10-25T12:00:00+00:00",
                            "source_timestamp": "2026-10-25T12:00:00+00:00"})


def test_the_graph_derivation_itself_ignores_peer_written_and_peer_namespace_edges(w):
    """Defence in depth: bypass ingest and apply peer-asserted edges directly; staleness must not move."""
    _local_world(w)
    n = [0]

    def raw(event_type, payload, actor=PEER):
        n[0] += 1
        _raw(w, f"raw-{n[0]}", event_type, payload, actor)
    raw("node.upsert", {"node_type": "approach", "node_id": "approach:fk", "label": "x"})       # a peer-written approach:
    raw("edge.add", {"edge_type": "fork", "from_id": "attempt:a1", "to_id": "approach:fk"})
    raw("edge.add", {"edge_type": "supersession", "from_id": "approach:fk", "to_id": "attempt:a1"})   # the decision
    raw("node.upsert", {"node_type": "peer", "node_id": "peer:acme.approach-z", "label": "x"})
    raw("edge.add", {"edge_type": "supersession", "from_id": "peer:acme.approach-z", "to_id": "attempt:a1"})
    raw("edge.add", {"edge_type": "dependency", "from_id": "attempt:a1", "to_id": "peer:acme.approach-z"})
    # a peer-written newer validated edge for the same subject with a different binding
    raw("edge.add", {"edge_type": "validated", "from_id": "gate:A", "to_id": "task:A", "attrs": {"commit": "c9"}})
    raw("edge.add", {"edge_type": "validated", "from_id": "peer:acme.gate", "to_id": "task:A", "attrs": {"commit": "c9"}})
    edges = {(e["edge_type"], e["from_id"], e["to_id"]) for e in w.repo.graph.neighborhood(OPERATOR, "attempt:a1", depth=2)["edges"]}
    assert ("supersession", "approach:fk", "attempt:a1") in edges           # the edges really are in the projection
    assert _stale(w.repo)["gate:B"] is False
    assert _stale(w.repo)["gate:A"] is False                                # only the local gate edge by LOCAL counts


def test_a_peer_dependency_edge_cannot_widen_the_closure_a_local_decision_reaches(w):
    """A fork decision about a peer-namespace node marks nothing stale just because a `dependency` edge says a
    local task depends on that node: edges touching the reserved namespace take no part in local staleness.
    (The control for this shape is test_positive_control_a_local_fork_decision_does_make_the_gate_stale.)"""
    _local_world(w)
    n = [0]

    def raw(event_type, payload, actor):
        n[0] += 1
        _raw(w, f"raw2-{n[0]}", event_type, payload, actor)
    raw("node.upsert", {"node_type": "peer", "node_id": "peer:acme.old", "label": "x"}, PEER)
    raw("edge.add", {"edge_type": "dependency", "from_id": "task:B", "to_id": "peer:acme.old"}, PEER)
    raw("edge.add", {"edge_type": "dependency", "from_id": "task:B", "to_id": "peer:acme.old"}, OPERATOR)
    raw("node.upsert", {"node_type": "approach", "node_id": "approach:loc", "label": "x"}, OPERATOR)
    raw("edge.add", {"edge_type": "supersession", "from_id": "approach:loc", "to_id": "peer:acme.old"}, OPERATOR)
    assert _stale(w.repo) == {"gate:A": False, "gate:B": False}
    # a peer-WRITTEN dependency edge between two local nodes cannot widen the closure either
    raw("node.upsert", {"node_type": "attempt", "node_id": "attempt:z", "label": "x"}, OPERATOR)
    raw("edge.add", {"edge_type": "dependency", "from_id": "task:B", "to_id": "attempt:z"}, PEER)
    raw("node.upsert", {"node_type": "approach", "node_id": "approach:loc2", "label": "x"}, OPERATOR)
    raw("edge.add", {"edge_type": "supersession", "from_id": "approach:loc2", "to_id": "attempt:z"}, OPERATOR)
    assert _stale(w.repo) == {"gate:A": False, "gate:B": False}
    # control: the same dependency written by a local principal does reach gate B
    raw("node.upsert", {"node_type": "attempt", "node_id": "attempt:z2", "label": "x"}, OPERATOR)
    raw("edge.add", {"edge_type": "dependency", "from_id": "task:B", "to_id": "attempt:z2"}, OPERATOR)
    raw("node.upsert", {"node_type": "approach", "node_id": "approach:loc3", "label": "x"}, OPERATOR)
    raw("edge.add", {"edge_type": "supersession", "from_id": "approach:loc3", "to_id": "attempt:z2"}, OPERATOR)
    assert _stale(w.repo) == {"gate:A": False, "gate:B": True}


def test_peer_edge_predicate():
    class E:
        def __init__(self, f, t, writer, writers=()):
            self.from_id, self.to_id, self.writer, self._w = f, t, writer, frozenset(writers)

        def writers(self):
            return self._w
    assert GraphProjection._peer_edge(E("peer:a.x", "task:t", "key:k"))
    assert GraphProjection._peer_edge(E("task:t", "peer:a.x", "key:k"))
    assert GraphProjection._peer_edge(E("task:t", "task:u", "peer:a:" + "ab" * 6))
    assert GraphProjection._peer_edge(E("task:t", "task:u", None, ["peer:a:" + "ab" * 6]))
    assert not GraphProjection._peer_edge(E("task:t", "task:u", "key:k", ["key:k"]))


# ===================================================================== review findings (PR #269)
def test_a_peer_cannot_store_a_row_under_a_local_stream(w):
    body = w.body("node.upsert", {"node_type": "peer", "node_id": "peer:acme.t", "label": "x"},
                  entity_id="mission:local-1")
    body["entity_id"] = "mission:local-1"
    refused(w.svc.ingest, body, PEER)
    assert w.repo.stream("lineage", "mission:local-1") == []
    body["entity_id"] = "peer:acme.t"
    w.svc.ingest(body, PEER)                                               # control: same event, own stream


def test_peer_evidence_refs_and_causation_are_claims_not_local_references(w):
    w.node("peer:acme.t", evidence_refs=[{"kind": "artifact", "uri": "artifact://local/secret"}],
           causation_id="local-event", correlation_id="local-flow")
    ev, meta = stored_meta(w, "peer:acme.t")
    assert not ev.get("evidence_refs") and ev.get("causation_id") is None and ev.get("correlation_id") is None
    assert meta["claimed_evidence_refs"][0]["uri"] == "artifact://local/secret"
    assert meta["claimed_ids"]["causation_id"] == "local-event"


def test_a_peer_removed_in_yaml_mid_request_does_not_land(w):
    reads = {"n": 0}

    def source():
        reads["n"] += 1
        return cfg() if reads["n"] <= 2 else {"enabled": True, "peers": {}}      # clamp + scope check, then gone
    w.svc.peer_source = source
    refused(w.node, "peer:acme.t")
    assert w.repo.stream("lineage", "peer:acme.t") == []
    w.svc.peer_source = lambda: cfg()                                             # control: still configured
    w.node("peer:acme.t")
