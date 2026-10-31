"""Owner routes for collaborators, activity and version diffs over real HTTP, on a real PostgreSQL (all migrations)."""
import asyncio
import hashlib
import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from herdr_engineering.control_plane import artifacts as artifacts_module
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.artifact_acl import ArtifactAcl
from herdr_engineering.control_plane.artifact_adapter import SyncArtifactAdapter
from herdr_engineering.control_plane.artifact_bundle import ArtifactBundle
from herdr_engineering.control_plane.artifact_collab_store import CollabGrantStore
from herdr_engineering.control_plane.artifact_scope import ArtifactGraphGate
from herdr_engineering.control_plane.artifacts import ArtifactStore
from herdr_engineering.control_plane.collab_bridge_files import BridgeLimits
from herdr_engineering.control_plane.collab_mirror import CollabMirror
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.registry_pg import FakeBlobs, registry_pg, uid  # noqa: F401 - imported fixture

MIB = 1024 * 1024
OWNER = "api-key:" + hashlib.sha256(b"owner-key").hexdigest()[:16] + ":operator"
OTHER_KEY = "other-key"


class CountingBlobs(FakeBlobs):
    def __init__(self):
        super().__init__()
        self.read = []

    def iter_get(self, key, expected_sha256, *, chunk_size=1 << 20):
        self.read.append(key)
        return super().iter_get(key, expected_sha256, chunk_size=chunk_size)


def entry(blobs, name, data, media_type="text/markdown", size=None):
    key, digest = blobs.put(data)
    return {"name": name, "media_type": media_type, "sha256": digest, "storage_key": key,
            "size_bytes": len(data) if size is None else size}


def run(pg, blobs, body):
    async def go():
        conn = await asyncpg.connect(pg["dsn"])
        try:
            return await body(ArtifactStore(conn, blobs), conn)
        finally:
            await conn.close()
    return asyncio.run(go())


async def new_artifact(store, conn, files, *, created_by=OWNER, artifact_type=None):
    kwargs = {"artifact_type": artifact_type} if artifact_type else {}
    artifact = await store.register_logical(original_filename="doc.md", created_by=created_by, **kwargs)
    aid = artifact["artifact_id"]
    if files is not None:
        await store.commit_version(aid, base_version_id=None, source_manifest_blob=None, output_manifest=files,
                                   created_by=created_by)
    return aid


@contextmanager
def collab_server(pg, blobs, *, enabled=True, with_store=True):
    async def make_registry():
        return ArtifactStore(await asyncpg.connect(pg["dsn"]), blobs)

    async def make_grants():
        return CollabGrantStore(await asyncpg.connect(pg["dsn"]))

    registry = SyncArtifactAdapter(make_registry)
    grants = SyncArtifactAdapter(make_grants) if with_store else None
    acl = ArtifactAcl()
    server = ControlPlaneServer(ControlPlaneRepo(), api_keys={"owner-key": "operator", OTHER_KEY: "operator"},
                                artifact_store=registry, artifact_acl=acl, collab_grant_store=grants,
                                collab_artifacts_enabled=enabled)
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    server.wait_until_ready()
    try:
        yield f"http://127.0.0.1:{server.port}", server, acl
    finally:
        server.shutdown()
        registry.close()
        if grants is not None:
            grants.close()


@pytest.fixture
def blobs():
    return CountingBlobs()


@pytest.fixture
def env(registry_pg, blobs):
    with collab_server(registry_pg, blobs) as value:
        yield value


def request(base, path, *, key="owner-key", body=None, headers=None, method=None):
    headers = {"X-API-Key": key, **(headers or {})}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(base + path, headers=headers, data=data, method=method)
    try:
        with urllib.request.urlopen(req) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        raw = error.read()
        return error.code, json.loads(raw) if raw else None


def seed(pg, blobs, files="default", **kwargs):
    if files == "default":
        files = [entry(blobs, "doc.md", b"hello\n")]

    async def body(store, conn):
        return await new_artifact(store, conn, files, **kwargs)
    return run(pg, blobs, body)


def sql(pg, blobs, query, *args):
    async def body(store, conn):
        return await conn.fetch(query, *args)
    return run(pg, blobs, body)


def acl_rows(pg, blobs, aid):
    return sql(pg, blobs, "SELECT principal_id, permissions, expires_at FROM artifact_acl "
                          "WHERE artifact_id=$1 AND principal_id LIKE 'collab:%' ORDER BY principal_id", aid)


# ------------------------------------------------------------------------------------------------ wiring / gate


def test_wiring_is_off_unless_enabled_and_needs_a_dsn(monkeypatch):
    from herdr_engineering import cli
    assert cli._collab_grant_wiring({}, "postgresql:///x") == (None, False)
    assert cli._collab_grant_wiring({"collab_artifacts": {"enabled": False}}, "postgresql:///x") == (None, False)
    assert cli._collab_grant_wiring({"collab_artifacts": {"enabled": "true"}}, "postgresql:///x") == (None, False)
    assert cli._collab_grant_wiring({"collab_artifacts": {"enabled": True}}, None) == (None, False)
    made = []

    class FakeAdapter:
        def __init__(self, factory):
            made.append(factory)

    monkeypatch.setattr("herdr_engineering.control_plane.artifact_adapter.SyncArtifactAdapter", FakeAdapter)
    adapter, enabled = cli._collab_grant_wiring({"collab_artifacts": {"enabled": True}}, "postgresql:///x")
    assert isinstance(adapter, FakeAdapter) and enabled is True and len(made) == 1


def test_wiring_adapter_serves_a_real_grant_store(registry_pg, monkeypatch):
    from herdr_engineering import cli
    blobs = CountingBlobs()
    aid = seed(registry_pg, blobs)
    adapter, enabled = cli._collab_grant_wiring({"collab_artifacts": {"enabled": True}}, registry_pg["dsn"])
    try:
        assert enabled and adapter("collaborators", aid) == []
    finally:
        adapter.close()


