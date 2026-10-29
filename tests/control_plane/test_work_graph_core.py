"""Work Graph pure core: replay equality, idempotence, reordering, per-field merge, history (always runs, no database)."""
from __future__ import annotations

import copy
import random

import pytest

from herdr_engineering.control_plane import work_graph_core as core
from herdr_engineering.control_plane import workspace_contracts as wc

from .work_graph_events import scenario


def build(events):
    p = core.Projection()
    p.apply_all(events)
    return p


def kinds(dump, kind):
    return [e for e in dump["entities"].values() if e["kind"] == kind]


def test_the_stream_projects_the_expected_work_objects():
    d = build(scenario().events).dump()
    assert {e["native_id"] for e in kinds(d, "project")} == {"p1", "p2"}
    assert len(kinds(d, "task")) == 12 and len(kinds(d, "room")) == 4
    t = d["entities"][wc.urn_for("task", "p1-r0-t0")]
    assert t["status"] == "done" and t["display_name"] == "Investigate failure p1-r0-t0"
    assert t["attrs"]["room_task_id"] == "p1-r0-t0" and t["attrs"]["room_task_ttl"] == "24h"
    assert ["collab_task", "p1-r0-t0"] in t["aliases"]                       # the durable node links to the room task
    assert wc.urn_for("task", "p1-r0-t0") == wc.urn_for("task", "p1-r0-t0")


def test_replay_in_any_order_gives_the_same_graph():
    events = scenario().events
    want = build(events).digest()
    rng = random.Random(7)
    for _ in range(25):
        shuffled = events[:]
        rng.shuffle(shuffled)
        assert build(shuffled).digest() == want


def test_a12_repeated_events_create_no_duplicates():
    events = scenario().events
    want = build(events).dump()
    noisy = [e for e in events for _ in range(random.Random(3).randint(1, 3))]
    p = core.Projection()
    first = p.apply_all(noisy)
    assert first == len(events)                                              # every repeat was a no-op
    assert p.dump() == want
    assert len(kinds(p.dump(), "task")) == 12                                # never a duplicate task


def test_a12_dropped_event_is_repaired_by_applying_it_later():
    events = scenario().events
    want = build(events).digest()
    dropped = [e for e in events if e["event_id"] == "ev-collab-20"][0]
    p = core.Projection()
    p.apply_all([e for e in events if e is not dropped])
    assert p.digest() != want                                                # a gap changes the graph
    assert p.apply(dropped) is True and p.digest() == want                   # and the late event converges it
    assert p.apply(copy.deepcopy(dropped)) is False


def test_a07_history_survives_when_agents_and_rooms_close():
    d = build(scenario().events).dump()
    agent = d["entities"][wc.urn_for("agent", "p1-agent")]
    assert agent["status"] == "revoked" and agent["tombstoned_at"] is None   # the node stays
    carol = [f for f in d["facts"].values() if f["subject_ref"] == "user:carol" and f["object_id"] == wc.urn_for("room", "p1-r0")]
    assert len(carol) == 1 and carol[0]["open"] is False                     # role assignment kept as a closed segment
    assert carol[0]["segments"][0]["attrs"]["role"] == "member" and carol[0]["segments"][0]["to"]
    sess = [f for f in d["facts"].values() if f["fact_kind"] == "agent_session"]
    assert len(sess) == 1 and sess[0]["open"] is False and sess[0]["segments"][0]["from"]
    grant = [f for f in d["facts"].values() if f["fact_kind"] == "agent_grant" and "p1-agent" in f["subject_ref"]]
    assert grant and not grant[0]["open"]
    assert wc.urn_for("agent_session", "sess-1") in d["entities"]
    # the edges to the closed room's tasks are all still live (a closed agent does not unlink the work)
    live = [e for e in d["edges"].values() if e["live"] and e["edge_type"] == "contains"]
    assert len(live) >= 12


def test_per_field_merge_a_late_create_does_not_lose_an_early_update():
    ev = scenario().events
    only = [e for e in ev if e["payload"].get("task_id") == "p1-r0-t0" or e["event_type"] in ("project_created", "room_created")]
    upd = [e for e in only if e["event_type"] == "task_updated"]
    created = [e for e in only if e["event_type"] == "task_created"][0]
    p = core.Projection()
    p.apply_all(upd)                                                          # updates first: a stub task with a status
    stub = p.dump()["entities"][wc.urn_for("task", "p1-r0-t0")]
    assert stub["status"] == "done" and stub["display_name"] == "p1-r0-t0"
    p.apply(created)
    t = p.dump()["entities"][wc.urn_for("task", "p1-r0-t0")]
    assert t["status"] == "done" and t["display_name"].startswith("Investigate")     # create filled name, did not reset status


def test_tombstone_and_edge_remove_are_not_resurrected_by_a_late_older_add():
    s = scenario()
    s.collab(300, "mission_linked", project_id="p1", room_id="p1-r0", mission_id="mX")
    link = s.events[-1]
    s.collab(310, "mission_unlinked", project_id="p1", room_id="p1-r0", mission_id="mX")
    unlink = s.events[-1]
    a = build([unlink, link]).dump()
    b = build([link, unlink]).dump()
    assert a == b
    e = [x for x in a["edges"].values() if x["to_id"] == wc.urn_for("mission", "mX")][0]
    assert e["live"] is False and e["tombstoned_at"] and len(e["intervals"]) == 1
    relink = s.collab(320, "mission_linked", project_id="p1", room_id="p1-r0", mission_id="mX")
    c = build([relink, unlink, link]).dump()
    e = [x for x in c["edges"].values() if x["to_id"] == wc.urn_for("mission", "mX")][0]
    assert e["live"] is True and len(e["intervals"]) == 2                    # interval history, not a delete


