# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""Work Graph end to end: REAL collab events -> projection -> both HTTP front doors, with the flag off and on."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from herdr_engineering.control_plane import workspace_contracts as wc
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.control_plane.work_graph_service import WorkGraphService, handle
from herdr_engineering.control_plane.work_graph_store import WorkGraphStore
from tests.control_plane.test_collab_tasks import W, code, svc  # noqa: F401

from .wg_pg import wg, wg_pg  # noqa: F401

OP = {"kind": "operator", "id": "key:op", "authenticated_by": "api_key"}


@pytest.fixture
def world(svc, wg, wg_pg):
    w = W(svc)
    agent, info = w.agent("alice")
    task = svc.create_task(w.alice, w.room, {"target_agent_id": info["agent_id"], "text": "Investigate the CI failure\nsee log"})
    svc.update_task(agent, task["task_id"], {"status": "accepted"})
    svc.update_task(agent, task["task_id"], {"status": "done", "result": "fixed"})
    events = [dict(e) for e in svc._log]
    wg.run(WorkGraphStore(wg.pool).apply_events(events))
    service = WorkGraphService(wg_pg["dsn"], autostart=False)
    svc.work_graph_handle = lambda principal, method, parts, query, body: handle(service, principal, method, parts, query, body)
    w.task_id, w.agent_id, w.service, w.events = task["task_id"], info["agent_id"], service, events
    yield w
    service.close()


def get(svc, user, *parts, **query):
    return svc.dispatch("GET", ["graph", "work", *parts], query, {}, f"Bearer {tok(user)}", None)


def test_real_collab_events_project_into_project_room_task_and_agent(world, wg):
    st = WorkGraphStore(wg.pool)
    d = wg.run(st.dump())
    kinds = sorted(e["kind"] for e in d["entities"].values())
    assert {"project", "room", "task", "agent", "team"} <= set(kinds)
    task = d["entities"][wc.urn_for("task", world.task_id)]
    assert task["status"] == "done" and task["display_name"] == "Investigate the CI failure"
    assert ["collab_task", world.task_id] in task["aliases"] and task["attrs"]["room_task_ttl"] == "24h"
    roles = {(f["subject_ref"], f["segments"][0]["attrs"].get("role")) for f in d["facts"].values() if f["fact_kind"] == "team_role"}
    assert ("user:alice", "owner") in roles and ("user:bob", "contributor") in roles and ("user:dave", "guest") in roles
    assert all(e["source_event_id"] for e in d["entities"].values())


def test_collab_door_member_sees_their_work_and_a_stranger_or_guest_gets_one_404(world, svc):
    status, out = get(svc, "carol", "entities", kind="task")
    assert status == 200 and [t["native_id"] for t in out["items"]] == [world.task_id]
    status, out = get(svc, "alice", "tasks", world.task_id)
    assert status == 200 and out["root"]["status"] == "done"
    for stranger in ("mallory", "dave"):                                     # signed in, no non-guest room; and a guest
        for args in (("entities",), ("tasks", world.task_id), ("search",), ("snapshot",)):
            status, out = get(svc, stranger, *args, q="Investigate", root=f"task:{world.task_id}")
            assert status == 404 and out == {"error": "not found"}, (stranger, args)


def test_collab_door_other_project_is_invisible_and_layout_cas_works(world, svc):
    bob_pid = svc.create_project(world.bob, {"name": "bobs"})["project_id"]
    bob_room = svc.create_room(world.bob, bob_pid, {"name": "b"})["room_id"]
    wg_events = [dict(e) for e in svc._log]
    store = world.service.store or world.service._store()
    world.service._run(store.apply_events(wg_events))
    status, out = get(svc, "alice", "entities", kind="room,project")
    assert {i["native_id"] for i in out["items"]} == {world.room, world.room2, world_project(svc, world)}
    assert get(svc, "alice", "snapshot", root=f"room:{bob_room}")[0] == 404
    status, lay = get(svc, "alice", "layout", ref=f"room:{world.room}", which="default")
    assert status == 200 and lay["version"] == 0
    body = {"ref": f"room:{world.room}", "shared": True, "document": {"nodes": {wc.urn_for("room", world.room): {"x": 1, "y": 2}}}}
    assert svc.dispatch("POST", ["graph", "work", "layout"], {}, body, f"Bearer {tok('alice')}", None)[0] == 428   # no If-Match
    status, saved = svc.dispatch("POST", ["graph", "work", "layout"], {}, {**body, "if_match": lay["etag"]}, f"Bearer {tok('alice')}", None)
    assert status == 200 and saved["version"] == 1
    assert svc.dispatch("POST", ["graph", "work", "layout"], {}, {**body, "if_match": lay["etag"]}, f"Bearer {tok('alice')}", None)[0] == 412


def tok(name):
    from tests.control_plane.test_collab_tasks import USERS
    return USERS[name]


def world_project(svc, world):
    return svc._rooms[world.room]["project_id"]