ALL_ROUTES = [
    ("GET", "/collaborators", None),
    ("POST", "/collaborators", {"agent_id": "a1", "permission": "read"}),
    ("POST", "/collaborators/a1/revoke", {}),
    ("POST", "/collaborators/invite-grant", {"invite_id": "i1", "permission": "read"}),
    ("GET", "/collab-activity", None),
    ("GET", "/versions/v1/diff", None),
    ("POST", "/collab-restrict", {}),
]


@pytest.mark.parametrize("kwargs", [{"enabled": False}, {"with_store": False}])
def test_every_route_is_503_when_disabled_before_the_store_is_touched(registry_pg, blobs, kwargs):
    aid = seed(registry_pg, blobs)
    with collab_server(registry_pg, blobs, **kwargs) as (base, server, _):
        for method, tail, body in ALL_ROUTES:
            status, data = request(base, f"/api/v1/artifacts/{aid}{tail}", body=body, method=method)
            assert status == 503 and data == {"error": "collaboration is not enabled"}, (method, tail)
            # even a caller with no rights and an unknown artifact learn only "disabled"
            assert request(base, f"/api/v1/artifacts/nope{tail}", key=OTHER_KEY, body=body, method=method)[0] == 503


# ------------------------------------------------------------------------------------------------ access


def test_non_admin_and_unknown_artifact_get_the_same_404_on_every_route(env, registry_pg, blobs):
    base, server, acl = env
    aid = seed(registry_pg, blobs)
    for method, tail, body in ALL_ROUTES:
        denied = request(base, f"/api/v1/artifacts/{aid}{tail}", key=OTHER_KEY, body=body, method=method)
        unknown = request(base, f"/api/v1/artifacts/missing-artifact{tail}", body=body, method=method)
        assert denied[0] == 404 == unknown[0], (method, tail)
        assert denied[1] == unknown[1] == {"error": "not found"}


def test_read_only_grant_cannot_administer_but_can_diff(env, registry_pg, blobs):
    base, server, _ = env
    aid = seed(registry_pg, blobs)
    other = "api-key:" + hashlib.sha256(OTHER_KEY.encode()).hexdigest()[:16] + ":operator"
    sql(pg := registry_pg, blobs, "INSERT INTO artifact_acl (artifact_id, principal_id, permissions, granted_by) "
                                  "VALUES ($1,$2,'[\"read\"]'::jsonb,'o')", aid, other)
    assert request(base, f"/api/v1/artifacts/{aid}/collaborators", key=OTHER_KEY)[0] == 404
    version = sql(pg, blobs, "SELECT version_id FROM artifact_versions WHERE artifact_id=$1", aid)[0]["version_id"]
    assert request(base, f"/api/v1/artifacts/{aid}/versions/{version}/diff", key=OTHER_KEY)[0] == 200


def test_cross_origin_post_is_403_and_same_origin_is_allowed(env, registry_pg, blobs):
    base, server, _ = env
    aid = seed(registry_pg, blobs)
    body = {"agent_id": "a1", "permission": "read"}
    for tail, payload in (("/collaborators", body), ("/collaborators/a1/revoke", {}),
                          ("/collaborators/invite-grant", {"invite_id": "i1", "permission": "read"}),
                          ("/collab-restrict", {})):
        status, _ = request(base, f"/api/v1/artifacts/{aid}{tail}", body=payload,
                            headers={"Origin": "https://evil.example"})
        assert status == 403, tail
    assert not acl_rows(registry_pg, blobs, aid)
    same = {"Origin": base}
    assert request(base, f"/api/v1/artifacts/{aid}/collaborators", body=body, headers=same)[0] == 201


# ------------------------------------------------------------------------------------------------ grants


def test_grant_writes_collab_row_and_leaves_the_web_acl_alone(env, registry_pg, blobs):
    base, server, acl = env
    aid = seed(registry_pg, blobs)
    before = dict(acl._grants), {k: set(v) for k, v in acl._holders.items()}
    status, data = request(base, f"/api/v1/artifacts/{aid}/collaborators",
                           body={"agent_id": "agent:one.2-x", "permission": "read", "expires_in_days": 7})
    assert status == 201, data
    assert data["agent_id"] == "agent:one.2-x" and data["permission"] == "read"
    rows = acl_rows(registry_pg, blobs, aid)
    assert [r["principal_id"] for r in rows] == ["collab:agent:one.2-x"]
    assert json.loads(rows[0]["permissions"]) == ["read"]
    delta = rows[0]["expires_at"] - datetime.now(UTC)
    assert timedelta(days=6, hours=23) < delta <= timedelta(days=7)
    assert (dict(acl._grants), {k: set(v) for k, v in acl._holders.items()}) == before
    assert not acl.has_grants(aid)


def test_default_expiry_is_30_days_and_the_listing_shows_it(env, registry_pg, blobs):
    base, server, _ = env
    aid = seed(registry_pg, blobs)
    assert request(base, f"/api/v1/artifacts/{aid}/collaborators", body={"agent_id": "a", "permission": "read"})[0] == 201
    status, listing = request(base, f"/api/v1/artifacts/{aid}/collaborators")
    assert status == 200 and listing["writable"] is True and listing["writable_reason"] is None
    assert listing["pending_invites"] == []
    [row] = listing["collaborators"]
    assert row["agent_id"] == "a" and row["permission"] == "read" and row["commits"] == 0
    expires = datetime.fromisoformat(row["expires_at"])
    assert timedelta(days=29, hours=23) < expires - datetime.now(UTC) <= timedelta(days=30)


def test_internal_and_external_agents_can_both_be_granted_write(env, registry_pg, blobs):
    base, server, _ = env
    aid = seed(registry_pg, blobs)
    for agent in ("agent:internal.1", "ext-guest_7"):
        status, data = request(base, f"/api/v1/artifacts/{aid}/collaborators",
                               body={"agent_id": agent, "permission": "write"})
        assert status == 201 and data["permission"] == "write", data
    rows = acl_rows(registry_pg, blobs, aid)
    assert all(json.loads(r["permissions"]) == ["read", "edit"] for r in rows) and len(rows) == 2