def test_artifact_versions_are_immutable_and_chained():
    d = build(scenario().events).dump()
    v1 = d["entities"][wc.urn_for("artifact_version", "ver-1")]
    v2 = d["entities"][wc.urn_for("artifact_version", "ver-2")]
    assert v1["display_name"] == "art-1 v1" and v2["attrs"]["parent_version_id"] == "ver-1"
    sup = [e for e in d["edges"].values() if e["edge_type"] == "supersedes"]
    assert [(e["from_id"], e["to_id"]) for e in sup] == [(v2["entity_id"], v1["entity_id"])]
    s = scenario()
    s.events.append({**s.events[-1], "event_id": "evil", "source_timestamp": s.at(500), "source_sequence": 99,
                     "payload": {"artifact_id": "art-1", "version_id": "ver-1", "version_number": 1, "title": "x", "state": "committed"}})
    again = build(s.events).dump()["entities"][wc.urn_for("artifact_version", "ver-1")]
    assert again["display_name"] == "art-1 v1"                                # a later upsert cannot rewrite a version


def test_every_node_edge_and_fact_links_to_a_source_event():
    p = build(scenario().events)
    assert core.evidence_violations(p.dump(), p.applied) == []
    assert core.evidence_violations(p.dump(), list(p.applied)[1:]) != []    # the check really notices a missing event


def test_orphan_check_works_in_both_directions():
    p = build(scenario().events)
    reg = {"art-1": [{"room_id": "p1-r0", "task_id": "p1-r0-t0", "issue_ref": "acme/app#7", "project_id": "p1"}]}
    assert core.artifact_orphans(p.dump(), reg) == {"missing": [], "unlinked": [], "dangling": [], "unbound": []}
    reg2 = {**reg, "art-2": [{"room_id": "p1-r0"}], "art-3": []}
    o = core.artifact_orphans(p.dump(), reg2)
    assert o["missing"] == ["art-2", "art-3"]
    assert core.artifact_orphans(p.dump(), {})["dangling"] == ["art-1"]
    reg3 = {"art-1": reg["art-1"] + [{"room_id": "elsewhere"}]}
    assert core.artifact_orphans(p.dump(), reg3)["unlinked"] == ["art-1<-room:elsewhere"]


def test_edges_obey_the_p01_endpoint_matrix():
    with pytest.raises(wc.ContractError):
        core.edge("assigned_to", ("room", "r"), ("agent", "a"))
    d = build(scenario().events).dump()
    for e in d["edges"].values():
        fk, tk = wc.parse_urn(e["from_id"])[0], wc.parse_urn(e["to_id"])[0]
        af, at = wc.EDGE_ENDPOINTS[e["edge_type"]]
        assert fk in af and tk in at


def test_pi_ingest_fields_are_optional_and_session_without_room_is_private():
    ev = {"event_id": "s1", "entity_type": "session", "entity_id": "sx", "event_type": "SessionStarted", "sequence": 1,
          "source_timestamp": "2026-10-01T00:00:00Z", "payload": {}, "actor": {"id": "key:abc"}}
    d = build([ev]).dump()
    e = d["entities"][wc.urn_for("agent_session", "sx")]
    assert e["visibility"] == "private" and e["grantees"] == ["key:abc"] and e["room_ids"] == []
    ev2 = {**ev, "event_id": "s2", "entity_id": "sy", "visibility_scope": "room", "room_id": "r9", "workspace_id": "w",
           "payload": {"agent_instance_id": "inst-1"}}
    e2 = build([ev2]).dump()["entities"][wc.urn_for("agent_session", "sy")]
    assert e2["room_ids"] == ["r9"] and e2["attrs"]["agent_instance_id"] == "inst-1" and e2["attrs"]["workspace_id"] == "w"


def test_visibility_rule_mirrors_graph_can_see():
    from herdr_engineering.control_plane import graph
    members = {"kind": "member", "id": "user:alice", "authenticated_by": "collab",
               "access": {"rooms": frozenset({"r1"}), "room_ws": frozenset(), "room_missions": frozenset({("r1", "m1")})}}
    principals = [None, {"kind": "unknown", "id": None, "authenticated_by": "none"},
                  {"kind": "operator", "id": "key:op", "authenticated_by": "api_key"},
                  {"kind": "viewer", "id": "key:v", "authenticated_by": "api_key"}, members,
                  {**members, "access": {"rooms": frozenset({"r2"}), "room_ws": frozenset(), "room_missions": frozenset()}}]
    rows = [(("org", None, ()), {"visibility": "org"}), (("room", ("room", "r1", None, ()), ()), {"visibility": "room", "room_ids": ["r1"]}),
            (("room", ("room", "r2", None, ()), ()), {"visibility": "room", "room_ids": ["r2"]}),
            (("mission", ("mission", "m1", "r1", ()), ()), {"visibility": "mission", "mission_scope": ["r1|m1"]}),
            (("private", None, ("key:v",)), {"visibility": "private", "grantees": ["key:v"]}),
            (("private", None, ("key:zz",)), {"visibility": "private", "grantees": ["key:zz"]})]
    for pr in principals:
        for (_, label_args, writers), row in rows:
            scope = row["visibility"]
            label = label_args if scope in ("room", "mission") else scope
            want = graph.can_see(pr, label, frozenset(writers) if writers else None)
            assert bool(core.visible(pr, row)) == want, (pr, row)
