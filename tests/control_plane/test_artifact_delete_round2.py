"""Regression tests for the second review of the owner-delete feature (N1, N3-N8, N10, N12, N13) on a real PostgreSQL.

N2 (the collaborator mirror) lives in ``test_artifact_delete_mirror.py``. No mocks: a real database, real HTTP, and a
content-addressed in-memory object store; races are made deterministic with real Postgres advisory locks and threads."""
import asyncio
import json
import threading
import time
import urllib.error
import urllib.request

import asyncpg
import pytest

from herdr_engineering.control_plane.artifact_acl import ArtifactAcl
from herdr_engineering.control_plane.artifact_delete_api import TombstoneCache
from herdr_engineering.control_plane.artifact_shares import ShareError, ShareStore
from herdr_engineering.control_plane.artifacts import ArtifactError, ArtifactStore
from herdr_engineering.control_plane.share_bundle import BundleFile, BundleIndex
from tests.control_plane import registry_pg as _registry_pg_module
from tests.control_plane.registry_pg import FakeBlobs, uid
from tests.control_plane.test_artifact_delete_api import api, delete, request
from tests.control_plane.test_artifact_registry_ext import publish, with_store

registry_pg = _registry_pg_module.registry_pg      # the module-scoped PostgreSQL fixture
OWNER = "api-key:abc:operator"
ROOT = "/api/v1/artifacts"


def run(coro):
    return asyncio.run(coro)


def key_of(version):
    return version["output_manifest"][0]["storage_key"]


def data_for(tag):
    return f"{tag} {uid('d')}".encode()


class FlakyBlobs(FakeBlobs):
    fail = False

    def delete(self, key):
        if self.fail:
            raise OSError("object store down")
        super().delete(key)


def in_thread(coro_fn):
    """Run an async function on its own event loop and connection in a thread; returns (thread, box)."""
    box: dict = {}

    def target():
        try:
            box["result"] = asyncio.run(coro_fn())
        except BaseException as exc:                    # noqa: BLE001 - handed back to the test
            box["error"] = exc
    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, box


# ------------------------------------------------------------------------------------------------ N13
def test_n13_a_hold_written_by_the_old_code_is_still_a_hold(registry_pg):
    blobs = FakeBlobs()

    async def body(store, conn):
        a, va = await publish(store, blobs, owner="o", name="a.md", media="text/markdown", data=data_for("n13"))
        await store.tombstone_artifact(a, actor=OWNER, reason="hold")
        # exactly what the pre-flag code wrote for a legal hold: tombstoned, no purge date, the new column untouched
        await conn.execute("UPDATE artifacts SET purge_after=NULL, legal_hold=false WHERE artifact_id=$1", a)
        out = await store.purge_now(a, actor=OWNER, reason="x")
        assert (out["result"], out["reason"]) == ("refused", "legal_hold")
        assert blobs.head(key_of(va))
        assert (await store.restore_many([a], actor=OWNER))[0]["reason"] == "legal_hold"
        assert (await store.list_tombstoned())[0]["legal_hold"] is True
        assert await store.purge_due() == []
        # the worker's reconcile pass normalises the row into the durable flag
        assert (await store.reconcile())["legacy_holds_normalised"] >= 1
        assert await conn.fetchval("SELECT legal_hold FROM artifacts WHERE artifact_id=$1", a) is True
        out = await store.purge_now(a, actor=OWNER, reason="x", override_legal_hold=True)
        assert out["result"] == "deleted" and not blobs.head(key_of(va))

    run(with_store(registry_pg["dsn"], blobs, body))


