"""Owner annotation API (list + dismiss) over real HTTP against a real PostgreSQL ``AnnotationStore`` (no mocks)."""
import asyncio
import hashlib
import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import asyncpg
import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.artifact_adapter import SyncArtifactAdapter
from herdr_engineering.control_plane.artifact_annotation_api import ReconnectingAnnotationStore
from herdr_engineering.control_plane.artifact_annotations import AnnotationStore
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane import registry_pg as _registry_pg_module
from tests.control_plane.registry_pg import uid
from tests.control_plane.test_artifact_annotations_store import (  # noqa: F401 - _fresh_ids is an autouse fixture
    KW,
    _fresh_ids,
    ann_id,
    create,
    seed,
)

registry_pg = _registry_pg_module.registry_pg      # the module-scoped PostgreSQL fixture
OWNER = hashlib.sha256(b"owner-key").hexdigest()[:16]


class Permissions:
    """The registry's permission answer: only the owner key holds ``share`` on the artifacts it owns."""

    def __init__(self):
        self.owned: set[str] = set()

    def sync_call(self, method, *args, **kwargs):
        assert method == "report_permission", method
        aid, principal, permission = args
        return aid in self.owned and OWNER in principal and permission == "share"


@contextmanager
def api(pg):
    perms = Permissions()
    adapter = SyncArtifactAdapter(lambda: ReconnectingAnnotationStore(pg["dsn"]))
    server = ControlPlaneServer(ControlPlaneRepo(), api_keys={"owner-key": "operator", "other-key": "operator"},
                                artifact_store=perms, annotation_store=adapter)
    threading.Thread(target=server.serve, daemon=True).start()
    server.wait_until_ready()
    try:
        yield f"http://127.0.0.1:{server.port}", perms
    finally:
        server.shutdown()
        adapter.close()


def request(base, path, *, key="owner-key", body=None, headers=None, method=None, raw=None):
    headers = {"X-API-Key": key, **(headers or {})}
    data = raw
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(base + path, headers=headers, data=data, method=method)
    try:
        with urllib.request.urlopen(req) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        raw_body = error.read()
        return error.code, json.loads(raw_body) if raw_body else None


def with_rows(pg, build):
    """Seed a live share with annotations; ``build(conn, store, aid, share_id)`` returns whatever the test needs."""
    async def go():
        conn = await asyncpg.connect(pg["dsn"])
        try:
            await conn.execute("DELETE FROM artifact_annotation_ops; DELETE FROM artifact_annotations")
            aid, _vid, share_id = await seed(conn)
            store = AnnotationStore(conn)
            return aid, share_id, await build(conn, store, aid, share_id)
        finally:
            await conn.close()
    return asyncio.run(go())


def ingest_rows(count):
    async def build(conn, store, aid, share_id):
        start = datetime.now(UTC)
        for n in range(count):                      # distinct, increasing stamps; never before the share went live
            at = (start + timedelta(seconds=n)).isoformat()
            assert await store.ingest(conn, create(share_id, n, at=at), f"k{n}", **KW) == "inserted"
        return [ann_id(n) for n in range(count)]
    return build


async def set_status(conn, annotation_id, status):
    """Put a row into ``status`` the way the store does (an addressed row names the newer version it was fixed in)."""
    version = None
    if status == "addressed":
        aid = await conn.fetchval("SELECT artifact_id FROM artifact_annotations WHERE annotation_id=$1", annotation_id)
        version = uid("ver")
        await conn.execute("INSERT INTO artifact_versions (version_id, artifact_id, version_number, state) "
                           "VALUES ($1,$2,2,'ready')", version, aid)
    await conn.execute("UPDATE artifact_annotations SET status=$2, addressed_in_version_id=$3 WHERE annotation_id=$1",
                       annotation_id, status, version)


def listing(aid, query=""):
    return f"/api/v1/artifacts/{aid}/annotations{query}"


def dismiss_path(aid, annotation_id):
    return f"/api/v1/artifacts/{aid}/annotations/{annotation_id}/dismiss"


