"""The internal read API: real Postgres, real blobs, real HTTP on a real port, no doubles."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import asyncpg
import msgpack
import pytest

from herdr_artifact_host.blobs import BlobStore
from herdr_artifact_host.internal_server import (
    InternalConfig,
    InternalServer,
    ReadOnlyPool,
    build_app,
    key_digest,
)
from herdr_artifact_host.roles import Role, routes_for
from tests.artifact_host.seed import FILES, Host, Resp, Seeded

KEY = "dash-key-" + "x" * 32
AUTH = {"Authorization": f"Bearer {KEY}"}
JSON = {**AUTH, "Accept": "application/json"}
TABLES = (
    "share_events",
    "share_claims",
    "shares",
    "artifacts",
    "library_items",
    "member_usage",
    "annotations",
)


@dataclass
class Internal:
    host: Host
    port: int

    def get(self, path: str, headers: dict[str, str] | None = AUTH) -> Resp:
        return self.host.request("GET", path, headers, port=self.port)

    def req(
        self,
        method: str,
        path: str,
        headers: dict[str, str] | None = AUTH,
        body: bytes | None = None,
    ) -> Resp:
        return self.host.request(method, path, headers, body, port=self.port)

    def js(self, path: str) -> Any:
        r = self.get(path, JSON)
        assert r.status == 200, (path, r.status, r.body)
        return r.json()

    def counts(self) -> dict[str, int]:
        return {t: self.host.rows(f"SELECT count(*) AS n FROM {t}")[0]["n"] for t in TABLES}


def _start(host: Host, config: InternalConfig) -> InternalServer:
    async def boot() -> InternalServer:
        server = InternalServer(build_app(config, host._pool, BlobStore(host.blob_root)))
        await server.start()
        return server

    assert host.loop is not None
    return asyncio.run_coroutine_threadsafe(boot(), host.loop).result(30)


@pytest.fixture
def internal(host: Host) -> Iterator[Internal]:
    server = _start(host, InternalConfig(key_digest(KEY), key_failures_per_minute=10_000))
    try:
        yield Internal(host, server.port)
    finally:
        assert host.loop is not None
        asyncio.run_coroutine_threadsafe(server.stop(), host.loop).result(30)


def _version(s: Seeded) -> str:
    return s.version_id


# -- auth and methods -------------------------------------------------------------------------------------


def test_missing_and_wrong_key_are_the_same_401(internal: Internal) -> None:
    paths = ["/v1/feed", "/v1/shares", "/v1/nope", "/s/whatever"]
    shapes = set()
    for path in paths:
        for headers in (
            None,
            {"Authorization": "Bearer wrong"},
            {"Authorization": f"Basic {KEY}"},
            {"Authorization": f"Bearer {KEY} extra"},
            {"Authorization": "Bearer "},
            {"Authorization": f"bearer {KEY}"},
        ):
            r = internal.get(path, headers)
            assert r.status == 401, (path, headers)
            shapes.add(r.shape())
    assert len(shapes) == 1


def test_correct_key_is_accepted_and_digest_only_configured() -> None:
    with pytest.raises(ValueError):
        InternalConfig(KEY)  # the key itself is not a digest
    with pytest.raises(ValueError):
        InternalConfig(key_digest(KEY), role="member")
    assert InternalConfig(key_digest(KEY)).role == "dashboard-read"


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
def test_non_get_is_405(internal: Internal, method: str) -> None:
    for headers in (AUTH, None, {"Authorization": "Bearer wrong"}):
        r = internal.req(
            method, "/v1/feed", headers, b"x" if method in ("POST", "PUT", "PATCH") else None
        )
        assert r.status == 405, (method, headers)
        assert r.header("Allow") == "GET"


def test_failed_key_limiter_blocks_a_client(host: Host) -> None:
    server = _start(host, InternalConfig(key_digest(KEY), key_failures_per_minute=3))
    try:
        api = Internal(host, server.port)
        assert [api.get("/v1/feed", None).status for _ in range(3)] == [401, 401, 401]
        assert api.get("/v1/feed", None).status == 429
        assert (
            api.get("/v1/feed", AUTH).status == 429
        )  # blocked clients stay blocked for the window
    finally:
        assert host.loop is not None
        asyncio.run_coroutine_threadsafe(server.stop(), host.loop).result(30)


def test_spoofed_forwarded_for_from_public_peer_is_ignored(host: Host) -> None:
    # the peer is loopback (trusted proxy) so the last forwarded entry is the key; a non-address buys no new bucket
    server = _start(host, InternalConfig(key_digest(KEY), key_failures_per_minute=2))
    try:
        api = Internal(host, server.port)
        for i in range(2):
            api.get("/v1/feed", {"X-Forwarded-For": f"not-an-ip-{i}"})
        assert api.get("/v1/feed", {"X-Forwarded-For": "not-an-ip-9"}).status == 429
    finally:
        assert host.loop is not None
        asyncio.run_coroutine_threadsafe(server.stop(), host.loop).result(30)


# -- wire format ------------------------------------------------------------------------------------------


def test_msgpack_by_default_and_json_on_request(internal: Internal, host: Host) -> None:
    host.seed_share()
    packed = internal.get("/v1/feed")
    assert packed.status == 200
    assert packed.header("Content-Type") == "application/msgpack"
    as_json = internal.get("/v1/feed", JSON)
    assert as_json.header("Content-Type") == "application/json"
    assert msgpack.unpackb(packed.body, raw=False) == json.loads(as_json.body)
    for r in (packed, as_json):
        assert r.header("Cache-Control") == "no-store"
        assert r.header("X-Content-Type-Options") == "nosniff"


def test_errors_follow_the_accept_header(internal: Internal) -> None:
    r = internal.get("/v1/nope", JSON)
    assert r.status == 404 and r.json() == {"error": "not_found"}
    assert msgpack.unpackb(internal.get("/v1/nope").body, raw=False) == {"error": "not_found"}


# -- feed, search, folders, items -------------------------------------------------------------------------


def test_operator_sees_every_members_artifacts(internal: Internal, host: Host) -> None:
    a, b = host.seed_share(), host.seed_share(intake="pending")
    body = internal.js("/v1/feed?sources=mine,pending")
    ids = {i["artifact_id"]: i for i in body["items"]}
    assert ids[a.artifact_id]["source"] == "mine" and ids[b.artifact_id]["source"] == "pending"
    assert body["total"] == 2 and body["pending"] == 1 and body["next_offset"] is None
    assert isinstance(ids[a.artifact_id]["shared_at"], str)


def test_feed_filters_and_paging_use_the_dashboard_parameters(
    internal: Internal, host: Host
) -> None:
    seeded = [host.seed_share() for _ in range(3)]
    host.execute(
        "UPDATE artifacts SET artifact_type='report' WHERE artifact_id=$1", seeded[0].artifact_id
    )
    host.execute(
        "UPDATE artifacts SET display_name='Zebra findings' WHERE artifact_id=$1",
        seeded[1].artifact_id,
    )
    page = internal.js("/v1/feed?limit=2")
    assert len(page["items"]) == 2 and page["total"] == 3 and page["next_offset"] == 2
    assert len(internal.js("/v1/feed?limit=2&offset=2")["items"]) == 1
    assert [i["artifact_id"] for i in internal.js("/v1/feed?type=report")["items"]] == [
        seeded[0].artifact_id
    ]
    assert [i["artifact_id"] for i in internal.js("/v1/feed?q=zebra")["items"]] == [
        seeded[1].artifact_id
    ]
    assert internal.js("/v1/feed?since=2999-01-01T00:00:00Z")["items"] == []
    assert internal.js("/v1/feed?until=2000-01-01T00:00:00Z")["total"] == 0


@pytest.mark.parametrize(
    "query",
    [
        "sources=bogus",
        "limit=abc",
        "since=yesterday",
        "since=2026-01-01T00:00:00",
        "q=a&q=b",
        "offset=x",
    ],
)
def test_bad_feed_parameters_are_400(internal: Internal, query: str) -> None:
    assert internal.get(f"/v1/feed?{query}").status == 400


def test_search_requires_q_and_includes_pending(internal: Internal, host: Host) -> None:
    s = host.seed_share(intake="pending")
    host.execute(
        "UPDATE artifacts SET display_name='Needle doc' WHERE artifact_id=$1", s.artifact_id
    )
    assert internal.get("/v1/search").status == 400
    found = internal.js("/v1/search?q=needle")
    assert [i["artifact_id"] for i in found["items"]] == [s.artifact_id]
    assert "folders" not in found


def test_dead_artifacts_are_never_listed_or_served(internal: Internal, host: Host) -> None:
    for lifecycle in ("retracted", "tombstoned"):
        s = host.seed_share(lifecycle=lifecycle)
        assert internal.js("/v1/feed")["items"] == []
        for path in (
            f"/v1/items/{s.artifact_id}",
            f"/v1/items/{s.artifact_id}/versions",
            f"/v1/content/{s.artifact_id}/{s.version_id}/index.html",
        ):
            assert internal.get(path).status == 404
        assert internal.js("/v1/shares")["shares"] == []
    d = host.seed_share(intake="discarded")
    assert internal.get(f"/v1/items/{d.artifact_id}").status == 404


def test_folders_and_folder_filter(internal: Internal, host: Host) -> None:
    s = host.seed_share()
    owner = host.rows("SELECT owner FROM artifacts WHERE artifact_id=$1", s.artifact_id)[0]["owner"]
    host.execute(
        "INSERT INTO library_folders (folder_id, owner, name) VALUES ('fld_aaaaaaaaaaaaaaaaaaaaaaaa',$1,'Reports')",
        owner,
    )
    host.execute(
        "INSERT INTO library_items (folder_id, artifact_id, source) VALUES ('fld_aaaaaaaaaaaaaaaaaaaaaaaa',$1,'manual')",
        s.artifact_id,
    )
    host.seed_share()
    folders = internal.js("/v1/folders")["folders"]
    assert folders == [
        {
            "folder_id": "fld_aaaaaaaaaaaaaaaaaaaaaaaa",
            "owner": owner,
            "name": "Reports",
            "item_count": 1,
        }
    ]
    assert internal.js(f"/v1/folders?owner={owner}")["folders"] == folders
    assert internal.js("/v1/folders?owner=nobody")["folders"] == []
    filtered = internal.js("/v1/feed?folder_id=fld_aaaaaaaaaaaaaaaaaaaaaaaa")
    assert [i["artifact_id"] for i in filtered["items"]] == [s.artifact_id]
    assert filtered["items"][0]["folder_ids"] == ["fld_aaaaaaaaaaaaaaaaaaaaaaaa"]
    assert internal.js("/v1/feed?folder_id=fld_bbbbbbbbbbbbbbbbbbbbbbbb")["items"] == []


def test_item_and_versions(internal: Internal, host: Host) -> None:
    s = host.seed_share()
    item = internal.js(f"/v1/items/{s.artifact_id}")["item"]
    assert item["artifact_id"] == s.artifact_id and item["share_count"] == 1
    assert item["intake"] == "accepted" and item["lifecycle"] == "active"
    host.execute(
        "UPDATE artifacts SET current_version_id=$2 WHERE artifact_id=$1",
        s.artifact_id,
        s.version_id,
    )
    item = internal.js(f"/v1/items/{s.artifact_id}")["item"]
    assert {f["path"] for f in item["files"]} == set(FILES)
    versions = internal.js(f"/v1/items/{s.artifact_id}/versions")["versions"]
    assert [v["version_id"] for v in versions] == [s.version_id] and versions[0]["current"] is True
    assert internal.get("/v1/items/missing").status == 404
    assert internal.get("/v1/items/missing/versions").status == 404
    assert internal.get("/v1/items/%2e%2e").status == 404


# -- shares -----------------------------------------------------------------------------------------------


def test_shares_are_redacted(internal: Internal, host: Host) -> None:
    s = host.seed_share(audience="lab", emails=("a@x.org",))
    host.execute("INSERT INTO agent_tokens (digest, member_id) VALUES ($1,'m1')", "f" * 64)
    digest = host.rows("SELECT token_hash FROM shares WHERE share_id=$1", s.share_id)[0][
        "token_hash"
    ]
    for accept in (AUTH, JSON):
        r = internal.get("/v1/shares", accept)
        assert r.status == 200
        blob = r.body
        for secret in (s.token.encode(), digest.encode(), b"f" * 64, b"token_hash", b"/s/"):
            assert secret not in blob
    row = internal.js("/v1/shares")["shares"][0]
    assert row["share_id"] == s.share_id and row["item"] == s.artifact_id
    assert row["audience"] == "lab" and row["state"] == "live" and row["active"] is True
    assert isinstance(row["expires_at"], str)
    assert not {k for k in row if "token" in k or "url" in k or "digest" in k}
    # a share URL or digest passed as a filter does not echo back either
    assert internal.js(f"/v1/shares?artifact_id={s.artifact_id}")["total"] == 1
    assert internal.js("/v1/shares?state=revoked")["shares"] == []
    assert internal.js("/v1/shares?audience=public")["shares"] == []
    assert internal.get("/v1/shares?state=bogus").status == 400


# -- content ----------------------------------------------------------------------------------------------


def test_content_is_served_sandboxed(internal: Internal, host: Host) -> None:
    s = host.seed_share()
    r = internal.get(f"/v1/content/{s.artifact_id}/{s.version_id}/index.html")
    assert r.status == 200 and r.body == FILES["index.html"]
    assert r.header("Content-Security-Policy") == CSP
    assert r.header("X-Content-Type-Options") == "nosniff"
    assert r.header("Cache-Control") == "no-store"
    assert r.header(
        "Content-Type",
    ) is not None and r.header("Content-Type").startswith("text/html")
    nested = internal.get(f"/v1/content/{s.artifact_id}/{s.version_id}/data/big.bin")
    assert nested.status == 200 and nested.body == FILES["data/big.bin"]
    assert nested.header("Content-Type") == "application/octet-stream"
    assert nested.header("Content-Security-Policy") == CSP


def test_content_type_ignores_stored_media_type(internal: Internal, host: Host) -> None:
    sha = host.put_blob(b"<script>1</script>")
    manifest = [
        {"name": "evil.bin", "sha256": sha, "size_bytes": 18, "media_type": "text/html,text/html"}
    ]
    s = host.seed_share({"x": b"x"})
    host.execute(
        "UPDATE artifact_versions SET output_manifest=$2::jsonb WHERE version_id=$1",
        s.version_id,
        json.dumps(manifest),
    )
    r = internal.get(f"/v1/content/{s.artifact_id}/{s.version_id}/evil.bin")
    assert r.status == 200 and r.header("Content-Type") == "application/octet-stream"
    assert (
        r.header("Content-Security-Policy") == CSP
        and r.header("X-Content-Type-Options") == "nosniff"
    )


@pytest.mark.parametrize(
    "tail",
    [
        "../index.html",
        "data/../index.html",
        "%2e%2e/index.html",
        "..%2findex.html",
        "data%2fbig.bin",
        "%2Fetc%2Fpasswd",
        "index.html%00",
        "nope.html",
        "data/",
        "",
        "index.html/",
        "%5c..%5cindex.html",
        "..%252findex.html",
    ],
)
def test_content_path_traversal_and_unknown_paths_are_404(
    internal: Internal, host: Host, tail: str
) -> None:
    s = host.seed_share()
    assert internal.get(f"/v1/content/{s.artifact_id}/{s.version_id}/{tail}").status == 404


def test_content_wrong_version_or_state_is_404(internal: Internal, host: Host) -> None:
    a, b = host.seed_share(), host.seed_share()
    assert internal.get(f"/v1/content/{a.artifact_id}/{b.version_id}/index.html").status == 404
    assert internal.get(f"/v1/content/{a.artifact_id}/missing/index.html").status == 404
    host.execute("UPDATE artifact_versions SET state='purged' WHERE version_id=$1", a.version_id)
    assert internal.get(f"/v1/content/{a.artifact_id}/{a.version_id}/index.html").status == 404


def test_content_with_a_corrupt_blob_is_404(internal: Internal, host: Host) -> None:
    s = host.seed_share()
    sha = host.rows(
        "SELECT output_manifest::text AS m FROM artifact_versions WHERE version_id=$1", s.version_id
    )
    entry = next(e for e in json.loads(sha[0]["m"]) if e["name"] == "notes.txt")
    path = BlobStore(host.blob_root)._path(entry["sha256"])
    path.write_bytes(b"tampered")
    assert internal.get(f"/v1/content/{s.artifact_id}/{s.version_id}/notes.txt").status == 404


# -- reads write nothing ----------------------------------------------------------------------------------


def test_a_full_read_sweep_writes_nothing(internal: Internal, host: Host) -> None:
    s = host.seed_share()
    p = host.seed_share(audience="public", intake="pending")
    host.execute("INSERT INTO share_claims (member_id, share_id) VALUES ('m1', $1)", p.share_id)
    host.execute(
        "INSERT INTO share_events (share_id, kind, actor) VALUES ($1,'created','x')", s.share_id
    )
    before = internal.counts()
    snapshot = {
        t: host.rows(f"SELECT * FROM {t}")
        for t in ("share_events", "share_claims", "shares", "member_usage")
    }
    for sd in (s, p):
        for path in (
            "/v1/feed?sources=mine,assigned,opened,pending",
            "/v1/folders",
            f"/v1/items/{sd.artifact_id}",
            f"/v1/items/{sd.artifact_id}/versions",
            "/v1/shares",
            "/v1/search?q=a",
            f"/v1/content/{sd.artifact_id}/{sd.version_id}/index.html",
            f"/v1/content/{sd.artifact_id}/{sd.version_id}/missing",
        ):
            internal.get(path)
    assert internal.counts() == before
    assert {t: host.rows(f"SELECT * FROM {t}") for t in snapshot} == snapshot


def test_read_only_pool_refuses_writes(migrated_dsn: str) -> None:
    async def go() -> None:
        pool = await asyncpg.create_pool(migrated_dsn, min_size=1, max_size=2)
        try:
            ro = ReadOnlyPool(pool)
            assert await ro.fetchval("SELECT 1") == 1
            with pytest.raises(asyncpg.ReadOnlySQLTransactionError):
                await ro.fetch(
                    "INSERT INTO artifact_blobs (sha256, size_bytes) VALUES ($1, 1) RETURNING 1",
                    "a" * 64,
                )
            with pytest.raises(asyncpg.ReadOnlySQLTransactionError):
                await ro.fetchrow("DELETE FROM artifacts RETURNING 1")
            assert await pool.fetchval("SELECT count(*) FROM artifact_blobs") == 0
        finally:
            await pool.close()

    asyncio.run(go())


# -- role boundary ----------------------------------------------------------------------------------------


def _concrete(template: str) -> tuple[str, str]:
    method, path = template.split(" ", 1)
    path = re.sub(r"\{[a-z]+\}", "x", path)
    return method, path


def test_public_routes_are_404_on_the_internal_listener(internal: Internal) -> None:
    for template in routes_for(Role.PUBLIC):
        method, path = _concrete(template)
        if method != "GET":
            continue
        r = internal.get(path)
        assert r.status == 404, template


def test_internal_routes_are_404_on_the_public_listener(internal: Internal, host: Host) -> None:
    for template in routes_for(Role.INTERNAL):
        method, path = _concrete(template)
        for headers in (None, AUTH):
            assert host.request(method, path, headers).status == 404, template


def test_route_tables_are_disjoint_and_complete() -> None:
    assert routes_for(Role.PUBLIC).isdisjoint(routes_for(Role.INTERNAL))
    assert {t.split(" ", 1)[1] for t in routes_for(Role.INTERNAL) if "/v1/" in t} == {
        "/v1/feed",
        "/v1/folders",
        "/v1/items/{id}",
        "/v1/items/{id}/versions",
        "/v1/shares",
        "/v1/search",
        "/v1/content/{item}/{version}/{path}",
    }
    assert all(t.startswith("GET ") for t in routes_for(Role.INTERNAL))


def test_healthz_needs_no_key(internal: Internal) -> None:
    assert internal.get("/internal/healthz", None).status == 200


# -- fix round 1 ------------------------------------------------------------------------------------------

CSP = "sandbox; default-src 'none'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; font-src 'self'"
HOSTILE = [
    "/v1/feed?offset=99999999999999999999",
    "/v1/feed?limit=99999999999999999999",
    "/v1/feed?offset=-99999999999999999999",
    "/v1/feed?since=0001-01-01T00:00:00%2B14:00",
    "/v1/feed?until=9999-12-31T23:59:59-14:00",
    "/v1/feed?since=9999-12-31T23:59:59-14:00",
    "/v1/feed?q=a%00b",
    "/v1/feed?type=a%00b",
    "/v1/feed?owner=a%00b",
    "/v1/feed?folder_id=a%00b",
    "/v1/search?q=a%00b",
    "/v1/search?q=a&owner=a%00b",
    "/v1/shares?artifact_id=a%00b",
    "/v1/shares?offset=99999999999999999999",
    "/v1/folders?owner=a%00b",
    "/v1/feed?sources=mine%00",
]


@pytest.mark.parametrize("path", HOSTILE)
def test_hostile_parameters_are_400_with_headers(internal: Internal, path: str) -> None:
    r = internal.get(path)
    assert r.status in (200, 400), path
    assert (
        r.header("X-Content-Type-Options") == "nosniff" and r.header("Cache-Control") == "no-store"
    )
    if "%00" in path or "9999-12-31" in path or "0001-01-01" in path:
        assert r.status == 400, path


def test_unexpected_errors_are_encoded_with_headers(host: Host) -> None:
    server = _start(host, InternalConfig(key_digest(KEY), key_failures_per_minute=10_000))
    try:
        api = Internal(host, server.port)
        assert host.loop is not None
        surface = server.app[
            __import__("herdr_artifact_host.internal_server", fromlist=["SURFACE"]).SURFACE
        ]

        async def boom(*_a: object, **_k: object) -> None:
            raise RuntimeError("secret-param-value")

        surface.route = boom  # a real handler fault, not a double of the database
        r = api.get("/v1/feed", JSON)
        assert r.status == 500 and r.json() == {"error": "internal_error"}
        assert (
            r.header("X-Content-Type-Options") == "nosniff"
            and r.header("Cache-Control") == "no-store"
        )
        assert b"secret" not in r.body
    finally:
        assert host.loop is not None
        asyncio.run_coroutine_threadsafe(server.stop(), host.loop).result(30)


def test_csp_on_every_response(internal: Internal, host: Host) -> None:
    s = host.seed_share()
    for path, status in (
        (f"/v1/content/{s.artifact_id}/{s.version_id}/index.html", 200),
        (f"/v1/content/{s.artifact_id}/{s.version_id}/nope.html", 404),
        (f"/v1/content/{s.artifact_id}/{s.version_id}/../x", 404),
        ("/v1/feed?limit=x", 400),
    ):
        r = internal.get(path)
        assert r.status == status, path
        assert r.header("Content-Security-Policy") == CSP, path
    assert internal.get("/v1/feed", None).header("Content-Security-Policy") == CSP
    assert "allow-same-origin" not in CSP


def test_owner_filters_items_and_folder_ids_are_real(internal: Internal, host: Host) -> None:
    a, b = host.seed_share(), host.seed_share()
    owners = {
        x.artifact_id: host.rows("SELECT owner FROM artifacts WHERE artifact_id=$1", x.artifact_id)[
            0
        ]["owner"]
        for x in (a, b)
    }
    for i, x in enumerate((a, b)):
        fid = f"fld_{str(i) * 24}"
        host.execute(
            "INSERT INTO library_folders (folder_id, owner, name) VALUES ($1,$2,$3)",
            fid,
            owners[x.artifact_id],
            f"F{i}",
        )
        host.execute(
            "INSERT INTO library_items (folder_id, artifact_id, source) VALUES ($1,$2,'manual')",
            fid,
            x.artifact_id,
        )
    everyone = {i["artifact_id"]: i for i in internal.js("/v1/feed")["items"]}
    assert everyone[a.artifact_id]["folder_ids"] == ["fld_" + "0" * 24]
    assert everyone[b.artifact_id]["folder_ids"] == ["fld_" + "1" * 24]
    mine = internal.js(f"/v1/feed?owner={owners[a.artifact_id]}")
    assert [i["artifact_id"] for i in mine["items"]] == [a.artifact_id] and mine["total"] == 1
    assert [
        i["artifact_id"]
        for i in internal.js(f"/v1/search?q=a&owner={owners[b.artifact_id]}")["items"]
    ] == [b.artifact_id]
    assert internal.js("/v1/feed?owner=nobody")["items"] == []
    assert [
        i["artifact_id"] for i in internal.js("/v1/feed?folder_id=fld_" + "1" * 24)["items"]
    ] == [b.artifact_id]


def test_empty_string_digest_is_refused() -> None:
    import hashlib

    with pytest.raises(ValueError):
        InternalConfig(hashlib.sha256(b"").hexdigest())


def test_empty_bearer_never_authenticates(internal: Internal) -> None:
    for value in ("Bearer", "Bearer ", "Bearer  "):
        assert internal.get("/v1/feed", {"Authorization": value}).status == 401


def _from(address: str, port: int, headers: dict[str, str] | None = None) -> int:
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10, source_address=(address, 0))
    try:
        conn.request("GET", "/v1/feed", headers=headers or {})
        return conn.getresponse().status
    finally:
        conn.close()


def test_limiter_ignores_forwarded_for_and_has_a_global_backstop(host: Host) -> None:
    server = _start(
        host,
        InternalConfig(
            key_digest(KEY), key_failures_per_minute=2, key_failures_global_per_minute=4
        ),
    )
    try:
        # rotating X-Forwarded-For buys no fresh bucket: the socket peer is the key
        assert [
            _from("127.0.0.2", server.port, {"X-Forwarded-For": f"10.0.0.{i}"}) for i in range(3)
        ] == [401, 401, 429]
        # distinct peers each have their own bucket, but the global backstop stops a distributed guesser
        assert _from("127.0.0.3", server.port) == 401
        assert _from("127.0.0.4", server.port) == 401
        assert _from("127.0.0.5", server.port) == 429
        # a good key from a fresh peer is not locked out by the backstop
        assert _from("127.0.0.6", server.port, AUTH) == 200
    finally:
        assert host.loop is not None
        asyncio.run_coroutine_threadsafe(server.stop(), host.loop).result(30)


def test_query_is_decoded_once(internal: Internal, host: Host) -> None:
    s = host.seed_share()
    host.execute("UPDATE artifacts SET display_name='x%41y' WHERE artifact_id=$1", s.artifact_id)
    assert [i["artifact_id"] for i in internal.js("/v1/feed?q=x%2541y")["items"]] == [s.artifact_id]
    assert internal.js("/v1/feed?q=xAy")["items"] == []


def test_get_with_a_body_is_refused(internal: Internal) -> None:
    r = internal.req("GET", "/v1/feed", AUTH, b"hello")
    assert r.status == 400
    assert internal.req("GET", "/v1/feed", {**AUTH, "Content-Length": "0"}).status == 200


def test_item_lists_files_only_for_a_ready_head(internal: Internal, host: Host) -> None:
    s = host.seed_share()
    host.execute(
        "UPDATE artifacts SET current_version_id=$2 WHERE artifact_id=$1",
        s.artifact_id,
        s.version_id,
    )
    assert internal.js(f"/v1/items/{s.artifact_id}")["item"]["files"] != []
    host.execute("UPDATE artifact_versions SET state='purged' WHERE version_id=$1", s.version_id)
    assert internal.js(f"/v1/items/{s.artifact_id}")["item"]["files"] == []


def test_healthz_is_rate_limited_and_touches_no_table(host: Host) -> None:
    server = _start(host, InternalConfig(key_digest(KEY), healthz_per_minute=3))
    try:
        api = Internal(host, server.port)
        assert [api.get("/internal/healthz", None).status for _ in range(4)] == [200, 200, 200, 429]
    finally:
        assert host.loop is not None
        asyncio.run_coroutine_threadsafe(server.stop(), host.loop).result(30)
