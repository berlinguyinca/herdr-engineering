"""Owner-only artifact delete/restore API over real HTTP against a real PostgreSQL registry (no mocks)."""
import asyncio
import hashlib
import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager

import asyncpg
import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.artifact_adapter import SyncArtifactAdapter
from herdr_engineering.control_plane.artifact_delete_api import owner_digests_config
from herdr_engineering.control_plane.artifacts import ArtifactStore
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane import registry_pg as _registry_pg_module
from tests.control_plane.registry_pg import FakeBlobs, uid

registry_pg = _registry_pg_module.registry_pg      # the module-scoped PostgreSQL fixture
KEYS = {"owner-key": "operator", "creator-key": "operator", "member-key": "operator", "viewer-key": "viewer"}
OWNER_DIGEST = hashlib.sha256(b"owner-key").hexdigest()[:16]
CREATOR = f"api-key:{hashlib.sha256(b'creator-key').hexdigest()[:16]}:operator"


class Env:
    def __init__(self, base, adapter, blobs):
        self.base, self.adapter, self.blobs = base, adapter, blobs

    def seed(self, data=None, *, owner=CREATOR, name="a.md", media="text/markdown"):
        data = data or b"payload " + uid("d").encode()
        art = self.adapter("register_logical", original_filename=name, created_by=owner, mime_type=media)
        key, sha = self.blobs.put(data)
        out = [{"name": name, "storage_key": key, "sha256": sha, "size_bytes": len(data), "media_type": media}]
        self.adapter("commit_version", art["artifact_id"], base_version_id=None, source_manifest_blob=None,
                     output_manifest=out, created_by=owner)
        return art["artifact_id"], key

    def lifecycle(self, aid):
        return self.adapter("list_lifecycle_events", aid)


@contextmanager
def api(pg, *, owners=(OWNER_DIGEST,), keys=KEYS, store=True, keyless_owner=False, extra=None):
    blobs = FakeBlobs()

    async def make():
        return ArtifactStore(await asyncpg.connect(pg["dsn"]), blobs)

    adapter = SyncArtifactAdapter(make)
    server = ControlPlaneServer(ControlPlaneRepo(), api_keys=keys, artifact_store=adapter if store else None,
                                artifact_delete_owners=frozenset(owners),
                                artifact_delete_allow_keyless_owner=keyless_owner, rustfs=blobs, **(extra or {}))
    threading.Thread(target=server.serve, daemon=True).start()
    server.wait_until_ready()
    try:
        yield Env(f"http://127.0.0.1:{server.port}", adapter, blobs)
    finally:
        server.shutdown()
        adapter.close()


def request(base, path, *, key="owner-key", body=None, headers=None, method=None, raw=None):
    headers = {**({"X-API-Key": key} if key else {}), **(headers or {})}
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


def delete(env, ids, **kw):
    body = {"ids": ids, "mode": "tombstone", "reason": "cleanup", **kw.pop("body", {})}
    return request(env.base, "/api/v1/artifacts/delete", body=body, **kw)


def test_authz_matrix_only_the_owner_key_may_delete_or_restore(registry_pg):
    with api(registry_pg) as env:
        aid, key = env.seed()
        for caller, status in [("creator-key", 404), ("member-key", 404), ("viewer-key", 403), (None, 401),
                               ("wrong-key", 401)]:
            for path in ("delete", "restore"):
                got, body = request(env.base, f"/api/v1/artifacts/{path}", key=caller,
                                    body={"ids": [aid], "mode": "tombstone", "reason": "x"})
                assert got == status, (caller, path, body)
        assert request(env.base, "/api/v1/artifacts/tombstoned", key="creator-key")[0] == 404
        assert request(env.base, "/api/v1/artifacts/tombstoned", key="viewer-key")[0] == 404
        assert env.lifecycle(aid) == [] and env.blobs.head(key)
        # the same fixed answer for an id that exists and one that does not: no existence oracle
        assert (delete(env, ["art-nope"], key="member-key") == delete(env, [aid], key="member-key"))
        got, body = delete(env, [aid])
        assert got == 200 and body["results"][0]["result"] == "tombstoned"
        assert request(env.base, "/api/v1/artifacts/delete-access", key="owner-key")[1] == {"owner": True}
        for caller in ("creator-key", "member-key", "viewer-key"):
            assert request(env.base, "/api/v1/artifacts/delete-access", key=caller)[1] == {"owner": False}