def test_n13_the_migration_backfill_can_be_run_again(registry_pg):
    blobs = FakeBlobs()

    async def body(store, conn):
        a, _ = await publish(store, blobs, owner="o", name="a.md", media="text/markdown", data=data_for("n13m"))
        await store.tombstone_artifact(a, actor=OWNER, reason="hold")
        await conn.execute("UPDATE artifacts SET purge_after=NULL, legal_hold=false WHERE artifact_id=$1", a)
        sql = open("deploy/control-plane/migrations/0047_artifact_delete_hardening.sql").read()
        await conn.execute(sql)
        await conn.execute(sql)
        assert await conn.fetchval("SELECT legal_hold FROM artifacts WHERE artifact_id=$1", a) is True

    run(with_store(registry_pg["dsn"], blobs, body))


# ------------------------------------------------------------------------------------------------ N6
def test_n6_a_reused_key_on_a_changed_state_is_refused_not_reported_as_a_replay(registry_pg):
    blobs = FakeBlobs()

    async def body(store, conn):
        a, _ = await publish(store, blobs, owner="o", name="a.md", media="text/markdown", data=data_for("n6"))
        first = await store.delete_many([a], mode="tombstone", actor=OWNER, reason="r", request_key="K")
        again = await store.delete_many([a], mode="tombstone", actor=OWNER, reason="r", request_key="K")
        assert first[0]["result"] == "tombstoned" and again[0]["replayed"] is True        # a true replay still works
        assert (await store.restore_many([a], actor=OWNER, request_key="R"))[0]["result"] == "restored"
        assert await store.lifecycle_of(a) == "active"
        stale = await store.delete_many([a], mode="tombstone", actor=OWNER, reason="r", request_key="K")
        assert (stale[0]["result"], stale[0]["reason"]) == ("refused", "request_key_reused")
        assert await store.lifecycle_of(a) == "active"                  # and the owner is told it was NOT deleted
        await store.delete_many([a], mode="tombstone", actor=OWNER, reason="r", request_key="K2")
        stale = await store.restore_many([a], actor=OWNER, request_key="R")
        assert (stale[0]["result"], stale[0]["reason"]) == ("refused", "request_key_reused")
        assert await store.lifecycle_of(a) == "tombstoned"

    run(with_store(registry_pg["dsn"], blobs, body))


# ------------------------------------------------------------------------------------------------ N7
def test_n7_a_restricted_restore_is_decided_under_the_row_lock(registry_pg):
    blobs = FakeBlobs()

    async def body(store, conn):
        a, _ = await publish(store, blobs, owner="o", name="a.md", media="text/markdown", data=data_for("n7"))
        await store.tombstone_artifact(a, actor=OWNER, reason="gone")
        with pytest.raises(ArtifactError):
            await store.restore_artifact(a, actor="admin", allowed_from={"archived"})
        assert await store.lifecycle_of(a) == "tombstoned"
        await store.restore_artifact(a, actor=OWNER)
        await store.archive_artifact(a, actor="admin")
        assert (await store.restore_artifact(a, actor="admin", allowed_from={"archived"}))["lifecycle"] == "active"

    run(with_store(registry_pg["dsn"], blobs, body))


# ------------------------------------------------------------------------------------------------ N8
def test_n8_only_canonical_keys_are_ever_deleted_and_blobs_match_by_digest(registry_pg):
    blobs = FakeBlobs()

    async def body(store, conn):
        data = data_for("n8")
        a, va = await publish(store, blobs, owner="o", name="a.md", media="text/markdown", data=data)
        key = key_of(va)
        digest = key.rsplit("/", 1)[-1]
        assert store._canonical_key(f"x/../{key}") == key and store._canonical_key("sha256/aa/bb/short") is None
        # an alias spelling in the queue is dropped, never handed to the object store
        alias = f"sha256//../{key}"
        blobs.objects[alias] = b"other"
        await conn.execute("INSERT INTO artifact_pending_deletes (storage_key) VALUES ($1)", alias)
        assert await store.drain_pending_deletes([alias]) == [] and alias in blobs.objects
        assert not await conn.fetchval("SELECT 1 FROM artifact_pending_deletes WHERE storage_key=$1", alias)
        # another artifact holds the same bytes under a different spelling of the key: still shared, by digest
        b, _ = await publish(store, blobs, owner="o2", name="b.md", media="text/markdown", data=data_for("n8b"))
        await conn.execute(
            "INSERT INTO artifact_blobs (scope, blob_sha256, storage_key, size_bytes, media_type, verified_at) "
            "VALUES ($1,$2,$3,1,'text/plain',now())", b, digest, f"alias/{digest}")
        out = await store.purge_now(a, actor=OWNER, reason="x")
        assert out["deleted"] == [] and out["retained_shared"] == [key] and blobs.head(key)

    run(with_store(registry_pg["dsn"], blobs, body))


