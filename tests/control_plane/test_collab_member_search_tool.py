# ruff: noqa: F811  (imported pytest fixtures are used as test arguments)
"""Task 12: the read-only ``artifact_search`` member tool, through the real ``collab_mcp.handle``; plus the shared
matcher against the SQL search on real PostgreSQL."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from herdr_engineering.control_plane.artifact_library import MAX_QUERY, library_matches
from herdr_engineering.control_plane.artifact_members import member_owner_key
from herdr_engineering.control_plane.collab_bridge_files import atomic_write_json
from herdr_engineering.control_plane.collab_member_tools import MEMBER_TOOL_NAMES
from tests.control_plane.test_artifact_member_service import ART, OTHER, World
from tests.control_plane.test_collab_approval import s as approval_svc  # noqa: F401
from tests.control_plane.test_collab_external import enroll
from tests.control_plane.test_collab_mcp_artifacts import err, names, ok_json, tool

TOOL = "artifact_search"


def item(aid, title, source="mine", status="active", folder_ids=(), sharer="member:op", kind="web", filename=""):
    return {"artifact_id": aid, "title": title, "sharer": sharer, "shared_at": "2026-11-01T00:00:00+00:00",
            "source": source, "status": status, "expires_at": None, "share_id": None, "folder_ids": list(folder_ids),
            "artifact_type": kind, "size_bytes": 1, "filename": filename}


def write_library(world, who, items, folders=()):
    key = member_owner_key(f"member:{who}")
    path = world.bridge.mirror / "member-views" / f"{key}.json"
    doc = json.loads(path.read_text())
    doc["library"] = {"items": items, "folders": [{"folder_id": i, "name": n, "item_count": 0} for i, n in folders]}
    atomic_write_json(path.parent, path.name, doc)


@pytest.fixture
def world(approval_svc, tmp_path):
    w = World(approval_svc, tmp_path)
    w.view("op", [ART])
    w.view("lab", [OTHER])
    w.info = w.agent("op")[1]
    write_library(w, "op", [
        item("a1", "Budget Q3", folder_ids=["fld_1"], filename="budget.xlsx", kind="spreadsheet"),
        item("a2", "Roadmap", source="assigned", sharer="member:lab"),
        item("a3", "Old share", source="opened", status="expired", kind="", filename=""),
        item("a4", "Revoked share", source="opened", status="revoked"),
        item("p1", "Secret push", source="pending", status="pending", filename="push.html"),
    ], folders=[("fld_1", "Finance"), ("fld_2", "Empty")])
    write_library(w, "lab", [item("l1", "Lab only budget", sharer="member:lab")])
    yield w
    w.bridge.stop()


def search(world, **args):
    return ok_json(tool(world, world.info, TOOL, **args)[1])


def ids(world, **args):
    return [i["artifact_id"] for i in search(world, **args)["items"]]


def test_tool_listed_for_member_agents_only(world):
    assert TOOL in MEMBER_TOOL_NAMES and TOOL in names(world, world.info)
    assert not names(world, world.agent("mal")[1]) & MEMBER_TOOL_NAMES
    pid = world.s.create_project(world.people["op"], {"name": "p"})["project_id"]
    room = world.s.create_room(world.people["op"], pid, {"name": "r"})["room_id"]
    _, ext, _ = enroll(SimpleNamespace(s=world.s, alice=world.people["op"], room=room))
    assert TOOL not in names(world, ext)
    for info in (world.agent("mal")[1], ext):
        assert err(world, info, TOOL)["status"] == 403


def test_description_is_plain_and_states_the_scope(world):
    from tests.control_plane.test_collab_member_tools import world_tools
    (desc,) = [t["description"] for t in world_tools(world, world.info) if t["name"] == TOOL]
    assert "read-only" in desc.lower() and "library" in desc and "pending" in desc and "never anyone else" in desc


def test_search_returns_owner_visible_items_only(world):
    out = search(world)
    assert [i["artifact_id"] for i in out["items"]] == ["a1", "a2", "a3", "a4"] and out["total"] == 4
    assert out["items"][0] == {"artifact_id": "a1", "title": "Budget Q3", "source": "mine", "status": "active",
                               "folders": ["Finance"]}
    assert ids(world, q="BUDGET") == ["a1"]            # lab's "Lab only budget" is another member's
    assert ids(world, q="xlsx") == ["a1"] and ids(world, q="finance") == ["a1"] and ids(world, q="spreadsheet") == ["a1"]
    assert ids(world, q="member:lab") == ["a2"]
    assert ids(world, source="assigned") == ["a2"] and ids(world, type="spreadsheet") == ["a1"]
    assert ids(world, limit=2) == ["a1", "a2"] and search(world, limit=2)["total"] == 4


def test_another_members_artifacts_never_returned(world):
    assert "l1" not in ids(world) and "l1" not in ids(world, q="lab only")
    lab = world.agent("lab")[1]
    assert [i["artifact_id"] for i in ok_json(tool(world, lab, TOOL)[1])["items"]] == ["l1"]


def test_pending_not_returned(world):
    assert "p1" not in ids(world) and ids(world, q="secret") == [] and ids(world, q="push.html") == []
    assert ids(world, source="mine") == ["a1"]


def test_folder_filter_by_name_and_id(world):
    assert ids(world, folder="Finance") == ["a1"] and ids(world, folder="fld_1") == ["a1"]
    assert ids(world, folder="Empty") == [] and ids(world, folder="nope") == [] and ids(world, folder="finance") == []


def test_expired_and_revoked_show_status_without_extras(world):
    by = {i["artifact_id"]: i for i in search(world)["items"]}
    assert by["a3"] == {"artifact_id": "a3", "title": "Old share", "source": "opened", "status": "expired",
                        "folders": []}
    assert by["a4"]["status"] == "revoked" and set(by["a4"]) == set(by["a3"])


def test_args_validated_unknown_key_refused(world):
    for bad in ({"nope": 1}, {"source": "pending"}, {"q": 5}, {"folder": ""}, {"type": 3}, {"limit": "5"},
                {"limit": True}):
        assert err(world, world.info, TOOL, **bad)["status"] == 400, bad
    assert err(world, world.info, TOOL, q="x" * (MAX_QUERY + 1))["status"] == 400
    assert ids(world, q="x" * MAX_QUERY) == []


def test_limit_above_50_refused(world):
    assert err(world, world.info, TOOL, limit=51)["status"] == 400
    assert err(world, world.info, TOOL, limit=0)["status"] == 400
    assert len(ids(world, limit=50)) == 4


def test_missing_view_fails_closed_with_a_clear_error(world):
    (world.bridge.mirror / "member-views" / f"{member_owner_key('member:op')}.json").unlink()
    out = err(world, world.info, TOOL)
    assert out["status"] == 503 and "not available" in out["error"]


def test_tool_is_read_only(world):
    before = (world.bridge.mirror / "member-views" / f"{member_owner_key('member:op')}.json").read_bytes()
    search(world, q="budget")
    assert (world.bridge.mirror / "member-views" / f"{member_owner_key('member:op')}.json").read_bytes() == before


def test_matcher_rules():
    kw = dict(title="A_b 100%", sharer="Zed", artifact_type="web", filename="Plan.PDF", status="active",
              folder_names=["Reads"])
    assert library_matches("", **kw) and library_matches("a_b", **kw) and library_matches("%", **kw)
    assert library_matches("plan.pdf", **kw) and library_matches("reads", **kw) and not library_matches("axb", **kw)
    assert not library_matches("plan.pdf", **{**kw, "status": "expired"})


# ----------------------------------------------------------------------------- parity with the SQL search
from tests.control_plane import test_artifact_library_feed as base  # noqa: E402
from tests.control_plane.test_artifact_library_feed import (  # noqa: E402,F401
    artifact,
    dsn,
    feed,
    members,
    run,
    share,
    share_postgres,
)


def test_python_matcher_equals_sql_search(dsn):
    async def body(store, pool):
        rows = {}
        for name, cols in {"Quarterly Budget": {}, None: {"original_filename": "Roadmap-Final.pdf"},
                           "Plain": {"artifact_type": "spreadsheet"}, "100% done": {}, "a_b": {}, "axb": {},
                           "Filed": {}}.items():
            aid = await artifact(pool, base.ME_P, name=name)
            for col, val in cols.items():
                await pool.execute(f"UPDATE artifacts SET {col}=$1 WHERE artifact_id=$2", val, aid)  # noqa: S608
            rows[aid] = name
        folder = await store.create_folder(base.ME_P, "Onboarding")
        await store.file_artifact(base.ME_P, folder.folder_id, next(k for k, v in rows.items() if v == "Filed"))
        sharer = await artifact(pool, base.YOU_P, name="Zed")
        await share(pool, sharer, emails=[base.EMAIL], title="Zed")
        everything = (await feed(store)).items
        files = {r["artifact_id"]: r["original_filename"] or "" for r in
                 await pool.fetch("SELECT artifact_id, original_filename FROM artifacts")}
        fname = {f.folder_id: f.name for f in await store.list_folders(base.ME_P)}
        for q in ("QUARTERLY", "roadmap-final", "spreadsheet", "onboard", base.YOU, "%", "_", "a_b", "\\", "zzz",
                  "web", "Plain", "pdf"):
            sql = {i.artifact_id for i in (await feed(store, q=q)).items}
            py = {i.artifact_id for i in everything if library_matches(
                q, title=i.title, sharer=i.sharer, artifact_type=i.artifact_type, filename=files[i.artifact_id],
                status=i.status, folder_names=[fname[f] for f in i.folder_ids])}
            assert py == sql, q

    run(dsn, body)
