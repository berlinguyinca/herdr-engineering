# ruff: noqa: F811  (pytest fixtures are imported by name from registry_pg)
"""The dashboard library API over real HTTP on a real PostgreSQL (all migrations).  No mocks: a real ControlPlaneServer in a
thread, the real LibraryStore adapter and the real registry store.  The dashboard viewer is its API-key principal."""
import asyncio
import hashlib
import json
import threading
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager

import asyncpg
import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.artifact_acl import ArtifactAcl
from herdr_engineering.control_plane.artifact_adapter import SyncArtifactAdapter
from herdr_engineering.control_plane.artifact_library import PendingCapExceeded
from herdr_engineering.control_plane.artifact_library_api import (
    error_body,
    make_library_adapter,
    parse_feed_query,
)
from herdr_engineering.control_plane.artifacts import ArtifactStore
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.registry_pg import FakeBlobs, registry_pg  # noqa: F401 - imported fixture

KEYS = {"alice-key": "operator", "bob-key": "operator", "viewer-key": "viewer"}


def principal(key: str) -> str:
    return f"api-key:{hashlib.sha256(key.encode()).hexdigest()[:16]}:{KEYS[key]}"


ALICE, BOB = principal("alice-key"), principal("bob-key")


class Env:
    def __init__(self, pg):
        self.pg = pg
        self.base = ""

    def sql(self, query, *args):
        async def go():
            conn = await asyncpg.connect(self.pg["dsn"])
            try:
                return await conn.fetch(query, *args)
            finally:
                await conn.close()
        return asyncio.run(go())

    def artifact(self, owner, *, name="Doc", intake="accepted"):
        aid = "artifact_" + uuid.uuid4().hex
        self.sql("INSERT INTO artifacts (artifact_id,created_by,display_name,intake,original_filename)"
                 " VALUES ($1,$2,$3,$4,$5)", aid, owner, name, intake, f"{name}.html")
        return aid

    def req(self, path, *, key="alice-key", body=None, method=None, headers=None):
        headers = {"X-API-Key": key, **(headers or {})} if key else dict(headers or {})
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, headers=headers, data=data, method=method)
        try:
            with urllib.request.urlopen(request) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            raw = error.read()
            return error.code, json.loads(raw) if raw else None

    def folder(self, name="Papers", key="alice-key"):
        status, out = self.req("/api/v1/library/folders", key=key, body={"name": name})
        assert status == 201, out
        return out["folder"]["folder_id"]


@contextmanager
def library_server(pg):
    blobs = FakeBlobs()

    async def registry():
        return ArtifactStore(await asyncpg.connect(pg["dsn"]), blobs)

    async def pool():
        return await asyncpg.create_pool(pg["dsn"], min_size=1, max_size=2)

    reg, lib = SyncArtifactAdapter(registry), make_library_adapter(pool)
    server = ControlPlaneServer(ControlPlaneRepo(), api_keys=KEYS, artifact_store=reg, artifact_acl=ArtifactAcl(),
                                rustfs=blobs, library=lib)
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    server.wait_until_ready()
    env = Env(pg)
    env.base = f"http://127.0.0.1:{server.port}"
    try:
        yield env
    finally:
        server.shutdown()
        reg.close()
        lib.close()


@pytest.fixture
def env(registry_pg):
    with library_server(registry_pg) as value:
        value.sql("TRUNCATE artifacts, artifact_acl, artifact_shares, artifact_library_folders CASCADE")
        yield value


LIBRARY = "/api/v1/library"


def test_dashboard_library_requires_auth(env):
    folder = env.folder()
    aid = env.artifact(ALICE, intake="pending")
    routes = [("GET", LIBRARY, None), ("POST", LIBRARY + "/folders", {"name": "x"}),
              ("PUT", f"{LIBRARY}/folders/{folder}", {"name": "y"}), ("DELETE", f"{LIBRARY}/folders/{folder}", None),
              ("POST", LIBRARY + "/items", {"folder_id": folder, "artifact_id": aid}),
              ("DELETE", f"{LIBRARY}/folders/{folder}/items/{aid}", None),
              ("POST", f"{LIBRARY}/pending/{aid}/accept", {}), ("POST", f"{LIBRARY}/pending/{aid}/discard", {})]
    for method, path, body in routes:
        assert env.req(path, key=None, body=body, method=method)[0] == 401, (method, path)
        assert env.req(path, key="nobody", body=body, method=method)[0] in {401, 403}, (method, path)
    for method, path, body in routes[1:]:       # a read-only key may look but not write
        assert env.req(path, key="viewer-key", body=body, method=method)[0] == 403, (method, path)
    assert env.req(LIBRARY, key="viewer-key")[0] == 200
    assert env.sql("SELECT intake FROM artifacts WHERE artifact_id=$1", aid)[0]["intake"] == "pending"
    assert env.sql("SELECT name FROM artifact_library_folders")[0]["name"] == "Papers"