# ------------------------------------------------------------------------------------------------ N5
def test_n5_an_artifacts_own_staged_upload_does_not_pin_its_bytes_after_purge(registry_pg):
    blobs = FakeBlobs()

    async def body(store, conn):
        data = data_for("n5")
        a, va = await publish(store, blobs, owner="o", name="a.md", media="text/markdown", data=data)
        key = key_of(va)
        await conn.execute(
            "INSERT INTO artifact_collab_uploads (owner_key, upload_id, artifact_id, name, sha256, size_bytes, "
            "storage_key, media_type, charged) VALUES ('agent:x', $1, $2, 'a.md', $3, 1, $4, 'text/markdown', false)",
            uid("u"), a, key.rsplit("/", 1)[-1], key)
        out = await store.purge_now(a, actor=OWNER, reason="x")
        assert out["deleted"] == [key] and not blobs.head(key)
        # but another live artifact's staged upload of the same bytes still protects them
        b, vb = await publish(store, blobs, owner="o", name="b.md", media="text/markdown", data=data_for("n5b"))
        c, _ = await publish(store, blobs, owner="o", name="c.md", media="text/markdown", data=data_for("n5c"))
        await conn.execute(
            "INSERT INTO artifact_collab_uploads (owner_key, upload_id, artifact_id, name, sha256, size_bytes, "
            "storage_key, media_type, charged) VALUES ('agent:x', $1, $2, 'b.md', $3, 1, $4, 'text/markdown', false)",
            uid("u"), c, key_of(vb).rsplit("/", 1)[-1], key_of(vb))
        out = await store.purge_now(b, actor=OWNER, reason="x")
        assert out["deleted"] == [] and blobs.head(key_of(vb))

    run(with_store(registry_pg["dsn"], blobs, body))


# ------------------------------------------------------------------------------------------------ N10
class KillingBlobs(FlakyBlobs):
    """A delete that kills the store's database connection and then fails: the bookkeeping after it cannot run either."""

    def __init__(self, dsn):
        super().__init__()
        self.dsn, self.pid = dsn, None

    def delete(self, key):
        if self.pid is not None:
            async def kill():
                c = await asyncpg.connect(self.dsn)
                try:
                    await c.fetchval("SELECT pg_terminate_backend($1)", self.pid)
                finally:
                    await c.close()
            thread, _ = in_thread(kill)
            thread.join()
            raise OSError("object store down")
        super().delete(key)


def test_n10_a_post_commit_bookkeeping_failure_reports_purged_with_a_warning(registry_pg):
    blobs = KillingBlobs(registry_pg["dsn"])

    async def body(store, conn):
        a, va = await publish(store, blobs, owner="o", name="a.md", media="text/markdown", data=data_for("n10"))
        blobs.pid = await conn.fetchval("SELECT pg_backend_pid()")
        out = await store.delete_many([a], mode="purge_now", actor=OWNER, reason="x")
        return a, va, out

    a, va, out = run(with_store(registry_pg["dsn"], blobs, body))
    assert out[0]["result"] == "deleted" and out[0]["warning"] == "cleanup_pending"      # not "error"

    async def check():
        c = await asyncpg.connect(registry_pg["dsn"])
        try:
            assert await c.fetchval("SELECT purged_at IS NOT NULL FROM artifact_versions WHERE version_id=$1",
                                    va["version_id"])
            assert await c.fetchval("SELECT count(*) FROM artifact_pending_deletes WHERE storage_key=$1",
                                    key_of(va)) == 1                               # the next sweep finishes the job
        finally:
            await c.close()
    run(check())