def test_write_on_a_report_is_409_with_the_writable_reason(env, registry_pg, blobs):
    base, server, _ = env
    aid = seed(registry_pg, blobs, files=None, artifact_type="report")
    status, data = request(base, f"/api/v1/artifacts/{aid}/collaborators",
                           body={"agent_id": "a1", "permission": "write"})
    assert status == 409 and data["error"] == data["writable_reason"] == \
        "report artifacts are read-only for collaborators"
    assert not acl_rows(registry_pg, blobs, aid)
    assert request(base, f"/api/v1/artifacts/{aid}/collaborators",
                   body={"agent_id": "a1", "permission": "read"})[0] == 201      # read is fine on a report
    status, listing = request(base, f"/api/v1/artifacts/{aid}/collaborators")
    assert listing["writable"] is False and listing["writable_reason"] == data["writable_reason"]


def test_artifact_without_an_owner_is_409_legacy(env, registry_pg, blobs):
    base, server, _ = env
    aid = seed(registry_pg, blobs, created_by=None)
    sql(registry_pg, blobs, "INSERT INTO artifact_acl (artifact_id, principal_id, permissions, granted_by) "
                            "VALUES ($1,$2,'[\"admin\"]'::jsonb,'o')", aid, OWNER)
    for tail, body in (("/collaborators", {"agent_id": "a1", "permission": "read"}),
                       ("/collaborators/invite-grant", {"invite_id": "i1", "permission": "read"})):
        status, data = request(base, f"/api/v1/artifacts/{aid}{tail}", body=body)
        assert status == 409 and data["error"] == "legacy artifact: set an owner first", tail
    assert not acl_rows(registry_pg, blobs, aid)


@pytest.mark.parametrize("body", [
    {"agent_id": "a1", "permission": "read", "expires_in_days": 91},
    {"agent_id": "a1", "permission": "read", "expires_in_days": 0},
    {"agent_id": "a1", "permission": "read", "expires_in_days": True},
    {"agent_id": "a1", "permission": "read", "expires_in_days": 1.5},
    {"agent_id": "a1", "permission": "read", "expires_in_days": "7"},
    {"agent_id": "a1", "permission": "read", "extra": 1},
    {"agent_id": "a1", "permission": "admin"},
    {"agent_id": "a1"},
    {"permission": "read"},
    {"agent_id": "", "permission": "read"},
    {"agent_id": "x" * 81, "permission": "read"},
    {"agent_id": "has space", "permission": "read"},
    {"agent_id": "bad/slash", "permission": "read"},
    {"agent_id": "nl\nx", "permission": "read"},
    {"agent_id": 5, "permission": "read"},
    {"agent_id": "a1", "permission": ["read"]},
])
def test_grant_body_is_validated_strictly(env, registry_pg, blobs, body):
    base, server, _ = env
    aid = seed(registry_pg, blobs)
    status, data = request(base, f"/api/v1/artifacts/{aid}/collaborators", body=body)
    assert status == 400, data
    assert not acl_rows(registry_pg, blobs, aid)


def test_grant_bounds_accept_the_edges(env, registry_pg, blobs):
    base, server, _ = env
    aid = seed(registry_pg, blobs)
    for days, agent in ((1, "a" * 80), (90, "b")):
        status, data = request(base, f"/api/v1/artifacts/{aid}/collaborators",
                               body={"agent_id": agent, "permission": "read", "expires_in_days": days})
        assert status == 201, data


def test_non_json_content_type_and_non_object_body_are_400(env, registry_pg, blobs):
    base, server, _ = env
    aid = seed(registry_pg, blobs)
    req = urllib.request.Request(f"{base}/api/v1/artifacts/{aid}/collaborators", method="POST",
                                 data=b'{"agent_id":"a","permission":"read"}',
                                 headers={"X-API-Key": "owner-key", "Content-Type": "text/plain"})
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(req)
    assert caught.value.code == 400
    assert request(base, f"/api/v1/artifacts/{aid}/collaborators", body=["x"])[0] == 400


def test_revoke_removes_the_row_and_unknown_agent_is_still_200(env, registry_pg, blobs):
    base, server, _ = env
    aid = seed(registry_pg, blobs)
    request(base, f"/api/v1/artifacts/{aid}/collaborators", body={"agent_id": "a1", "permission": "write"})
    request(base, f"/api/v1/artifacts/{aid}/collaborators", body={"agent_id": "a2", "permission": "read"})
    status, data = request(base, f"/api/v1/artifacts/{aid}/collaborators/a1/revoke", body={})
    assert status == 200 and data == {"agent_id": "a1", "revoked": True}
    assert [r["principal_id"] for r in acl_rows(registry_pg, blobs, aid)] == ["collab:a2"]
    status, data = request(base, f"/api/v1/artifacts/{aid}/collaborators/a1/revoke", body={})
    assert status == 200 and data["revoked"] is False
    # an empty POST body is fine for revoke; an unknown key is not
    assert request(base, f"/api/v1/artifacts/{aid}/collaborators/a2/revoke", method="POST")[0] == 200
    assert request(base, f"/api/v1/artifacts/{aid}/collaborators/a2/revoke", body={"x": 1})[0] == 400
    assert request(base, f"/api/v1/artifacts/{aid}/collaborators/bad%20id/revoke", body={})[0] == 400