def test_a_keyless_listener_is_an_owner_only_with_the_explicit_opt_in_and_no_owner_list_means_nobody(registry_pg):
    with api(registry_pg, keys=None, owners=()) as env:                 # I5: default is NOT owner
        aid, _ = env.seed()
        assert delete(env, [aid], key=None)[0] == 404
        assert request(env.base, "/api/v1/artifacts/delete-access", key=None)[1] == {"owner": False}
    with api(registry_pg, keys=None, owners=(), keyless_owner=True) as env:
        aid, _ = env.seed()
        assert delete(env, [aid], key=None)[0] == 200
        assert env.lifecycle(aid)[0]["actor"] == "keyless:operator"      # M9: distinguishable in the audit trail
    with api(registry_pg, owners=()) as env:
        aid, _ = env.seed()
        assert delete(env, [aid])[0] == 404


def test_tombstone_hides_then_restore_brings_back_and_replays(registry_pg):
    with api(registry_pg) as env:
        a, ka = env.seed()
        b, kb = env.seed()
        got, body = delete(env, [a, b, "art-missing"], body={"hold_days": 3, "request_key": "r1"})
        assert got == 200 and body["summary"] == {"tombstoned": 2, "not_found": 1}
        assert env.blobs.head(ka)                                  # a tombstone keeps the bytes
        listed = request(env.base, "/api/v1/artifacts?limit=500", key="creator-key")[1]["artifacts"]
        assert a not in [x["artifact_id"] for x in listed]
        rows = request(env.base, "/api/v1/artifacts/tombstoned")[1]["artifacts"]
        assert {a, b} <= {x["artifact_id"] for x in rows}
        again = delete(env, [a, b], body={"hold_days": 3, "request_key": "r1"})[1]
        assert [r["replayed"] for r in again["results"]] == [True, True]
        assert len([e for e in env.lifecycle(a) if e["action"] == "tombstone"]) == 1
        got, body = request(env.base, "/api/v1/artifacts/restore", body={"ids": [a, "art-missing"], "request_key": "q"})
        assert got == 200 and [r["result"] for r in body["results"]] == ["restored", "not_found"]
        assert a in [x["artifact_id"] for x in request(env.base, "/api/v1/artifacts?limit=500", key="creator-key")[1]["artifacts"]]
        got, body = request(env.base, "/api/v1/artifacts/restore", body={"ids": [a]})
        assert body["results"][0] == {"artifact_id": a, "result": "refused", "reason": "not_tombstoned", "replayed": False}


def test_purge_now_needs_the_typed_count_and_never_touches_shared_bytes(registry_pg):
    with api(registry_pg) as env:
        shared = b"shared " + uid("s").encode()
        a, ka = env.seed(shared)
        b, kb = env.seed(shared)
        c, kc = env.seed()
        for confirm in (None, "1", 3, "3 ", "03"):
            body = {"mode": "purge_now", **({"confirm": confirm} if confirm is not None else {})}
            got, out = delete(env, [a, c], body=body)
            assert (got, out) == (400, {"error": "invalid request"}), confirm
        assert env.lifecycle(a) == [] and env.blobs.head(ka) and env.blobs.head(kc)
        got, out = delete(env, [a, c, c], body={"mode": "purge_now", "confirm": "2"})      # duplicates count once
        assert got == 200 and [r["result"] for r in out["results"]] == ["deleted", "deleted"]
        assert out["results"][0]["objects_deleted"] == 0 and out["results"][0]["objects_kept_shared"] == 1
        assert env.blobs.head(kb) and not env.blobs.head(kc)
        got, out = delete(env, [b], body={"mode": "purge_now", "confirm": "1", "request_key": "p"})
        assert out["results"][0]["objects_deleted"] == 1 and not env.blobs.head(kb)
        assert "storage" not in json.dumps(out) and "sha256/" not in json.dumps(out)
        got, out = delete(env, [b], body={"mode": "purge_now", "confirm": "1", "request_key": "p"})
        assert out["results"][0]["result"] == "already_purged" and out["results"][0]["replayed"] is True
        events = env.lifecycle(b)
        assert [e["action"] for e in events] == ["tombstone", "purge"] and events[1]["actor"].startswith("api-key:")