def test_viewer_only_sees_own_feed(env):
    mine = env.artifact(ALICE, name="Alice doc")
    theirs = env.artifact(BOB, name="Bob doc")
    granted = env.artifact(BOB, name="Granted to alice")
    env.sql("INSERT INTO artifact_acl (artifact_id, principal_id, permissions, granted_by)"
            " VALUES ($1,$2,'[\"read\"]'::jsonb,$3)", granted, ALICE, BOB)
    bob_folder = env.folder("Bobs", key="bob-key")
    status, alice = env.req(LIBRARY)
    assert status == 200
    ids = {i["artifact_id"]: i["source"] for i in alice["items"]}
    assert ids == {mine: "mine", granted: "assigned"} and theirs not in ids
    assert alice["folders"] == [] and bob_folder not in json.dumps(alice)
    _, bob = env.req(LIBRARY, key="bob-key")
    assert {i["artifact_id"] for i in bob["items"]} == {theirs, granted}
    assert [f["name"] for f in bob["folders"]] == ["Bobs"]


def test_feed_is_the_store_feed(env):
    """Both surfaces call LibraryStore.feed: the API's page equals the store's page for the same principal."""
    from herdr_engineering.control_plane.artifact_library import LibraryStore
    from herdr_engineering.control_plane.artifact_member_intents import _item_json
    env.artifact(ALICE, name="One")
    env.artifact(ALICE, name="Two")

    async def direct():
        pool = await asyncpg.create_pool(env.pg["dsn"], min_size=1, max_size=1)
        try:
            page = await LibraryStore(pool).feed(ALICE, "", frozenset(), principal=ALICE)
            return [_item_json(i) for i in page.items]
        finally:
            await pool.close()
    assert env.req(LIBRARY)[1]["items"] == asyncio.run(direct())


def test_folder_crud_through_api(env):
    aid = env.artifact(ALICE, name="Paper")
    fid = env.folder("Reading")
    assert env.req(LIBRARY + "/folders", body={"name": "reading"})[:1] == (409,)       # duplicate, case-insensitive
    assert env.req(LIBRARY + "/folders", body={"name": "  "})[0] == 400
    status, out = env.req(f"{LIBRARY}/folders/{fid}", method="PUT", body={"name": "To read"})
    assert (status, out["folder"]["name"]) == (200, "To read")
    assert env.req(LIBRARY + "/items", body={"folder_id": fid, "artifact_id": aid})[0] == 201
    _, feed = env.req(f"{LIBRARY}?folder_id={fid}")
    assert [i["artifact_id"] for i in feed["items"]] == [aid] and feed["items"][0]["folder_ids"] == [fid]
    assert feed["folders"][0]["item_count"] == 1
    assert env.req(f"{LIBRARY}/folders/{fid}/items/{aid}", method="DELETE")[0] == 200
    assert env.req(f"{LIBRARY}?folder_id={fid}")[1]["items"] == []
    assert env.req(f"{LIBRARY}/folders/{fid}", method="DELETE")[0] == 200
    assert env.req(LIBRARY)[1]["folders"] == []
    assert env.sql("SELECT 1 FROM artifacts WHERE artifact_id=$1", aid)            # the artifact itself stays


def test_other_viewers_folder_and_unreadable_artifact_are_the_same_404(env):
    bobs = env.folder("Bobs", key="bob-key")
    mine = env.artifact(ALICE)
    secret = env.artifact(BOB)
    missing = "artifact_" + uuid.uuid4().hex
    answers = [
        env.req(f"{LIBRARY}/folders/{bobs}", method="PUT", body={"name": "mine now"}),
        env.req(f"{LIBRARY}/folders/{bobs}", method="DELETE"),
        env.req(LIBRARY + "/items", body={"folder_id": bobs, "artifact_id": mine}),
        env.req(f"{LIBRARY}/folders/{bobs}/items/{mine}", method="DELETE"),
        env.req(f"{LIBRARY}/folders/fld_nope", method="PUT", body={"name": "x"}),
    ]
    assert {(s, json.dumps(b, sort_keys=True)) for s, b in answers} == {
        (404, json.dumps({"error": "not_found", "message": "not_found"}, sort_keys=True))}
    own = env.folder("Mine")
    unreadable = {json.dumps(env.req(LIBRARY + "/items", body={"folder_id": own, "artifact_id": a}))
                  for a in (secret, missing)}
    assert unreadable == {json.dumps([404, {"error": "not_found", "message": "not_found"}])}
    assert env.sql("SELECT name FROM artifact_library_folders WHERE folder_id=$1", bobs)[0]["name"] == "Bobs"
    assert env.sql("SELECT count(*) AS n FROM artifact_library_items")[0]["n"] == 0