def test_invite_grant_stores_a_pending_grant_and_validates(env, registry_pg, blobs):
    base, server, _ = env
    aid = seed(registry_pg, blobs)
    path = f"/api/v1/artifacts/{aid}/collaborators/invite-grant"
    status, data = request(base, path, body={"invite_id": "inv-1", "permission": "write", "expires_in_days": 3})
    assert status == 201, data
    assert data["invite_id"] == "inv-1" and data["permission"] == "write"
    status, listing = request(base, f"/api/v1/artifacts/{aid}/collaborators")
    [pending] = listing["pending_invites"]
    assert pending["invite_id"] == "inv-1" and pending["permission"] == "write"
    assert listing["collaborators"] == []
    for bad in ({"invite_id": "i", "permission": "read", "expires_in_days": 91},
                {"invite_id": "i", "permission": "read", "extra": 1},
                {"invite_id": "", "permission": "read"},
                {"invite_id": "x y", "permission": "read"},
                {"invite_id": "i", "permission": "root"},
                {"permission": "read"}):
        assert request(base, path, body=bad)[0] == 400, bad
    # write on a non-writable artifact is refused here too
    report = seed(registry_pg, blobs, files=None, artifact_type="report")
    status, data = request(base, f"/api/v1/artifacts/{report}/collaborators/invite-grant",
                           body={"invite_id": "inv-2", "permission": "write"})
    assert status == 409 and data["writable_reason"]


# ------------------------------------------------------------------------------------------------ activity


def test_activity_lists_a_conflict_with_its_names(env, registry_pg, blobs):
    base, server, _ = env
    aid = seed(registry_pg, blobs)

    async def body(store, conn):
        await conn.execute(
            "INSERT INTO artifact_collab_intents (ticket_id, kind, actor_kind, actor_id, artifact_id, outcome) "
            "VALUES ('t-1','commit','agent','a1',$1,'conflict')", aid)
        await conn.execute(
            "INSERT INTO artifact_collab_attempts (attempt_id, artifact_id, collaborator_agent_id, ticket_id, "
            "conflict_names) VALUES ('att-1',$1,'a1','t-1','[\"doc.md\",\"notes/x.md\"]'::jsonb)", aid)
    run(registry_pg, blobs, body)
    status, data = request(base, f"/api/v1/artifacts/{aid}/collab-activity")
    assert status == 200
    [attempt] = data["attempts"]
    assert attempt["outcome"] == "conflict" and attempt["conflict_names"] == ["doc.md", "notes/x.md"]
    status, listing = request(base, f"/api/v1/artifacts/{aid}/collaborators")
    assert status == 200
    assert request(base, f"/api/v1/artifacts/{aid}/collab-activity", method="POST", body={})[0] == 404


# ------------------------------------------------------------------------------------------------ diffs / summaries


def two_versions(pg, blobs, old_files, new_files):
    async def body(store, conn):
        aid = await new_artifact(store, conn, old_files)
        head = (await store.list_version_summaries(aid))[0]["version_id"]
        second = await store.commit_version(aid, base_version_id=head, source_manifest_blob=None,
                                            output_manifest=new_files, created_by=OWNER)
        return aid, head, second["version_id"]
    return run(pg, blobs, body)


def diff_of(base, aid, vid, **kwargs):
    return request(base, f"/api/v1/artifacts/{aid}/versions/{vid}/diff", **kwargs)


def test_diff_covers_added_deleted_modified_unchanged_and_matches_artifact_bundle(env, registry_pg, blobs):
    base, server, _ = env
    old_text, new_text = b"one\ntwo\nthree\n", b"one\n2\nthree\n"
    same = entry(blobs, "same.md", b"same\n")
    aid, first, second = two_versions(
        registry_pg, blobs,
        [entry(blobs, "mod.md", old_text), same, entry(blobs, "gone.md", b"bye\n")],
        [entry(blobs, "mod.md", new_text), same, entry(blobs, "new.md", b"fresh\n")])
    status, data = diff_of(base, aid, second)
    assert status == 200 and data["version_id"] == second and data["against"] == first
    files = {f["name"]: f for f in data["files"]}
    assert {n: f["change"] for n, f in files.items()} == {"mod.md": "modified", "same.md": "unchanged",
                                                         "gone.md": "deleted", "new.md": "added"}
    assert files["mod.md"]["diff"] == ArtifactBundle.diff(old_text, new_text, media_type="text/markdown")
    assert files["new.md"]["diff"] == ArtifactBundle.diff(b"", b"fresh\n", media_type="text/markdown")
    assert files["gone.md"]["diff"] == ArtifactBundle.diff(b"bye\n", b"", media_type="text/markdown")
    assert files["same.md"]["diff"] is None and "reason" not in files["same.md"]
    assert [f["name"] for f in data["files"]] == sorted(files)
    assert "-two" in files["mod.md"]["diff"] and "+2" in files["mod.md"]["diff"]


def test_first_version_diffs_against_nothing(env, registry_pg, blobs):
    base, server, _ = env
    aid = seed(registry_pg, blobs)
    version = sql(registry_pg, blobs, "SELECT version_id FROM artifact_versions WHERE artifact_id=$1", aid)[0][0]
    status, data = diff_of(base, aid, version)
    assert status == 200 and data["against"] is None
    assert [(f["name"], f["change"]) for f in data["files"]] == [("doc.md", "added")]


def test_merge_version_is_diffed_against_its_first_parent(env, registry_pg, blobs):
    base, server, _ = env
    v1_text, v2_text, v3_text = b"a\nb\n", b"a\nb\nc\n", b"a\nb\nc\nd\n"

    async def body(store, conn):
        aid = await new_artifact(store, conn, [entry(blobs, "doc.md", v1_text)])
        v1 = (await store.list_version_summaries(aid))[0]["version_id"]
        v2 = (await store.commit_version(aid, base_version_id=v1, source_manifest_blob=None,
                                         output_manifest=[entry(blobs, "doc.md", v2_text)],
                                         created_by=OWNER))["version_id"]
        v3 = (await store.commit_version(aid, base_version_id=v2, parent_version_ids=[v2, v1],
                                         source_manifest_blob=None, created_via="collaborator",
                                         collaborator_agent_id="agent-1",
                                         output_manifest=[entry(blobs, "doc.md", v3_text)],
                                         created_by=OWNER))["version_id"]
        return aid, v1, v2, v3
    aid, v1, v2, v3 = run(registry_pg, blobs, body)
    status, data = diff_of(base, aid, v3)
    assert status == 200 and data["against"] == v2
    [file] = data["files"]
    assert file["diff"] == ArtifactBundle.diff(v2_text, v3_text, media_type="text/markdown")
    assert "+d" in file["diff"] and "+c" not in file["diff"]       # against v1 the diff would also add "c"