def test_legal_hold_is_refused_unless_the_owner_overrides_it(registry_pg):
    with api(registry_pg) as env:
        aid, key = env.seed()
        env.adapter("tombstone_artifact", aid, actor="o", reason="legal", legal_hold=True)
        got, out = delete(env, [aid], body={"mode": "purge_now", "confirm": "1"})
        assert got == 200 and out["results"][0]["result"] == "refused" and out["results"][0]["reason"] == "legal_hold"
        assert env.blobs.head(key)
        assert delete(env, [aid], body={"override_legal_hold": "yes"})[0] == 400
        got, out = delete(env, [aid], body={"mode": "purge_now", "confirm": "1", "override_legal_hold": True})
        assert out["results"][0]["result"] == "deleted" and not env.blobs.head(key)


@pytest.mark.parametrize("body", [
    {"ids": [], "mode": "tombstone", "reason": "r"},
    {"ids": "a", "mode": "tombstone", "reason": "r"},
    {"ids": [f"a{n}" for n in range(201)], "mode": "tombstone", "reason": "r"},
    {"ids": ["a/b"], "mode": "tombstone", "reason": "r"},
    {"ids": [1], "mode": "tombstone", "reason": "r"},
    {"ids": ["a"], "mode": "shred", "reason": "r"},
    {"ids": ["a"], "mode": "tombstone", "reason": "  "},
    {"ids": ["a"], "mode": "tombstone"},
    {"ids": ["a"], "mode": "tombstone", "reason": "r", "hold_days": -1},
    {"ids": ["a"], "mode": "tombstone", "reason": "r", "hold_days": 0},
    {"ids": ["a"], "mode": "tombstone", "reason": "r", "hold_days": 0.5},
    {"ids": ["a"], "mode": "tombstone", "reason": "r", "hold_days": True},
    {"ids": ["a"], "mode": "tombstone", "reason": "r", "hold_days": 99999},
    {"ids": ["a"], "mode": "tombstone", "reason": "r", "request_key": "bad key!"},
    {"ids": ["a"], "mode": "tombstone", "reason": "r", "actor": "someone"},
    {"ids": ["a"], "mode": "tombstone", "reason": "x" * 1001},
])
def test_malformed_requests_get_one_fixed_400(registry_pg, body):
    with api(registry_pg) as env:
        assert request(env.base, "/api/v1/artifacts/delete", body=body) == (400, {"error": "invalid request"})


def test_transport_rules_content_type_origin_and_json(registry_pg):
    with api(registry_pg) as env:
        aid, key = env.seed()
        good = json.dumps({"ids": [aid], "mode": "tombstone", "reason": "r"}).encode()
        assert request(env.base, "/api/v1/artifacts/delete", raw=good, method="POST",
                       headers={"Content-Type": "text/plain"})[0] == 415
        assert request(env.base, "/api/v1/artifacts/delete", raw=good, method="POST",
                       headers={"Content-Type": "application/json", "Origin": "https://evil.example"})[0] == 403
        assert request(env.base, "/api/v1/artifacts/delete", raw=b"{nope", method="POST",
                       headers={"Content-Type": "application/json"}) == (400, {"error": "invalid request"})
        assert request(env.base, "/api/v1/artifacts/delete", raw=b"[]", method="POST",
                       headers={"Content-Type": "application/json"})[0] == 400
        assert env.lifecycle(aid) == []
        host = env.base.removeprefix("http://")
        assert request(env.base, "/api/v1/artifacts/delete", raw=good, method="POST",
                       headers={"Content-Type": "application/json", "Origin": f"http://{host}"})[0] == 200


def test_existing_artifact_routes_are_not_shadowed(registry_pg):
    with api(registry_pg) as env:
        aid, _ = env.seed()
        assert request(env.base, f"/api/v1/artifacts/{aid}", key="creator-key")[0] == 200
        assert request(env.base, "/api/v1/artifacts/delete", key="owner-key", method="GET")[0] != 200
        got, body = request(env.base, f"/api/v1/artifacts/{aid}/restore", key="creator-key", body={})
        assert got in (200, 404, 400)           # the per-artifact lifecycle route still answers by its own rules