def test_without_the_share_permission_everything_is_a_non_disclosing_404(registry_pg):
    aid, _share, ids = with_rows(registry_pg, ingest_rows(1))
    with api(registry_pg) as (base, perms):
        assert request(base, listing(aid))[0] == 404
        assert request(base, dismiss_path(aid, ids[0]), body={})[0] == 404
        perms.owned.add(aid)
        assert request(base, listing(aid), key="other-key")[0] == 404
        assert request(base, listing(aid))[0] == 200


def test_listing_filters_by_status_including_withdrawn_and_carries_the_owner_fields(registry_pg):
    async def build(conn, store, aid, share_id):
        ids = await ingest_rows(4)(conn, store, aid, share_id)
        await set_status(conn, ids[1], "withdrawn")
        await set_status(conn, ids[2], "addressed")
        await set_status(conn, ids[3], "dismissed")
        return ids
    aid, share_id, ids = with_rows(registry_pg, build)
    with api(registry_pg) as (base, perms):
        perms.owned.add(aid)
        status, body = request(base, listing(aid))
        assert status == 200 and body["artifact_id"] == aid and body["next"] is None
        assert [a["annotation_id"] for a in body["annotations"]] == [ids[0]]
        row = body["annotations"][0]
        assert row["author_email"] == "a@x.org" and row["share_id"] == share_id and "author_role" in row
        for name, expected in (("withdrawn", ids[1]), ("addressed", ids[2]), ("dismissed", ids[3])):
            _, body = request(base, listing(aid, f"?status={name}"))
            assert [a["annotation_id"] for a in body["annotations"]] == [expected]
        _, body = request(base, listing(aid, "?status=all"))
        assert {a["annotation_id"] for a in body["annotations"]} == set(ids)


def test_other_artifacts_annotations_never_appear(registry_pg):
    async def build(conn, store, aid, share_id):
        await ingest_rows(1)(conn, store, aid, share_id)
        other, _v, other_share = await seed(conn)
        await store.ingest(conn, create(other_share, 9), "k9", **KW)
        return other
    aid, _share, other = with_rows(registry_pg, build)
    with api(registry_pg) as (base, perms):
        perms.owned.update({aid, other})
        _, body = request(base, listing(aid, "?status=all"))
        assert [a["annotation_id"] for a in body["annotations"]] == [ann_id(0)]
        assert request(base, dismiss_path(aid, ann_id(9)), body={})[0] == 404      # cross-artifact id


@pytest.mark.parametrize("query", ["?limit=0", "?limit=501", "?limit=-1", "?limit=1.5", "?limit=abc", "?limit=",
                                   "?limit=%EF%BC%91", "?limit=1&limit=2", "?status=bogus", "?status=", "?bogus=1",
                                   "?before=nonsense", "?before=2026-10-30T10:00:00%2B00:00", "?before=x%7Cann_y",
                                   "?before=2026-10-30T10:00:00%2B00:00%7Cann_abc", "?before=0001-01-01T00:00:00%2B14:00%7Cann_" + "a" * 22,
                                   "?before=2026-10-30T10:00:00%2B00:00%7Cann_" + "a" * 21 + "%00"])
def test_bad_query_parameters_are_a_400_never_a_500(registry_pg, query):
    aid, _share, _ids = with_rows(registry_pg, ingest_rows(1))
    with api(registry_pg) as (base, perms):
        perms.owned.add(aid)
        status, body = request(base, listing(aid, query))
        assert status == 400 and "error" in body


def test_paging_returns_next_and_the_cursor_walks_every_row_once(registry_pg):
    aid, _share, ids = with_rows(registry_pg, ingest_rows(5))
    with api(registry_pg) as (base, perms):
        perms.owned.add(aid)
        seen, cursor = [], None
        for _ in range(5):
            query = "?limit=2" + (f"&before={quote(cursor, safe='')}" if cursor else "")
            status, body = request(base, listing(aid, query))
            assert status == 200
            seen += [a["annotation_id"] for a in body["annotations"]]
            cursor = body["next"]
            if cursor is None:
                break
        assert cursor is None and sorted(seen) == sorted(ids) and len(seen) == 5