def test_binary_file_gets_reason_binary_and_no_bytes_are_read(env, registry_pg, blobs):
    base, server, _ = env
    png_old, png_new = b"\x89PNG\r\n\x1a\n\x00\x01", b"\x89PNG\r\n\x1a\n\x00\x02"
    aid, _, second = two_versions(
        registry_pg, blobs,
        [entry(blobs, "pic.png", png_old, "image/png"), entry(blobs, "nul.md", b"a\n")],
        [entry(blobs, "pic.png", png_new, "image/png"), entry(blobs, "nul.md", b"a\x00b\n")])
    blobs.read.clear()
    status, data = diff_of(base, aid, second)
    files = {f["name"]: f for f in data["files"]}
    assert files["pic.png"] == {"name": "pic.png", "change": "modified", "diff": None, "reason": "binary"}
    assert files["nul.md"]["reason"] == "binary" and files["nul.md"]["diff"] is None   # NUL byte in "text"
    assert not any(png_old_key in blobs.read for png_old_key in
                   (blobs.key(hashlib.sha256(png_old).hexdigest()), blobs.key(hashlib.sha256(png_new).hexdigest())))


def test_hostile_file_names_round_trip_as_json_data(env, registry_pg, blobs):
    base, server, _ = env
    names = ['q"uote\'s.md', "new\nline.md", "<script>alert(1)</script>.md", "ünï‮code.md", "../../etc/passwd",
             "</pre><img src=x onerror=1>.md"]
    aid, _, second = two_versions(registry_pg, blobs, [entry(blobs, "old.md", b"x\n")],
                                  [entry(blobs, n, b"y\n") for n in names])
    req = urllib.request.Request(f"{base}/api/v1/artifacts/{aid}/versions/{second}/diff",
                                 headers={"X-API-Key": "owner-key"})
    with urllib.request.urlopen(req) as response:
        assert response.headers["Content-Type"] == "application/json"
        data = json.loads(response.read())
    got = {f["name"]: f for f in data["files"]}
    assert set(names) <= set(got) and got["old.md"]["change"] == "deleted"
    for name in names:
        assert got[name]["change"] == "added" and "y" in got[name]["diff"]


def test_files_over_2_mib_on_either_side_are_oversize_and_never_read(env, registry_pg, blobs):
    base, server, _ = env
    big = b"x\n" * (MIB + 1)                       # 2 MiB + 2 bytes
    just_ok = b"y" * (2 * MIB - 1) + b"\n"
    aid, _, second = two_versions(
        registry_pg, blobs,
        [entry(blobs, "big-old.md", big), entry(blobs, "big-new.md", b"small\n"), entry(blobs, "ok.md", b"z\n")],
        [entry(blobs, "big-old.md", b"small\n"), entry(blobs, "big-new.md", big), entry(blobs, "ok.md", just_ok)])
    blobs.read.clear()
    status, data = diff_of(base, aid, second)
    files = {f["name"]: f for f in data["files"]}
    assert files["big-old.md"]["reason"] == "oversize" and files["big-old.md"]["diff"] is None
    assert files["big-new.md"]["reason"] == "oversize" and files["big-new.md"]["diff"] is None
    assert "reason" not in files["ok.md"] and files["ok.md"]["diff"]
    assert blobs.key(hashlib.sha256(big).hexdigest()) not in blobs.read


def test_a_manifest_that_lies_about_size_cannot_make_the_server_buffer_the_blob(env, registry_pg, blobs):
    base, server, _ = env
    big = b"line\n" * (MIB)                        # 5 MiB, claimed to be 10 bytes
    chunk_sizes = []
    original = blobs.iter_get

    def spy(key, sha, *, chunk_size=1 << 20):
        for chunk in original(key, sha, chunk_size=chunk_size):
            chunk_sizes.append(len(chunk))
            yield chunk
    blobs.iter_get = spy
    aid, _, second = two_versions(registry_pg, blobs, [entry(blobs, "a.md", b"a\n")],
                                  [entry(blobs, "a.md", big, size=10)])
    status, data = diff_of(base, aid, second)
    assert status == 200 and data["files"][0]["reason"] == "oversize" and data["files"][0]["diff"] is None
    assert sum(chunk_sizes) < 4 * MIB          # stopped one chunk past the 2 MiB cap, not after the full 5 MiB


def test_diff_text_stops_at_the_response_budget_and_the_rest_are_oversize(env, registry_pg, blobs, monkeypatch):
    base, server, _ = env
    monkeypatch.setattr(artifacts_module, "MAX_DIFF_RESPONSE_BYTES", 600)
    olds = [entry(blobs, f"f{i}.md", f"old {i}\n".encode() * 3) for i in range(6)]
    news = [entry(blobs, f"f{i}.md", f"new line {i}\n".encode() * 40) for i in range(6)]
    aid, _, second = two_versions(registry_pg, blobs, olds, news)
    status, data = diff_of(base, aid, second)
    assert status == 200
    done = [f for f in data["files"] if f["diff"] is not None]
    skipped = [f for f in data["files"] if f["diff"] is None]
    assert done and skipped and len(done) + len(skipped) == 6
    assert [f["name"] for f in done] == [f["name"] for f in data["files"]][:len(done)]   # budget is spent in order
    assert all(f["reason"] == "oversize" for f in skipped)
    assert sum(len(f["diff"].encode()) for f in done[:-1]) < 600       # a file starts only while under budget