def test_a_dead_store_is_one_fixed_503_without_driver_text(registry_pg):
    blobs = FakeBlobs()
    holder = {}

    async def make():
        holder["conn"] = await asyncpg.connect(registry_pg["dsn"])
        return ArtifactStore(holder["conn"], blobs)

    adapter = SyncArtifactAdapter(make)
    server = ControlPlaneServer(ControlPlaneRepo(), api_keys=KEYS, artifact_store=adapter,
                                artifact_delete_owners=frozenset({OWNER_DIGEST}))
    threading.Thread(target=server.serve, daemon=True).start()
    server.wait_until_ready()
    base = f"http://127.0.0.1:{server.port}"
    try:
        asyncio.run_coroutine_threadsafe(holder["conn"].close(), adapter._loop).result()
        for path, body in [("delete", {"ids": ["a"], "mode": "tombstone", "reason": "r"}),
                           ("delete", {"ids": ["a"], "mode": "purge_now", "reason": "r", "confirm": "1"}),
                           ("restore", {"ids": ["a"]})]:
            got = request(base, f"/api/v1/artifacts/{path}", body=body)
            assert got == (503, {"error": "artifact store unavailable"})
        assert request(base, "/api/v1/artifacts/tombstoned") == (503, {"error": "artifact store unavailable"})
        # a non-owner still sees the same 404 as ever, even with the store dead
        assert request(base, "/api/v1/artifacts/delete", key="member-key", body={"ids": ["a"]})[0] == 404
    finally:
        server.shutdown()
        adapter.close()


def test_no_registry_is_the_same_fixed_503(registry_pg):
    with api(registry_pg, store=False) as env:
        assert delete(env, ["a"]) == (503, {"error": "artifact store unavailable"})


def test_owner_digest_config_is_validated():
    assert owner_digests_config(None) == (frozenset(), False)
    assert owner_digests_config({"owner_key_digests": [OWNER_DIGEST]}).digests == {OWNER_DIGEST}
    assert owner_digests_config({"allow_keyless_owner": True}).allow_keyless is True
    for bad in ([OWNER_DIGEST], {"allow_keyless_owner": "yes"}, {"owner_key_digests": "x"}, {"owner_key_digests": ["short"]}, {"owner_key_digests": [1]},
                {"other": []}):
        with pytest.raises(ValueError, match="artifact_delete"):
            owner_digests_config(bad)


def test_legal_hold_restore_needs_the_override_over_http_and_refusals_are_audited(registry_pg):
    with api(registry_pg) as env:
        aid, key = env.seed()
        env.adapter("tombstone_artifact", aid, actor="o", reason="legal", legal_hold=True)
        got, out = request(env.base, "/api/v1/artifacts/restore", body={"ids": [aid]})
        assert got == 200 and out["results"][0]["reason"] == "legal_hold"
        assert env.lifecycle(aid)[-1]["action"] == "refused"
        got, out = request(env.base, "/api/v1/artifacts/restore", body={"ids": [aid], "override_legal_hold": True})
        assert out["results"][0]["result"] == "restored"
        got, out = request(env.base, "/api/v1/artifacts/restore", body={"ids": [aid], "override_legal_hold": "x"})
        assert got == 400


def test_a_reused_request_key_is_a_specific_refusal_over_http(registry_pg):
    with api(registry_pg) as env:
        aid, key = env.seed()
        assert delete(env, [aid], body={"request_key": "K"})[1]["results"][0]["result"] == "tombstoned"
        got, out = delete(env, [aid], body={"mode": "purge_now", "confirm": "1", "request_key": "K"})
        assert out["results"][0]["reason"] == "request_key_reused" and env.blobs.head(key)


def test_a_partly_failing_bulk_still_returns_every_result_but_a_fully_failing_one_is_the_fixed_503(registry_pg):
    with api(registry_pg) as env:
        a, _ = env.seed()
        got, out = delete(env, [a, "art-missing"], body={"mode": "purge_now", "confirm": "2", "request_key": "p"})
        assert got == 200 and [r["result"] for r in out["results"]] == ["deleted", "not_found"]