# ------------------------------------------------------------------------------------------------ N12
def test_n12_a_link_is_not_issued_for_an_artifact_deleted_meanwhile(registry_pg):
    blobs = FakeBlobs()

    async def body(store, conn):
        a, va = await publish(store, blobs, owner="o", name="a.md", media="text/markdown", data=data_for("n12"))
        shares = ShareStore(conn)
        share = await shares.create(artifact_id=a, version_id=va["version_id"], audience="lab",
                                    allow_emails=["x@example.org"])
        await store.tombstone_artifact(a, actor=OWNER, reason="gone")
        index = BundleIndex("a.md", [BundleFile("a.md", "a" * 64, 1, "text/markdown")], 1, [])
        with pytest.raises(ShareError):
            await shares.mark_live(share["share_id"], token_hash="a" * 64, index=index, approved_by="op")
        assert (await shares.get(share["share_id"]))["state"] == "pending"

    run(with_store(registry_pg["dsn"], blobs, body))


def test_approving_a_link_and_the_purge_rewrite_of_its_share_row_share_one_lock_order(registry_pg):
    """The purge holds the artifact row and then rewrites the artifact's share rows; an approve used to lock the share row
    and then wait for the artifact row, a deadlock. Both sides must now take the artifact row first (two real connections)."""
    blobs = FakeBlobs()

    async def body(store, conn):
        a, va = await publish(store, blobs, owner="o", name="a.md", media="text/markdown", data=data_for("lockorder"))
        shares = ShareStore(conn)
        share = await shares.create(artifact_id=a, version_id=va["version_id"], audience="lab",
                                    allow_emails=["x@example.org"])
        purge = await asyncpg.connect(registry_pg["dsn"])
        approve = await asyncpg.connect(registry_pg["dsn"])
        try:
            tx = purge.transaction()
            await tx.start()
            await purge.fetchval("SELECT 1 FROM artifacts WHERE artifact_id=$1 FOR UPDATE", a)   # what purge_now holds
            index = BundleIndex("a.md", [BundleFile("a.md", "a" * 64, 1, "text/markdown")], 1, [])
            approving = asyncio.ensure_future(ShareStore(approve).mark_live(
                share["share_id"], token_hash="b" * 64, index=index, approved_by="op"))
            await asyncio.sleep(0.6)                                       # the approve is now waiting on a lock
            await purge.execute("UPDATE artifact_shares SET title=NULL WHERE artifact_id=$1", a)  # anonymisation
            await tx.commit()
            live = await asyncio.wait_for(approving, 10)                   # neither side was aborted as a deadlock victim
            assert live["state"] == "live"
        finally:
            await purge.close()
            await approve.close()

    run(with_store(registry_pg["dsn"], blobs, body))


# ------------------------------------------------------------------------------------------------ N4
def test_n4_a_version_retract_cannot_touch_a_deleted_artifact(registry_pg):
    with api(registry_pg) as env:
        aid, _ = env.seed()
        version_id = env.adapter("list_versions", aid)[0]["version_id"]
        assert delete(env, [aid])[0] == 200
        got, _ = request(env.base, f"{ROOT}/{aid}/retract", key="creator-key", body={"version_id": version_id})
        assert got == 404
        assert env.adapter("get_version", version_id, aid)["state"] != "retracted"
        with pytest.raises(ArtifactError):
            env.adapter("record_retraction", aid, version_id, "x")
        with pytest.raises(ArtifactError):
            env.adapter("record_retraction", aid, None, "x")