def test_diff_unknown_version_and_foreign_version_are_404(env, registry_pg, blobs):
    base, server, _ = env
    aid = seed(registry_pg, blobs)
    other = seed(registry_pg, blobs)
    foreign = sql(registry_pg, blobs, "SELECT version_id FROM artifact_versions WHERE artifact_id=$1", other)[0][0]
    assert diff_of(base, aid, "nope")[0] == 404
    assert diff_of(base, aid, foreign)[0] == 404


def test_report_diff_with_base_version_id_still_reaches_the_report_route(env, registry_pg, blobs):
    base, server, _ = env
    aid = seed(registry_pg, blobs)
    version = sql(registry_pg, blobs, "SELECT version_id FROM artifact_versions WHERE artifact_id=$1", aid)[0][0]
    status, data = request(base, f"/api/v1/artifacts/{aid}/versions/{version}/diff?base_version_id={version}")
    assert "files" not in (data or {})            # the report route (not this one) answered


def test_version_summaries_include_the_commit_message(registry_pg, blobs):
    async def body(store, conn):
        aid = await new_artifact(store, conn, None)
        first = await store.commit_version(aid, base_version_id=None, source_manifest_blob=None,
                                           output_manifest=[entry(blobs, "a.md", b"a\n")], created_by=OWNER,
                                           provenance={"message": "tidy the intro"})
        second = await store.commit_version(aid, base_version_id=first["version_id"], source_manifest_blob=None,
                                            output_manifest=[entry(blobs, "a.md", b"b\n")], created_by=OWNER)
        rows = {r["version_id"]: r for r in await store.list_version_summaries(aid)}
        assert rows[first["version_id"]]["message"] == "tidy the intro"
        assert rows[second["version_id"]]["message"] is None
        assert "provenance" not in rows[first["version_id"]]
    run(registry_pg, blobs, body)


def test_invite_grant_reuse_is_idempotent_for_the_same_grant_and_409_otherwise(env, registry_pg, blobs):
    base, server, _ = env
    a, b = seed(registry_pg, blobs), seed(registry_pg, blobs)
    path = lambda aid: f"/api/v1/artifacts/{aid}/collaborators/invite-grant"  # noqa: E731
    assert request(base, path(a), body={"invite_id": "inv-r", "permission": "read"})[0] == 201
    assert request(base, path(a), body={"invite_id": "inv-r", "permission": "read"})[0] == 201      # idempotent
    status, data = request(base, path(a), body={"invite_id": "inv-r", "permission": "write"})
    assert status == 409 and data == {"error": "invite already registered"}
    status, data = request(base, path(b), body={"invite_id": "inv-r", "permission": "read"})
    assert status == 409 and data == {"error": "invite already registered"}      # no word of artifact a
    assert request(base, f"/api/v1/artifacts/{b}/collaborators")[1]["pending_invites"] == []
    assert len(request(base, f"/api/v1/artifacts/{a}/collaborators")[1]["pending_invites"]) == 1


def test_encoded_resource_segment_cannot_bypass_the_origin_guard(env, registry_pg, blobs):
    base, server, _ = env
    aid = seed(registry_pg, blobs)
    evil = {"Origin": "https://evil.example"}
    for tail, body in (("/collaborators", {"agent_id": "a1", "permission": "read"}),
                       ("/collaborators/a1/revoke", {}),
                       ("/collaborators/invite-grant", {"invite_id": "i1", "permission": "read"}),
                       ("/collab-restrict", {})):
        status, _ = request(base, f"/api/v1/%61rtifacts/{aid}{tail}", body=body, headers=evil)
        assert status == 403, tail
    assert not acl_rows(registry_pg, blobs, aid)
    assert sql(registry_pg, blobs, "SELECT 1 FROM artifact_collab_invite_grants WHERE artifact_id=$1", aid) == []


def test_diff_with_a_missing_first_parent_is_an_error_not_an_all_added_diff(env, registry_pg, blobs):
    base, server, _ = env
    aid, first, second = two_versions(registry_pg, blobs, [entry(blobs, "a.md", b"a\n")],
                                      [entry(blobs, "a.md", b"b\n")])
    async def body(store, conn):
        await conn.execute("SET session_replication_role = replica")
        await conn.execute("UPDATE artifact_versions SET parent_version_ids = ARRAY['ghost'], "
                           "parent_version_id='ghost' WHERE version_id=$1", second)
    run(registry_pg, blobs, body)
    status, data = diff_of(base, aid, second)
    assert status == 409 and data["reason"] == "parent version missing" and "files" not in data


def test_diff_file_count_and_total_read_budget_bound_cpu(env, registry_pg, blobs, monkeypatch):
    base, server, _ = env
    monkeypatch.setattr(artifacts_module, "MAX_DIFF_FILES", 3)
    olds = [entry(blobs, f"f{i}.md", b"old\n") for i in range(5)]
    news = [entry(blobs, f"f{i}.md", f"new {i}\n".encode()) for i in range(5)]
    aid, _, second = two_versions(registry_pg, blobs, olds, news)
    status, data = diff_of(base, aid, second)
    done = [f for f in data["files"] if f["diff"] is not None]
    assert [f["name"] for f in done] == ["f0.md", "f1.md", "f2.md"]
    assert [f.get("reason") for f in data["files"][3:]] == ["oversize", "oversize"]
    monkeypatch.setattr(artifacts_module, "MAX_DIFF_FILES", 200)
    monkeypatch.setattr(artifacts_module, "MAX_DIFF_READ_BYTES", 20)      # old 4 + new ~6 bytes per file
    status, data = diff_of(base, aid, second)
    done = [f for f in data["files"] if f["diff"] is not None]
    assert 1 <= len(done) < 5 and all(f["reason"] == "oversize" for f in data["files"] if f["diff"] is None)


# ------------------------------------------------------------------------------------------------ retraction fail-safe