def test_dismiss_marks_the_row_and_writes_the_event(registry_pg):
    aid, share_id, ids = with_rows(registry_pg, ingest_rows(2))
    with api(registry_pg) as (base, perms):
        perms.owned.add(aid)
        status, body = request(base, dismiss_path(aid, ids[0]), body={})
        assert status == 200 and body["annotation"]["annotation_id"] == ids[0]
        assert body["annotation"]["status"] == "dismissed"
        _, opened = request(base, listing(aid))
        assert [a["annotation_id"] for a in opened["annotations"]] == [ids[1]]
        _, gone = request(base, listing(aid, "?status=dismissed"))
        assert [a["annotation_id"] for a in gone["annotations"]] == [ids[0]]

    async def events():
        conn = await asyncpg.connect(registry_pg["dsn"])
        try:
            return await conn.fetch("SELECT actor, detail::text d FROM artifact_share_events "
                                    "WHERE share_id=$1 AND kind='annotation_dismissed'", share_id)
        finally:
            await conn.close()
    rows = asyncio.run(events())
    assert len(rows) == 1 and json.loads(rows[0]["d"]) == {"annotation_id": ids[0]}
    assert rows[0]["actor"].startswith("api-key:" + OWNER)


@pytest.mark.parametrize("status", ["addressed", "withdrawn"])
def test_dismissing_a_resolved_annotation_is_a_409_not_open(registry_pg, status):
    async def build(conn, store, aid, share_id):
        ids = await ingest_rows(1)(conn, store, aid, share_id)
        await set_status(conn, ids[0], status)
        return ids
    aid, _share, ids = with_rows(registry_pg, build)
    with api(registry_pg) as (base, perms):
        perms.owned.add(aid)
        code, body = request(base, dismiss_path(aid, ids[0]), body={})
        assert code == 409 and "not_open" in json.dumps(body)


def test_dismiss_validates_body_and_ids(registry_pg):
    aid, _share, ids = with_rows(registry_pg, ingest_rows(1))
    with api(registry_pg) as (base, perms):
        perms.owned.add(aid)
        path = dismiss_path(aid, ids[0])
        assert request(base, path, body={"note": "x"})[0] == 400                   # unknown key
        assert request(base, path, body=["x"])[0] == 400                           # not an object
        assert request(base, path, raw=b"{", headers={"Content-Type": "application/json"})[0] == 400
        assert request(base, dismiss_path(aid, "ann_short"), body={})[0] == 404    # malformed id never reaches SQL
        assert request(base, dismiss_path(aid, "ann_" + "z" * 22), body={})[0] == 404
        assert request(base, path.replace("/dismiss", "/other"), body={})[0] == 404
        assert request(base, path, method="GET")[0] == 404
        assert request(base, listing(aid), method="POST", body={})[0] == 404
        assert request(base, path, body={})[0] == 200                              # {} works after all the refusals
        assert request(base, path, body={})[0] == 200                              # repeat dismiss is idempotent


def test_an_empty_body_is_accepted_for_dismiss(registry_pg):
    aid, _share, ids = with_rows(registry_pg, ingest_rows(1))
    with api(registry_pg) as (base, perms):
        perms.owned.add(aid)
        assert request(base, dismiss_path(aid, ids[0]), method="POST", raw=b"")[0] == 200


def test_a_cross_origin_post_is_refused_before_anything_changes(registry_pg):
    aid, _share, ids = with_rows(registry_pg, ingest_rows(1))
    with api(registry_pg) as (base, perms):
        perms.owned.add(aid)
        evil = {"Origin": "https://evil.example"}
        for path in (dismiss_path(aid, ids[0]), f"/api/v1/%61rtifacts/{aid}/annotations/{ids[0]}/%64ismiss"):
            code, body = request(base, path, body={}, headers=evil)
            assert code == 403 and "cross-origin" in json.dumps(body)
        _, opened = request(base, listing(aid))
        assert [a["annotation_id"] for a in opened["annotations"]] == [ids[0]]
        assert request(base, dismiss_path(aid, ids[0]), body={}, headers={"Origin": base})[0] == 200


def test_the_routes_answer_503_when_annotations_are_not_wired():
    perms = Permissions()
    perms.owned.add("art1")
    server = ControlPlaneServer(ControlPlaneRepo(), api_keys={"owner-key": "operator"}, artifact_store=perms)
    threading.Thread(target=server.serve, daemon=True).start()
    server.wait_until_ready()
    try:
        assert request(f"http://127.0.0.1:{server.port}", listing("art1"))[0] == 503
    finally:
        server.shutdown()