# ------------------------------------------------------------------------------------------------ N1
def legacy_upload(env):
    data = b"legacy secret " + uid("d").encode()
    req = urllib.request.Request(env.base + f"{ROOT}?filename=s.txt&mime=text/plain", data=data,
                                 headers={"X-API-Key": "creator-key", "Content-Type": "text/plain"}, method="POST")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())["artifact_id"], data


def status_of(env, path, key="creator-key"):
    req = urllib.request.Request(env.base + path, headers={"X-API-Key": key})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def test_n1_a_deleted_legacy_upload_is_nonexistent_on_every_read_route(registry_pg):
    with api(registry_pg, extra={"artifact_acl": ArtifactAcl()}) as env:
        aid, data = legacy_upload(env)
        reads = [f"{ROOT}/{aid}/download", f"{ROOT}/{aid}/render", f"{ROOT}/{aid}/live", f"{ROOT}/{aid}"]
        assert [status_of(env, p)[0] for p in reads[:1]] == [200]
        assert delete(env, [aid])[0] == 200
        for path in reads:
            got, payload = status_of(env, path)
            assert got == 404 and data not in payload, path
        assert aid not in str(request(env.base, ROOT, key="creator-key")[1])
        assert aid not in str(request(env.base, ROOT, key="owner-key")[1])
        # restoring brings it back at once (the cache is refreshed by the owner's own restore)
        assert request(env.base, f"{ROOT}/restore", body={"ids": [aid]})[0] == 200
        assert status_of(env, f"{ROOT}/{aid}/download") == (200, data)
        assert aid in str(request(env.base, ROOT, key="creator-key")[1])
        # a purge keeps it nonexistent
        assert delete(env, [aid], body={"mode": "purge_now", "confirm": "1"})[0] == 200
        assert status_of(env, f"{ROOT}/{aid}/download")[0] == 404


def test_n1_writes_on_a_deleted_legacy_upload_ask_the_database(registry_pg):
    with api(registry_pg, extra={"artifact_acl": ArtifactAcl()}) as env:
        aid, _ = legacy_upload(env)
        # tombstoned behind the API's back: the read cache may lag, a write must not
        env.adapter("tombstone_artifact", aid, actor=OWNER, reason="gone")
        got, _ = request(env.base, f"{ROOT}/{aid}/retract", key="creator-key", body={"reason": "x"})
        assert got == 404


def test_n1_the_tombstone_cache_fails_closed_inside_the_window_only_and_expires():
    clock = [0.0]
    cache = TombstoneCache(ttl=5.0, clock=lambda: clock[0])

    def broken():
        raise RuntimeError("store down")
    with pytest.raises(RuntimeError):                           # never loaded: the failure propagates (503), no answer
        cache.contains("a", broken)
    assert cache.contains("a", lambda: ["a"]) is True and cache.contains("b", lambda: ["a"]) is False
    clock[0] = 4.0
    assert cache.contains("a", broken) is True                  # inside the staleness window the last answer is fine
    clock[0] = 10.0
    with pytest.raises(RuntimeError):                           # I1: past the window an unrefreshable cache is no answer
        cache.contains("a", broken)
    with pytest.raises(RuntimeError):
        cache.contains("zzz", broken)                           # not even for ids it never listed as tombstoned
    assert cache.contains("a", lambda: []) is False             # a restore elsewhere is seen after the store recovers
    cache.add(["c"])
    assert cache.contains("c", broken) is True
    cache.invalidate()
    with pytest.raises(RuntimeError):                           # an invalidated cache that cannot refresh is no answer
        cache.contains("c", broken)
    assert cache.contains("c", lambda: []) is False