@pytest.fixture
def failing_persist(registry_pg, blobs):
    """A REAL database failure: triggers make the retraction UPDATE (artifact or version) raise inside Postgres."""
    pg = registry_pg
    sql(pg, blobs, "CREATE OR REPLACE FUNCTION retraction_boom() RETURNS trigger AS $$ BEGIN "
                   "RAISE EXCEPTION 'boom secret-dsn'; END $$ LANGUAGE plpgsql")
    sql(pg, blobs, "DROP TRIGGER IF EXISTS boom_a ON artifacts")
    sql(pg, blobs, "DROP TRIGGER IF EXISTS boom_v ON artifact_versions")
    sql(pg, blobs, "CREATE TRIGGER boom_a BEFORE UPDATE ON artifacts FOR EACH ROW "
                   "WHEN (NEW.lifecycle = 'retracted') EXECUTE FUNCTION retraction_boom()")
    sql(pg, blobs, "CREATE TRIGGER boom_v BEFORE UPDATE ON artifact_versions FOR EACH ROW "
                   "WHEN (NEW.state = 'retracted') EXECUTE FUNCTION retraction_boom()")
    yield
    sql(pg, blobs, "DROP TRIGGER IF EXISTS boom_a ON artifacts")
    sql(pg, blobs, "DROP TRIGGER IF EXISTS boom_v ON artifact_versions")
    sql(pg, blobs, "DROP FUNCTION IF EXISTS retraction_boom()")


def grant_rows(base, aid, agent, permission):
    return request(base, f"/api/v1/artifacts/{aid}/collaborators", body={"agent_id": agent, "permission": permission})


def seeded_grants(base, pg, blobs):
    aid = seed(pg, blobs)
    assert grant_rows(base, aid, "r1", "read")[0] == 201
    assert grant_rows(base, aid, "w1", "write")[0] == 201
    return aid


def principals(pg, blobs, aid):
    return [r["principal_id"] for r in acl_rows(pg, blobs, aid)]


def version_of(pg, blobs, aid):
    return sql(pg, blobs, "SELECT version_id FROM artifact_versions WHERE artifact_id=$1", aid)[0]["version_id"]


@pytest.mark.parametrize("by_version", [False, True])
def test_failed_persist_revokes_write_grants_only_and_offers_restrict_all(env, registry_pg, blobs, failing_persist, by_version):
    base, server, _ = env
    aid = seeded_grants(base, registry_pg, blobs)
    body = {"version_id": version_of(registry_pg, blobs, aid)} if by_version else {}
    status, data = request(base, f"/api/v1/artifacts/{aid}/retract", body=body)
    assert status == 500
    assert data["persisted"] is False and data["hidden"] is True and data["error"]
    assert data["collab_write_grants_revoked"] == 1 and data["collab_revoke_error"] is None
    assert data["restrict_all_offered"] is True and data["restrict_all_applied"] is False
    assert principals(registry_pg, blobs, aid) == ["collab:r1"]          # read stays: nothing automatic restricts it


def test_successful_retraction_never_runs_the_callback_or_touches_grants(env, registry_pg, blobs):
    base, server, _ = env
    aid = seeded_grants(base, registry_pg, blobs)
    status, data = request(base, f"/api/v1/artifacts/{aid}/retract", body={"reason": "ok"})
    assert status == 200 and data["persisted"] is True
    assert data["collab_write_grants_revoked"] is None and data["collab_revoke_error"] is None
    assert "restrict_all_offered" not in data
    assert principals(registry_pg, blobs, aid) == ["collab:r1", "collab:w1"]


def test_failing_callback_is_reported_by_type_name_and_never_masks_the_500(env, registry_pg, blobs, failing_persist):
    base, server, _ = env
    aid = seeded_grants(base, registry_pg, blobs)
    server.collab_grant_store.close()                                      # the grant store is now really unusable
    status, data = request(base, f"/api/v1/artifacts/{aid}/retract", body={})
    assert status == 500 and data["persisted"] is False and data["error"]
    assert data["collab_write_grants_revoked"] is None
    assert data["collab_revoke_error"] == "RuntimeError"                   # a type name, no message
    assert data["restrict_all_offered"] is True


def test_restrict_all_in_the_retract_body_is_a_bool_and_unknown_keys_still_400(env, registry_pg, blobs):
    base, server, _ = env
    aid = seeded_grants(base, registry_pg, blobs)
    for bad in ("yes", 1, None, [True]):
        assert request(base, f"/api/v1/artifacts/{aid}/retract", body={"restrict_all": bad})[0] == 400, bad
    assert request(base, f"/api/v1/artifacts/{aid}/retract", body={"restrict_all": True, "scope": "x"})[0] == 400
    assert principals(registry_pg, blobs, aid) == ["collab:r1", "collab:w1"]


def test_failed_persist_with_restrict_all_also_removes_read_grants(env, registry_pg, blobs, failing_persist):
    base, server, _ = env
    aid = seeded_grants(base, registry_pg, blobs)
    other = seeded_grants(base, registry_pg, blobs)
    status, data = request(base, f"/api/v1/artifacts/{aid}/retract", body={"restrict_all": True})
    assert status == 500 and data["collab_write_grants_revoked"] == 1 and data["restrict_all_offered"] is True
    assert data["restrict_all_applied"] is True and data["collab_all_grants_revoked"] == 1 and data["collab_restrict_error"] is None   # the write one went first
    assert principals(registry_pg, blobs, aid) == []
    assert principals(registry_pg, blobs, other) == ["collab:r1", "collab:w1"]      # that artifact only


def test_restrict_all_on_a_successful_retraction_touches_no_grants(env, registry_pg, blobs):
    base, server, _ = env
    ok = seeded_grants(base, registry_pg, blobs)
    status, data = request(base, f"/api/v1/artifacts/{ok}/retract", body={"restrict_all": True})
    assert status == 200 and data.get("collab_all_grants_revoked") is None and data["restrict_all_applied"] is False
    assert principals(registry_pg, blobs, ok) == ["collab:r1", "collab:w1"]          # success: grants untouched