def test_pending_accept_through_api(env):
    aid = env.artifact(ALICE, name="Pushed", intake="pending")
    _, default = env.req(LIBRARY)
    assert default["items"] == []                                              # pending is opt-in
    _, withp = env.req(LIBRARY + "?sources=pending")
    assert [(i["artifact_id"], i["source"], i["status"]) for i in withp["items"]] == [(aid, "pending", "pending")]
    assert env.req(f"{LIBRARY}/pending/{aid}/accept", key="bob-key", body={}, method="POST")[0] == 404
    assert env.req(f"{LIBRARY}/pending/{aid}/accept", body={})[0] == 200
    assert env.sql("SELECT intake FROM artifacts WHERE artifact_id=$1", aid)[0]["intake"] == "accepted"
    assert [i["source"] for i in env.req(LIBRARY)[1]["items"]] == ["mine"]
    assert env.req(f"{LIBRARY}/pending/{aid}/accept", body={})[0] == 404       # no longer pending


def test_pending_discard_retracts_through_the_delete_path(env):
    aid = env.artifact(ALICE, name="Unwanted", intake="pending")
    accepted = env.artifact(ALICE, name="Kept")
    assert env.req(f"{LIBRARY}/pending/{aid}/discard", key="bob-key", body={}, method="POST")[0] == 404
    assert env.req(f"{LIBRARY}/pending/{accepted}/discard", body={})[0] == 404     # only pending can be discarded
    assert env.req(f"{LIBRARY}/pending/{aid}/discard", body={})[0] == 200
    assert env.sql("SELECT lifecycle FROM artifacts WHERE artifact_id=$1", aid)[0]["lifecycle"] == "retracted"
    assert env.sql("SELECT lifecycle FROM artifacts WHERE artifact_id=$1", accepted)[0]["lifecycle"] == "active"
    assert env.req(LIBRARY + "?sources=pending,mine")[1]["items"][0]["artifact_id"] == accepted
    assert env.req(f"{LIBRARY}/pending/{aid}/discard", body={})[0] == 404


def test_search_filters_and_paging(env):
    env.artifact(ALICE, name="Quarterly budget")
    env.artifact(ALICE, name="Field notes")
    fid = env.folder("Finance")
    budget = next(i["artifact_id"] for i in env.req(LIBRARY + "?q=budget")[1]["items"])
    env.req(LIBRARY + "/items", body={"folder_id": fid, "artifact_id": budget})
    _, hit = env.req(LIBRARY + "?q=BUDGET")
    assert [i["title"] for i in hit["items"]] == ["Quarterly budget"] and hit["total"] == 1
    assert [i["title"] for i in env.req(LIBRARY + "?q=finance")[1]["items"]] == ["Quarterly budget"]   # by folder name
    assert env.req(LIBRARY + "?q=%25")[1]["items"] == []                                               # LIKE wildcards are literal
    _, page = env.req(LIBRARY + "?limit=1")
    assert len(page["items"]) == 1 and page["total"] == 2 and page["next_offset"] == 1
    assert env.req(LIBRARY + "?limit=1&offset=1")[1]["next_offset"] is None
    assert env.req(LIBRARY + "?sources=bogus")[0] == 400
    assert env.req(LIBRARY + "?since=yesterday")[0] == 400
    assert env.req(LIBRARY + "?limit=abc")[0] == 400
    assert env.req(LIBRARY + "?q=" + "x" * 201)[:1] == (400,)


def test_pending_cap_error_is_a_429_message():
    status, body = error_body(PendingCapExceeded())
    assert status == 429 and body["error"] == "pending_cap" and "pending" in body["message"]


def test_parse_feed_query_rejects_repeats_and_naive_times():
    with pytest.raises(ValueError):
        parse_feed_query({"q": ["a", "b"]})
    with pytest.raises(ValueError):
        parse_feed_query({"since": ["2026-01-01T00:00:00"]})
    assert parse_feed_query({"since": ["2026-01-01T00:00:00Z"]})["since"].year == 2026


def test_there_is_no_claim_route_and_writes_are_same_origin(env):
    fid = env.folder()
    assert env.req(LIBRARY + "/claims", body={"share_id": "s"})[0] == 404
    assert env.req(LIBRARY + "/claim", body={"share_id": "s"})[0] == 404
    assert env.req(LIBRARY + "/folders", body={"name": "x"}, headers={"Origin": "http://evil.example"})[0] == 403
    assert env.req(f"{LIBRARY}/folders/{fid}", method="PUT", body={"name": "x"},
                   headers={"Origin": "http://evil.example"})[0] == 403
    assert env.req(LIBRARY + "/folders", body={"name": "x", "owner": "member:bob"})[0] == 400   # identity is never in the body