def test_i1_a_tombstoned_artifact_is_never_served_once_the_store_cannot_refresh_the_cache(registry_pg):
    with api(registry_pg, extra={"artifact_acl": ArtifactAcl()}) as env:
        env.server.tombstone_cache = TombstoneCache(ttl=0.3)
        aid, _ = legacy_upload(env)
        assert status_of(env, f"{ROOT}/{aid}")[0] == 200                  # warms the cache: not tombstoned
        # another process tombstones it; this process' cache still says "live" until the window ends
        env.adapter("tombstone_artifact", aid, actor=OWNER, reason="gone")
        # the real store connection dies, so the cache cannot refresh
        asyncio.run_coroutine_threadsafe(env.adapter._store._conn.close(), env.adapter._loop).result()
        time.sleep(0.5)                                                    # past the staleness window
        for path in (f"{ROOT}/{aid}", f"{ROOT}/{aid}/download", f"{ROOT}/{aid}/reviews"):
            code, body = status_of(env, path)
            assert code != 200, (path, code)                               # a dead store never serves the artifact
            assert b"legacy secret" not in body
        # the legacy read path consults only the cache: it answers the fixed 503, not a stale "live"
        assert status_of(env, f"{ROOT}/{aid}/download") == (503, b'{"error": "artifact store unavailable"}')


# ------------------------------------------------------------------------------------------------ N3
def sweep_in_thread(dsn, blobs):
    async def sweep():
        c = await asyncpg.connect(dsn)
        try:
            return await ArtifactStore(c, blobs).drain_pending_deletes()
        finally:
            await c.close()
    return in_thread(sweep)


async def queue_failed_purge(store, blobs, data):
    a, va = await publish(store, blobs, owner="o", name="a.md", media="text/markdown", data=data)
    blobs.fail = True
    await store.purge_now(a, actor=OWNER, reason="x")           # committed, bytes still there, key queued
    blobs.fail = False
    return key_of(va)


class SweepingBlobs(FlakyBlobs):
    """On the ``nth`` head() of ``race_key`` start the real purge sweep on its own connection and wait up to
    ``patience`` seconds for it: an unlocked writer lets it delete the object before the writer commits."""

    def __init__(self, dsn, nth, patience=1.5):
        super().__init__()
        self.dsn, self.nth, self.patience, self.race_key, self.calls_seen, self.sweeper = dsn, nth, patience, None, 0, None

    def head(self, key):
        present = super().head(key)
        if key == self.race_key:
            self.calls_seen += 1
            if self.calls_seen == self.nth:
                self.sweeper = sweep_in_thread(self.dsn, self)
                self.sweeper[0].join(self.patience)
        return present


def assert_twin_keeps_its_bytes(blobs, key, outcome):
    if blobs.sweeper is not None:
        blobs.sweeper[0].join()
    if outcome["version"] is not None:                          # committed: the bytes MUST still be there
        assert blobs.head(key)


@pytest.mark.parametrize("nth", [1, 2])
def test_n3_a_sweep_never_deletes_bytes_a_committing_twin_is_about_to_reference(registry_pg, nth):
    """nth=1: the sweep runs in the gap before the transaction (the commit must notice and refuse). nth=2: it starts
    inside the committing transaction, after the check (the shared digest lock must hold it off until the commit)."""
    blobs = SweepingBlobs(registry_pg["dsn"], nth)
    outcome = {"version": None}

    async def body(store, conn):
        data = data_for("n3")
        key = await queue_failed_purge(store, blobs, data)
        blobs.race_key = key
        try:
            _, outcome["version"] = await publish(store, blobs, owner="o2", name="b.md", media="text/markdown", data=data)
        except ArtifactError:
            pass
        return key

    key = run(with_store(registry_pg["dsn"], blobs, body))
    assert_twin_keeps_its_bytes(blobs, key, outcome)
    if nth == 2:
        assert outcome["version"] is not None                   # the lock made the commit succeed, bytes intact