def test_restrict_route_revokes_read_and_write_of_that_artifact_only(env, registry_pg, blobs, tmp_path):
    base, server, _ = env
    aid = seeded_grants(base, registry_pg, blobs)
    other = seeded_grants(base, registry_pg, blobs)
    sql(registry_pg, blobs, "INSERT INTO artifact_acl (artifact_id, principal_id, permissions, granted_by) "
                            "VALUES ($1,'api-key:x:operator','[\"read\"]'::jsonb,'o')", aid)   # a web grant stays
    status, data = request(base, f"/api/v1/artifacts/{aid}/collab-restrict", body={})
    assert status == 200 and data["revoked"] == 2
    assert principals(registry_pg, blobs, aid) == [] and principals(registry_pg, blobs, other) == ["collab:r1", "collab:w1"]
    assert sql(registry_pg, blobs, "SELECT 1 FROM artifact_acl WHERE artifact_id=$1 AND principal_id='api-key:x:operator'",
               aid)
    assert request(base, f"/api/v1/artifacts/{aid}/collab-restrict", body={})[1]["revoked"] == 0
    assert request(base, f"/api/v1/artifacts/{aid}/collab-restrict", body={"x": 1})[0] == 400
    assert request(base, f"/api/v1/artifacts/{aid}/collab-restrict", method="GET")[0] == 404


def test_restrict_route_needs_admin_and_changes_nothing_for_others(env, registry_pg, blobs):
    base, server, _ = env
    aid = seeded_grants(base, registry_pg, blobs)
    denied = request(base, f"/api/v1/artifacts/{aid}/collab-restrict", key=OTHER_KEY, body={})
    assert denied == (404, {"error": "not found"})
    assert request(base, f"/api/v1/artifacts/{aid}/collab-restrict", body={}, headers={"Origin": "https://evil.example"})[0] == 403
    assert principals(registry_pg, blobs, aid) == ["collab:r1", "collab:w1"]


def test_after_restrict_the_mirror_drops_the_grants_and_the_artifact_in_one_sync(env, registry_pg, blobs, tmp_path):
    base, server, _ = env
    aid = seeded_grants(base, registry_pg, blobs)
    mirror = CollabMirror(tmp_path / "mirror", rustfs=blobs, limits=BridgeLimits())

    async def sync_once(store, conn):
        gate = ArtifactGraphGate(None)
        gate.load_retractions(await store.load_retractions())
        await mirror.publish_grants(CollabGrantStore(conn), store)
        await mirror.sync(store, gate=gate)
    run(registry_pg, blobs, sync_once)
    assert (tmp_path / "mirror" / "artifacts" / aid / "head.json").exists()
    assert request(base, f"/api/v1/artifacts/{aid}/collab-restrict", body={})[1]["revoked"] == 2
    run(registry_pg, blobs, sync_once)
    agents = json.loads((tmp_path / "mirror" / "grants.json").read_text())["agents"]
    assert all(aid not in arts for arts in agents.values())
    assert not (tmp_path / "mirror" / "artifacts" / aid).exists()


def pending(pg, blobs, aid):
    return sorted(r["invite_id"] for r in sql(pg, blobs, "SELECT invite_id FROM artifact_collab_invite_grants "
                                              "WHERE artifact_id=$1 AND resolved_at IS NULL", aid))


def seed_invites(base, pg, blobs):
    aid = seeded_grants(base, pg, blobs)
    for inv, perm in (("w", "write"), ("r", "read")):
        assert request(base, f"/api/v1/artifacts/{aid}/collaborators/invite-grant",
                       body={"invite_id": f"{inv}-{aid}", "permission": perm})[0] == 201
    return aid


def test_failsafe_drops_pending_write_invites_so_a_later_join_creates_no_grant(env, registry_pg, blobs, failing_persist):
    base, server, _ = env
    aid, other = seed_invites(base, registry_pg, blobs), seed_invites(base, registry_pg, blobs)
    status, data = request(base, f"/api/v1/artifacts/{aid}/retract", body={})
    assert status == 500 and data["collab_write_grants_revoked"] == 1 and data["pending_invites_revoked"] == 1
    assert pending(registry_pg, blobs, aid) == [f"r-{aid}"] and len(pending(registry_pg, blobs, other)) == 2
    joined = server.collab_grant_store("resolve_invite_grants", {f"w-{aid}": {"agent_id": "late"}})
    assert joined == 0 and "collab:late" not in principals(registry_pg, blobs, aid)


def test_restrict_all_and_restrict_route_also_drop_every_pending_invite(env, registry_pg, blobs, failing_persist):
    base, server, _ = env
    aid, other = seed_invites(base, registry_pg, blobs), seed_invites(base, registry_pg, blobs)
    status, data = request(base, f"/api/v1/artifacts/{aid}/retract", body={"restrict_all": True})
    assert status == 500 and data["pending_invites_revoked"] == 2 and pending(registry_pg, blobs, aid) == []
    assert data["restrict_all_applied"] is True and len(pending(registry_pg, blobs, other)) == 2
    status, data = request(base, f"/api/v1/artifacts/{other}/collab-restrict", body={})
    assert status == 200 and data == {"revoked": 2, "pending_invites_revoked": 2}
    assert pending(registry_pg, blobs, other) == []


def test_restrict_all_applied_is_false_when_ignored_on_success_or_with_the_bridge_disabled(registry_pg, blobs):
    aid = seed(registry_pg, blobs)
    with collab_server(registry_pg, blobs, enabled=False) as (base, server, _):
        status, data = request(base, f"/api/v1/artifacts/{aid}/retract", body={"restrict_all": True})
        assert status == 200 and data["restrict_all_applied"] is False


def test_a_user_prefixed_agent_id_is_a_400_on_grant_and_revoke(env, registry_pg, blobs):
    base, server, _ = env
    aid = seed(registry_pg, blobs)
    assert request(base, f"/api/v1/artifacts/{aid}/collaborators",
                   body={"agent_id": "user:x", "permission": "read"})[0] == 400
    assert request(base, f"/api/v1/artifacts/{aid}/collaborators/user:x/revoke", body={})[0] == 400