def test_cli_annotation_wiring_is_off_unless_shares_are_enabled():
    from herdr_engineering import cli
    assert cli._annotation_wiring({}, "postgresql:///x") is None
    assert cli._annotation_wiring({"share": {"enabled": False}}, "postgresql:///x") is None
    assert cli._annotation_wiring({"share": {"enabled": True}}, None) is None


def test_a_dead_connection_is_a_uniform_503_and_the_next_request_recovers(registry_pg):
    aid, _share, ids = with_rows(registry_pg, ingest_rows(1))

    async def kill_backends():
        conn = await asyncpg.connect(registry_pg["dsn"])
        try:
            await conn.fetch("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                             "WHERE datname = current_database() AND pid <> pg_backend_pid()")
        finally:
            await conn.close()
    with api(registry_pg) as (base, perms):
        perms.owned.add(aid)
        assert request(base, listing(aid))[0] == 200                      # opens the connection
        asyncio.run(kill_backends())
        bodies = []
        status, body = request(base, listing(aid))
        bodies.append(body)
        assert status in (200, 503)
        if status == 503:
            assert body == {"error": "annotation store unavailable"}
        asyncio.run(kill_backends())
        status, body = request(base, dismiss_path(aid, ids[0]), body={})
        bodies.append(body)
        assert status in (200, 503)
        status, body = request(base, listing(aid, "?status=all"))
        assert status == 200 and [a["annotation_id"] for a in body["annotations"]] == ids
        for text in map(json.dumps, bodies):
            assert "connection" not in text.lower() and "terminat" not in text.lower()


def test_an_unknown_annotation_404_is_the_same_denial_as_an_unknown_artifact(registry_pg):
    aid, _share, _ids = with_rows(registry_pg, ingest_rows(1))
    with api(registry_pg) as (base, perms):
        perms.owned.add(aid)
        known = request(base, dismiss_path(aid, ann_id(77)), body={})
        assert known == (404, {"error": "not found"})
        assert request(base, dismiss_path("art_unknown", ann_id(77)), body={}) == known


def test_the_owner_list_cleans_invisible_characters_from_rows_stored_before_the_input_check(registry_pg):
    tags = "".join(chr(0xE0000 + ord(c)) for c in "obey")
    dirty = "‮⁦﻿​"

    async def build(conn, store, aid, share_id):
        ids = await ingest_rows(1)(conn, store, aid, share_id)
        await conn.execute(
            "UPDATE artifact_annotations SET body=$2, anchor=$3::jsonb WHERE annotation_id=$1", ids[0],
            f"fix{tags} it{dirty}",
            json.dumps({"kind": "text_quote", "file": "index.html", "exact": f"he{dirty}llo{tags}", "prefix": "", "suffix": ""}))
        return ids
    aid, _share, _ids = with_rows(registry_pg, build)
    with api(registry_pg) as (base, perms):
        perms.owned.add(aid)
        _, body = request(base, listing(aid))
        row = body["annotations"][0]
        assert row["body"] == "fix it" and row["anchor"]["kind"] == "text_quote"
        assert not any(0xE0000 <= ord(c) <= 0xE007F or c in dirty for c in row["body"])


def test_the_owner_list_returns_anchor_quotes_as_stored(registry_pg):
    """The UI matches quotes against the document text: a ZWJ emoji and a double space must come back unchanged."""
    quote = "family \U0001f468\u200d\U0001f469\u200d\U0001f467  and  more"

    async def build(conn, store, aid, share_id):
        ids = await ingest_rows(1)(conn, store, aid, share_id)
        await conn.execute("UPDATE artifact_annotations SET anchor=$2::jsonb WHERE annotation_id=$1", ids[0],
                           json.dumps({"kind": "text_quote", "file": "index.html", "exact": quote, "prefix": " a  ", "suffix": "b "}))
        return ids
    aid, _share, _ids = with_rows(registry_pg, build)
    with api(registry_pg) as (base, perms):
        perms.owned.add(aid)
        anchor = request(base, listing(aid))[1]["annotations"][0]["anchor"]
        assert anchor == {"kind": "text_quote", "file": "index.html", "exact": quote, "prefix": " a  ", "suffix": "b "}