class DigestLock:
    """A purge sweep caught mid-decision: another connection holding the EXCLUSIVE digest lock in a transaction until
    ``release()`` (which also runs on exit, so a failing assertion never leaves a thread hanging)."""

    def __init__(self, dsn, digest):
        self.locked, self._release = threading.Event(), threading.Event()

        async def hold():
            c = await asyncpg.connect(dsn)
            try:
                async with c.transaction():
                    await c.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", digest)
                    self.locked.set()
                    while not self._release.is_set():
                        await asyncio.sleep(0.02)
            finally:
                await c.close()
        self._thread, _ = in_thread(hold)
        assert self.locked.wait(5)

    def release(self):
        self._release.set()
        self._thread.join(5)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()


def test_n3_register_puts_under_the_digest_lock(registry_pg):
    blobs = FakeBlobs()

    async def body(store, conn):
        data = data_for("n3r")
        key, digest = blobs.put(data)
        blobs.delete(key)                                       # the sweep's decision is already taken
        with DigestLock(registry_pg["dsn"], digest) as sweep:
            task = asyncio.ensure_future(store.register(original_filename="r.txt", mime_type="text/plain", data=data,
                                                        created_by="o"))
            await asyncio.sleep(0.4)
            assert not task.done()                              # blocked behind the sweep
            sweep.release()
            aid = await task
        assert blobs.head(key) and await conn.fetchval("SELECT storage_key FROM artifacts WHERE artifact_id=$1", aid)

    run(with_store(registry_pg["dsn"], blobs, body))


def test_n3_register_report_blob_and_commit_version_wait_for_the_sweep_and_recheck(registry_pg):
    blobs = FakeBlobs()

    async def body(store, conn):
        data = data_for("n3c")
        a, base = await publish(store, blobs, owner="o", name="a.md", media="text/markdown", data=data_for("n3a"))
        key, digest = blobs.put(data)
        with DigestLock(registry_pg["dsn"], digest) as sweep:
            blob_task = asyncio.ensure_future(store.register_report_blob(
                a, storage_key=key, sha256=digest, size_bytes=len(data), media_type="text/markdown"))
            await asyncio.sleep(0.4)
            assert not blob_task.done()
            blobs.delete(key)                                   # the sweep deletes, then commits its transaction
            sweep.release()
            with pytest.raises(ArtifactError):                  # the writer re-checks under the lock: no dangling row
                await blob_task
        assert not await conn.fetchval("SELECT 1 FROM artifact_blobs WHERE scope=$1 AND blob_sha256=$2", a, digest)
        # and the same for a version commit
        key, digest = blobs.put(data)
        entry = {"name": "n.md", "storage_key": key, "sha256": digest, "size_bytes": len(data),
                 "media_type": "text/markdown"}
        with DigestLock(registry_pg["dsn"], digest) as sweep:
            task = asyncio.ensure_future(store.commit_version(
                a, base_version_id=base["version_id"], source_manifest_blob=None, output_manifest=[entry],
                created_by="o"))
            await asyncio.sleep(0.4)
            assert not task.done()
            blobs.delete(key)
            sweep.release()
            with pytest.raises(ArtifactError):
                await task

    run(with_store(registry_pg["dsn"], blobs, body))


def test_the_reviews_list_of_a_legacy_upload_follows_the_acl_when_a_store_exists(registry_pg):
    """A legacy event-projection upload has no registry row: the ACL alone decides, as for artifact-reviews/{id}."""
    from herdr_engineering.control_plane.artifact_reviews import ArtifactReviewManager
    reviews = ArtifactReviewManager()
    with api(registry_pg, extra={"artifact_acl": ArtifactAcl(), "artifact_review_manager": reviews}) as env:
        aid, _ = legacy_upload(env)
        reviews.request(artifact_id=aid, version_id="v1", requested_by="x")
        got, body = request(env.base, f"{ROOT}/{aid}/reviews", key="creator-key")
        assert got == 200 and len(body["reviews"]) == 1
        assert request(env.base, f"{ROOT}/{aid}/reviews", key="member-key")[0] == 404