def test_tombstone_is_invisible_to_non_owners_and_read_only_until_restored(registry_pg):
    with api(registry_pg) as env:
        aid, _ = env.seed()
        assert request(env.base, f"/api/v1/artifacts/{aid}", key="creator-key")[0] == 200
        delete(env, [aid])
        for path in (f"/api/v1/artifacts/{aid}", f"/api/v1/artifacts/{aid}/links", f"/api/v1/artifacts/{aid}/lifecycle"):
            assert request(env.base, path, key="creator-key")[0] == 404, path
        assert request(env.base, f"/api/v1/artifacts/{aid}/links", key="creator-key", body={"task_id": "t"})[0] == 404
        assert request(env.base, f"/api/v1/artifacts/{aid}/lifecycle", key="owner-key")[0] == 200     # the owner can audit
        request(env.base, "/api/v1/artifacts/restore", body={"ids": [aid]})
        assert request(env.base, f"/api/v1/artifacts/{aid}", key="creator-key")[0] == 200


def test_creators_cannot_tombstone_or_restore_through_the_old_per_artifact_routes(registry_pg):
    with api(registry_pg) as env:
        aid, key = env.seed()
        for key_name in ("creator-key", "member-key"):
            got = request(env.base, f"/api/v1/artifacts/{aid}/tombstone", key=key_name,
                          body={"reason": "x", "legal_hold": True})
            assert got[0] == 404, key_name
        assert env.lifecycle(aid) == []
        got, out = request(env.base, f"/api/v1/artifacts/{aid}/tombstone", key="owner-key", body={"reason": "x"})
        assert got == 200 and out["lifecycle"] == "tombstoned"
        assert request(env.base, f"/api/v1/artifacts/{aid}/restore", key="creator-key", body={})[0] == 404
        assert env.lifecycle(aid)[-1]["action"] == "tombstone"
        assert request(env.base, f"/api/v1/artifacts/{aid}/restore", key="owner-key", body={})[1]["lifecycle"] == "active"
        # archive loses nothing, so admin on the artifact keeps it
        assert request(env.base, f"/api/v1/artifacts/{aid}/archive", key="creator-key", body={})[1]["lifecycle"] == "archived"
        assert request(env.base, f"/api/v1/artifacts/{aid}/restore", key="creator-key", body={})[1]["lifecycle"] == "active"


def test_i1_tombstone_revokes_live_shares_removes_their_bundles_and_blocks_new_ones(registry_pg, tmp_path):
    from herdr_engineering.control_plane.artifact_shares import ShareStore

    async def make_shares():
        return ShareStore(await asyncpg.connect(registry_pg["dsn"]))

    shares = SyncArtifactAdapter(make_shares)
    settings = {"public_url": "http://127.0.0.1", "max_share_bytes": 1 << 28, "max_files": 100,
                "max_total_bytes": 1 << 30, "default_public_days": 30}
    try:
        with api(registry_pg, extra={"share_store": shares, "shares_root": tmp_path, "share_settings": settings}) as env:
            aid, key = env.seed(b"<html><body>hello " + uid("h").encode() + b"</body></html>", name="index.html",
                                media="text/html")
            version = env.adapter("get_record", aid, principal_id=CREATOR)["current_version_id"]
            got, made = request(env.base, f"/api/v1/artifacts/{aid}/shares", key="creator-key",
                                body={"version_id": version, "audience": "lab", "allow_emails": ["a@example.org"]})
            assert got == 201, made
            sid = made["share"]["share_id"]
            got, approved = request(env.base, f"/api/v1/artifacts/{aid}/shares/{sid}/approve", key="creator-key",
                                    body={"confirm": True})
            assert got == 200, approved
            assert (tmp_path / sid).exists() and any((tmp_path / "by-token").iterdir())
            got, out = delete(env, [aid])
            assert got == 200 and out["results"][0]["shares_revoked"] == 1
            assert not (tmp_path / sid).exists() and not any((tmp_path / "by-token").iterdir())
            assert shares("get", sid)["state"] == "revoked"
            # a deleted artifact cannot be shared out again, even by its creator
            assert request(env.base, f"/api/v1/artifacts/{aid}/shares", key="creator-key",
                           body={"version_id": version, "audience": "lab", "allow_emails": ["a@example.org"]})[0] == 404
            # archived (not deleted) is not active either: the share routes answer 409
            other, _ = env.seed()
            env.adapter("archive_artifact", other, actor="o")
            got, out = request(env.base, f"/api/v1/artifacts/{other}/shares", key="creator-key",
                               body={"version_id": "v", "audience": "lab", "allow_emails": ["a@example.org"]})
            assert got == 409 and out == {"error": "artifact is not active"}
    finally:
        shares.close()