# ---- the keyed door, over real HTTP, with the flag off and on ---------------------------------------------------
def serve(**kw):
    svr = ControlPlaneServer(ControlPlaneRepo(), api_keys={"op-key": "operator", "view-key": "viewer"}, **kw)
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    return svr


def call(svr, path, key="op-key", method="GET", body=None, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{svr.port}{path}", method=method, headers={"X-API-Key": key, **(headers or {})},
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read()), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}"), dict(e.headers)


def test_flag_off_is_a_404_and_the_capability_is_not_advertised():
    svr = serve()
    try:
        assert call(svr, "/api/v1/graph/work/entities")[0] == 404
        assert "unified_workspace_v1" not in call(svr, "/api/v1/health")[1]["capabilities"]
        assert call(svr, "/api/v1/graph/nodes?node_type=task")[0] == 200            # the existing lineage routes are untouched
    finally:
        svr.shutdown()


def test_flag_on_without_postgres_is_a_503_not_a_crash():
    svr = serve(unified_workspace_config={"enabled": True})
    try:
        status, out, _ = call(svr, "/api/v1/graph/work/entities")
        assert status == 503 and "unavailable" in out["error"]
        assert "unified_workspace_v1" in call(svr, "/api/v1/health")[1]["capabilities"]
    finally:
        svr.shutdown()


def test_keyed_door_extends_graph_routes_with_etag_paging_and_acl(world, wg, wg_pg):
    svr = serve(unified_workspace_config={"enabled": True}, github_pg_dsn=wg_pg["dsn"])
    try:
        for _ in range(100):
            if svr.work_graph.available():
                break
            threading.Event().wait(0.1)
        assert svr.work_graph.available()
        ref = urllib.parse.quote(f"task:{world.task_id}", safe="")
        status, ent, headers = call(svr, f"/api/v1/graph/work/entity?ref={ref}")
        assert status == 200 and ent["status"] == "done" and headers["ETag"] == ent["etag"]
        status, page, _ = call(svr, "/api/v1/graph/work/entities?kind=room&limit=1")
        assert status == 200 and len(page["items"]) == 1 and page["has_more"] and page["next_cursor"]
        status, nxt, _ = call(svr, f"/api/v1/graph/work/entities?kind=room&limit=1&cursor={page['next_cursor']}")
        assert status == 200 and nxt["items"][0]["id"] != page["items"][0]["id"]
        assert call(svr, "/api/v1/graph/work/entities?cursor=bad")[0] == 400
        # a plain viewer key is an org-level reader: room-scoped work is invisible, and it learns nothing from search
        status, vis, _ = call(svr, "/api/v1/graph/work/entities?kind=task", key="view-key")
        assert status == 200 and vis["items"] == []
        assert call(svr, f"/api/v1/graph/work/entity?ref={ref}", key="view-key")[0] == 404
        assert call(svr, "/api/v1/graph/work/search?q=Investigate", key="view-key")[1]["hits"] == []
        assert call(svr, "/api/v1/graph/work/status", key="view-key")[0] == 404
        status, st, _ = call(svr, "/api/v1/graph/work/status")
        assert status == 200 and st["counts"]["entities"] >= 5
        # snapshot by root and as_of
        status, snap, _ = call(svr, f"/api/v1/graph/work/snapshot?root={urllib.parse.quote('project:' + world_project_id(world))}&depth=2")
        assert status == 200 and any(n["kind"] == "task" for n in snap["nodes"])
        status, past, _ = call(svr, f"/api/v1/graph/work/snapshot?root={urllib.parse.quote('project:' + world_project_id(world))}&as_of=2001-01-01T00:00:00Z")
        assert status == 404                                             # the project did not exist then
        # layout writes over the keyed door are POST + If-Match + application/json (operator)
        pref = urllib.parse.quote("project:" + world_project_id(world), safe="")
        _, lay, h = call(svr, f"/api/v1/graph/work/layout?ref={pref}&which=default")
        body = {"ref": "project:" + world_project_id(world), "shared": True, "document": {"nodes": {}}}
        no_match = call(svr, "/api/v1/graph/work/layout", method="POST", body=body, headers={"Content-Type": "application/json"})
        assert no_match[0] == 428
        ok = call(svr, "/api/v1/graph/work/layout", method="POST", body=body,
                  headers={"Content-Type": "application/json", "If-Match": h["ETag"]})
        assert ok[0] == 200 and ok[1]["version"] == 1
        stale = call(svr, "/api/v1/graph/work/layout", method="POST", body=body,
                     headers={"Content-Type": "application/json", "If-Match": h["ETag"]})
        assert stale[0] == 412
        assert call(svr, "/api/v1/graph/work/layout", method="POST", body=body, key="view-key",
                    headers={"Content-Type": "application/json", "If-Match": ok[2]["ETag"]})[0] in (403, 404)
    finally:
        svr.shutdown()


def world_project_id(world):
    return world.service._run(world.service._store().get_entity(OP, f"room:{world.room}"))["project_id"]
