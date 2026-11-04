# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""Explicit teams in the Work Graph: the producer of the ``team`` kind (create / rename / archive / list).

Real Postgres, real collab service, real HTTP for the keyed door; no mocks."""
from __future__ import annotations

import threading
from datetime import datetime

import pytest

from herdr_engineering.control_plane import workspace_contracts as wc
from herdr_engineering.control_plane.work_graph_service import handle
from herdr_engineering.control_plane.work_graph_store import WorkGraphStore

from .test_collab_tasks import svc  # noqa: F401
from .test_work_graph_api import OP, call, get, serve, tok, world, world_project  # noqa: F401
from .wg_pg import wg, wg_pg  # noqa: F401

JSON = {"Content-Type": "application/json"}


@pytest.fixture(autouse=True)
def clean_source_log(wg):
    """The projection tables are truncated per test by ``wg``; the source log (fabric_events) lives as long as the module's
    database, so team events are cleared too."""
    wg.run(wg.pool.execute("DELETE FROM fabric_events"))
    yield


def make(world, **body):
    return handle(world.service, OP, "POST", ["teams"], {}, body)


def listing(world, principal=OP, **query):
    status, out, _ = handle(world.service, principal, "GET", ["teams"], query)
    assert status == 200
    return out["items"]


def names(items):
    return sorted(i["display_name"] for i in items)


def test_an_operator_creates_a_team_linked_to_a_room_and_it_is_listed(world, wg, svc):
    status, out, _ = make(world, name="  Release   crew ", room_id=world.room)
    assert status == 200 and out["created"] is True
    team = out["team"]
    assert team["kind"] == "team" and team["display_name"] == "Release crew" and team["status"] == "active"
    assert team["project_id"] == world_project(svc, world)           # derived from the room
    assert team["attrs"] == {"origin": "explicit"}
    assert "Release crew" in names(listing(world))
    # the source of truth is the event log: exactly one team event, written by the operator
    rows = wg.run(wg.pool.fetch("SELECT event_type, actor ->> 'id' AS who, payload FROM fabric_events WHERE entity_type = 'team'"))
    assert [(r["event_type"], r["who"]) for r in rows] == [("team_created", "key:op")]


def test_creating_the_same_team_twice_is_one_team(world, wg):
    first = make(world, name="Docs", room_id=world.room)[1]
    again = make(world, name="docs", room_id=world.room)[1]
    assert again["created"] is False and again["team"]["id"] == first["team"]["id"]
    assert len([i for i in listing(world) if i["display_name"] == "Docs"]) == 1
    assert wg.run(wg.pool.fetchval("SELECT count(*) FROM fabric_events WHERE entity_type = 'team'")) == 1
    other = make(world, name="Docs", room_id=world.room, idempotency_key="second-docs")[1]
    assert other["created"] is True and other["team"]["id"] != first["team"]["id"]


def test_rename_and_archive_change_the_listing_and_keep_history(world):
    team = make(world, name="Old name", room_id=world.room)[1]["team"]
    native = team["native_id"]
    status, out, _ = handle(world.service, OP, "POST", ["teams", native, "rename"], {}, {"name": "New name"})
    assert status == 200 and out["changed"] is True and out["team"]["display_name"] == "New name"
    assert handle(world.service, OP, "POST", ["teams", native, "rename"], {}, {"name": "New name"})[1]["changed"] is False
    status, out, _ = handle(world.service, OP, "POST", ["teams", native, "archive"], {}, {})
    assert status == 200 and out["changed"] is True and out["team"]["status"] == "archived"
    assert handle(world.service, OP, "POST", ["teams", native, "archive"], {}, {})[1]["changed"] is False
    assert "New name" not in names(listing(world))                                     # active is the default
    assert "New name" in names(listing(world, status="archived")) and "New name" in names(listing(world, status="all"))
    assert handle(world.service, OP, "POST", ["teams", native, "rename"], {}, {"name": "Again"})[0] == 400
    # history stays: the entity is archived, not deleted
    status, ent, _ = handle(world.service, OP, "GET", ["entity"], {"ref": f"team:{native}"})
    assert status == 200 and ent["status"] == "archived" and ent["tombstone"] is False


def test_the_room_is_the_membership_and_a_hidden_team_is_never_named(world, svc):
    linked = make(world, name="Alice and friends", room_id=world.room)[1]["team"]
    lonely = make(world, name="Room two", room_id=world.room2)[1]["team"]
    private = make(world, name="Operators only")[1]["team"]
    # carol is a viewer in room 1 only: she sees the room-1 team, never room 2's nor the unlinked one
    status, out = get(svc, "carol", "teams")
    assert status == 200 and names(out["items"]) == ["Alice and friends"]
    # alice is in both rooms
    assert names(get(svc, "alice", "teams")[1]["items"]) == ["Alice and friends", "Room two", "p team"]   # "p team" = the per-project team
    # the guest, a stranger and a plain viewer key learn nothing
    for who in ("dave", "mallory"):
        assert get(svc, who, "teams")[0] == 404
    assert listing(world, {"kind": "viewer", "id": "key:view", "authenticated_by": "api_key"}) == []
    for team in (lonely, private):
        status, out = get(svc, "carol", "entity", ref=f"team:{team['native_id']}")
        assert status == 404 and out == {"error": "not found"}
    assert get(svc, "carol", "entity", ref=f"team:{linked['native_id']}")[0] == 200


def test_the_team_map_root_reaches_the_room_and_the_project(world):
    team = make(world, name="Mapped", room_id=world.room)[1]["team"]
    status, snap, _ = handle(world.service, OP, "GET", ["snapshot"], {"root": f"team:{team['native_id']}", "depth": "2"})
    assert status == 200
    kinds = {n["kind"] for n in snap["nodes"]}
    assert {"team", "room", "project"} <= kinds
    types = {e["type"] for e in snap["edges"]}
    assert {"contains", "references"} <= types


def test_a_project_only_team_links_the_project_but_stays_operator_only(world, svc):
    pid = world_project(svc, world)
    team = make(world, name="Whole project", project_id=pid)[1]["team"]
    assert team["project_id"] == pid
    assert "Whole project" in names(listing(world))
    assert "Whole project" not in names(get(svc, "alice", "teams")[1]["items"])


def test_bad_input_and_unknown_links_are_refused_for_the_stated_reason(world):
    for bad in ({}, {"name": ""}, {"name": "   "}, {"name": "x" * 121}, {"name": 5}, {"name": "tab\there\x00"}):
        assert make(world, **bad)[0] == 400, bad
    assert make(world, name="n", room_id="no-such-room")[0] == 404
    assert make(world, name="n", project_id="no-such-project")[0] == 404
    assert make(world, name="n", room_id=world.room, project_id="some-other-project")[0] in (400, 404)
    assert handle(world.service, OP, "POST", ["teams", "team-nope", "archive"], {}, {})[0] == 404
    assert handle(world.service, OP, "GET", ["teams"], {"status": "bogus"})[0] == 400


def test_only_an_operator_may_write_and_a_member_gets_the_plain_404(world, svc):
    for principal in ({"kind": "viewer", "id": "key:view", "authenticated_by": "api_key"},
                      {"kind": "member", "id": "user:alice", "authenticated_by": "collab", "access": {"rooms": [world.room]}}):
        assert handle(world.service, principal, "POST", ["teams"], {}, {"name": "x", "room_id": world.room}) \
            == (404, {"error": "not found"}, {})
    status, out = svc.dispatch("POST", ["graph", "work", "teams"], {}, {"name": "x", "room_id": world.room},
                               f"Bearer {tok('alice')}", None)
    assert status == 404
    assert "x" not in names(listing(world))


def test_a_rebuild_from_clean_brings_every_team_back(world, wg):
    a = make(world, name="Survivor", room_id=world.room)[1]["team"]["native_id"]
    b = make(world, name="Renamed once", room_id=world.room2)[1]["team"]["native_id"]
    handle(world.service, OP, "POST", ["teams", b, "rename"], {}, {"name": "Renamed twice"})
    handle(world.service, OP, "POST", ["teams", a, "archive"], {}, {})
    for e in world.events:                                   # the collab history is in the source log too, as in production
        wg.run(wg.pool.execute(
            "INSERT INTO fabric_events (event_id, sequence, event_type, entity_type, entity_id, payload, source_timestamp) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7)", e["event_id"], e["sequence"], e["event_type"], e["entity_type"], e["entity_id"],
            e["payload"], datetime.fromisoformat(e["source_timestamp"])))
    world.service.reconcile()                                # the ledger now holds every event the source holds
    before = wg.run(WorkGraphStore(wg.pool).dump())
    world.service.rebuild()
    after = wg.run(WorkGraphStore(wg.pool).dump())
    team_ids = {wc.urn_for("team", a), wc.urn_for("team", b)}
    assert {k: after["entities"][k] for k in team_ids} == {k: before["entities"][k] for k in team_ids}
    assert after["entities"][wc.urn_for("team", a)]["status"] == "archived"
    assert after["entities"][wc.urn_for("team", b)]["display_name"] == "Renamed twice"
    assert after == before


def test_the_worker_repairs_a_team_event_that_was_appended_but_never_projected(world, wg):
    # simulate the crash window: the row is in the source log, the projection never saw it
    wg.run(wg.pool.execute(
        "INSERT INTO fabric_events (event_id, sequence, event_type, entity_type, entity_id, payload, source_timestamp, source, actor, "
        "visibility_scope) VALUES ('team.created.team-orphan', 0, 'team_created', 'team', 'team-orphan', $1, now(), 'workgraph.team', "
        "$2, 'room')", {"name": "Orphan", "project_id": None, "room_id": None}, {"kind": "operator", "id": "key:op"}))
    assert "Orphan" not in names(listing(world))
    world.service.reconcile()
    assert "Orphan" in names(listing(world))


# ---- the keyed door over real HTTP -------------------------------------------------------------------------------
def test_keyed_door_create_list_rename_archive_and_roles(world, wg_pg):
    svr = serve(unified_workspace_config={"enabled": True}, github_pg_dsn=wg_pg["dsn"])
    try:
        for _ in range(100):
            if svr.work_graph.available():
                break
            threading.Event().wait(0.1)
        assert svr.work_graph.available()
        status, out, _ = call(svr, "/api/v1/graph/work/teams", method="POST", body={"name": "HTTP team", "room_id": world.room},
                              headers=JSON)
        assert status == 200 and out["created"] is True
        native = out["team"]["native_id"]
        status, page, _ = call(svr, "/api/v1/graph/work/teams")
        assert status == 200 and "HTTP team" in names(page["items"])
        assert call(svr, f"/api/v1/graph/work/teams/{native}/rename", method="POST", body={"name": "HTTP renamed"}, headers=JSON)[1]["team"][
            "display_name"] == "HTTP renamed"
        assert call(svr, f"/api/v1/graph/work/teams/{native}/archive", method="POST", body={}, headers=JSON)[1]["team"]["status"] == "archived"
        assert "HTTP renamed" not in names(call(svr, "/api/v1/graph/work/teams")[1]["items"])
        assert "HTTP renamed" in names(call(svr, "/api/v1/graph/work/teams?status=all")[1]["items"])
        # a viewer key reads nothing and writes nothing
        assert call(svr, "/api/v1/graph/work/teams?status=all", key="view-key")[1]["items"] == []
        assert call(svr, "/api/v1/graph/work/teams", method="POST", body={"name": "no"}, key="view-key", headers=JSON)[0] in (403, 404)
        assert call(svr, "/api/v1/graph/work/teams", method="POST", body={"name": ""}, headers=JSON)[0] == 400
    finally:
        svr.shutdown()


def test_teams_are_off_without_the_flag_and_503_without_postgres():
    off = serve()
    try:
        assert call(off, "/api/v1/graph/work/teams")[0] == 404
        assert call(off, "/api/v1/graph/work/teams", method="POST", body={"name": "x"}, headers=JSON)[0] == 404
    finally:
        off.shutdown()
    nodb = serve(unified_workspace_config={"enabled": True})
    try:
        status, out, _ = call(nodb, "/api/v1/graph/work/teams")
        assert status == 503 and "unavailable" in out["error"]
    finally:
        nodb.shutdown()
