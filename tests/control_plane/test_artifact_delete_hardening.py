"""Regression tests for the review of the owner-delete feature (C1-C3, I3, I4, I6-I8, M1, M7, M9) on a real PostgreSQL."""
import asyncio
import json

import pytest

from herdr_engineering.control_plane.artifacts import ArtifactError
from tests.control_plane import registry_pg as _registry_pg_module
from tests.control_plane.registry_pg import FakeBlobs, uid
from tests.control_plane.test_artifact_registry_ext import publish, with_store

registry_pg = _registry_pg_module.registry_pg      # the module-scoped PostgreSQL fixture
OWNER = "api-key:abc:operator"


def run(coro):
    return asyncio.run(coro)


def key_of(version):
    return version["output_manifest"][0]["storage_key"]


async def seed(store, blobs, tag="x", data=None):
    return await publish(store, blobs, owner="o", name=f"{tag}.md", media="text/markdown",
                         data=data or f"{tag} {uid('d')}".encode())


class FlakyBlobs(FakeBlobs):
    """FakeBlobs whose deletes fail while ``fail`` is set (fault injection for the object store)."""

    fail = False

    def delete(self, key):
        if self.fail:
            raise OSError("object store down")
        super().delete(key)


# ---------------------------------------------------------------- C1
def test_c1_a_request_key_used_for_another_action_is_refused_and_no_bytes_are_lost(registry_pg):
    blobs = FakeBlobs()

    async def body(store, conn):
        a, va = await seed(store, blobs)
        await store.delete_many([a], mode="tombstone", actor=OWNER, reason="r", request_key="K")
        out = await store.delete_many([a], mode="purge_now", actor=OWNER, reason="r", request_key="K")
        assert out[0]["result"] == "refused" and out[0]["reason"] == "request_key_reused"
        assert blobs.head(key_of(va))
        assert (await store.get_version(va["version_id"]))["purged_at"] is None
        out = await store.restore_many([a], actor=OWNER, request_key="K")
        assert out[0]["result"] == "refused" and out[0]["reason"] == "request_key_reused"       # M1
        assert (await store.restore_many([a], actor=OWNER, request_key="K2"))[0]["result"] == "restored"
        out = await store.delete_many([a], mode="tombstone", actor=OWNER, reason="r", request_key="K2")
        assert out[0]["result"] == "refused" and out[0]["reason"] == "request_key_reused"

    run(with_store(registry_pg["dsn"], blobs, body))


def test_c1_the_purge_record_commits_before_bytes_are_deleted_and_a_failed_delete_is_retried(registry_pg):
    blobs = FlakyBlobs()

    async def body(store, conn):
        a, va = await seed(store, blobs)
        blobs.fail = True
        out = await store.purge_now(a, actor=OWNER, reason="r")
        assert out["result"] == "deleted" and out["deleted"] == []
        assert blobs.head(key_of(va))                                        # bytes linger, but the record says purged
        assert (await store.get_version(va["version_id"]))["purged_at"] is not None
        assert (await store.restore_many([a], actor=OWNER))[0]["result"] == "refused"
        assert await conn.fetchval("SELECT count(*) FROM artifact_pending_deletes WHERE storage_key=$1", key_of(va)) == 1
        blobs.fail = False
        assert await store.drain_pending_deletes() == [key_of(va)]
        assert not blobs.head(key_of(va))
        assert await conn.fetchval("SELECT count(*) FROM artifact_pending_deletes") == 0

    run(with_store(registry_pg["dsn"], blobs, body))


def test_c1_a_queued_delete_is_dropped_when_a_live_artifact_now_uses_the_bytes(registry_pg):
    blobs = FlakyBlobs()

    async def body(store, conn):
        shared = b"shared later " + uid("s").encode()
        a, va = await seed(store, blobs, "a", shared)
        blobs.fail = True
        await store.purge_now(a, actor=OWNER, reason="r")
        b, vb = await seed(store, blobs, "b", shared)                         # a new artifact now uses the same bytes
        blobs.fail = False
        assert await store.drain_pending_deletes() == []
        assert blobs.head(key_of(vb))
        assert await conn.fetchval("SELECT count(*) FROM artifact_pending_deletes WHERE storage_key=$1", key_of(vb)) == 0

    run(with_store(registry_pg["dsn"], blobs, body))


# ---------------------------------------------------------------- C2, I8
def test_c2_a_purged_artifact_with_no_versions_cannot_be_restored(registry_pg):
    blobs = FakeBlobs()

    async def body(store, conn):
        art = await store.register_logical(original_filename="r", artifact_type="report", mime_type="text/html",
                                           created_by="o")
        a = art["artifact_id"]
        assert (await store.purge_now(a, actor=OWNER, reason="x"))["result"] == "deleted"
        out = await store.restore_many([a], actor=OWNER)
        assert out[0]["result"] == "refused" and out[0]["reason"] == "purged"
        assert await conn.fetchval("SELECT lifecycle FROM artifacts WHERE artifact_id=$1", a) == "tombstoned"
        with pytest.raises(ArtifactError, match="purged"):
            await store.restore_artifact(a, actor=OWNER)

    run(with_store(registry_pg["dsn"], blobs, body))


def test_i8_the_sweep_purges_a_legacy_artifact_that_has_only_a_storage_key(registry_pg):
    blobs = FakeBlobs()

    async def body(store, conn):
        art = await store.register_logical(original_filename="old.bin", mime_type="application/octet-stream",
                                           created_by="o")
        a = art["artifact_id"]
        key, _ = blobs.put(b"legacy " + uid("l").encode())
        await conn.execute("UPDATE artifacts SET storage_key=$2 WHERE artifact_id=$1", a, key)
        await store.tombstone_artifact(a, actor=OWNER, reason="old")
        await conn.execute("UPDATE artifacts SET purge_after=now() - interval '1 day' WHERE artifact_id=$1", a)
        assert [r["artifact_id"] for r in await store.purge_due()] == [a]
        assert not blobs.head(key)
        assert await store.purge_due() == []
        assert (await store.restore_many([a], actor=OWNER))[0]["reason"] == "purged"

    run(with_store(registry_pg["dsn"], blobs, body))


# ---------------------------------------------------------------- C3
def test_c3_legal_hold_survives_restore_and_every_exit_needs_the_override(registry_pg):
    blobs = FakeBlobs()

    async def body(store, conn):
        a, va = await seed(store, blobs, "h")
        await store.tombstone_artifact(a, actor="o", reason="hold", legal_hold=True)
        assert (await store.purge_now(a, actor=OWNER, reason="x"))["reason"] == "legal_hold"
        out = await store.restore_many([a], actor=OWNER)
        assert out[0]["result"] == "refused" and out[0]["reason"] == "legal_hold"
        out = await store.restore_many([a], actor=OWNER, override_legal_hold=True)
        assert out[0]["result"] == "restored"
        ev = await conn.fetchval("SELECT detail->>'legal_hold_overridden' FROM artifact_lifecycle_events "
                                 "WHERE artifact_id=$1 AND action='restore'", a)
        assert ev == "true"
        assert blobs.head(key_of(va))

    run(with_store(registry_pg["dsn"], blobs, body))


def test_c3_an_active_held_artifact_cannot_be_retombstoned_or_purged_without_the_override(registry_pg):
    blobs = FakeBlobs()

    async def body(store, conn):
        a, va = await seed(store, blobs, "h")
        await store.tombstone_artifact(a, actor="o", reason="hold", legal_hold=True)
        await store.restore_many([a], actor=OWNER, override_legal_hold=True)
        await conn.execute("UPDATE artifacts SET legal_hold=true WHERE artifact_id=$1", a)    # held again, active
        out = await store.delete_many([a], mode="tombstone", actor=OWNER, reason="r", hold_days=1)
        assert out[0]["result"] == "refused" and out[0]["reason"] == "legal_hold"
        out = await store.delete_many([a], mode="purge_now", actor=OWNER, reason="r")
        assert out[0]["result"] == "refused" and out[0]["reason"] == "legal_hold"
        out = await store.delete_many([a], mode="tombstone", actor=OWNER, reason="r", hold_days=1,
                                      override_legal_hold=True)
        assert out[0]["result"] == "tombstoned"
        assert await conn.fetchval("SELECT legal_hold FROM artifacts WHERE artifact_id=$1", a) is False
        out = await store.delete_many([a], mode="purge_now", actor=OWNER, reason="r")
        assert out[0]["result"] == "deleted"

    run(with_store(registry_pg["dsn"], blobs, body))


def test_c3_the_override_is_recorded_on_a_purge_and_the_sweep_never_purges_a_hold(registry_pg):
    blobs = FakeBlobs()

    async def body(store, conn):
        a, va = await seed(store, blobs, "h")
        await store.tombstone_artifact(a, actor="o", reason="hold", legal_hold=True)
        await conn.execute("UPDATE artifacts SET purge_after=now() - interval '1 day' WHERE artifact_id=$1", a)
        assert [r["artifact_id"] for r in await store.purge_due() if r["artifact_id"] == a] == []
        out = await store.purge_now(a, actor=OWNER, reason="x", override_legal_hold=True)
        assert out["result"] == "deleted"
        detail = json.loads(await conn.fetchval(
            "SELECT detail::text FROM artifact_lifecycle_events WHERE artifact_id=$1 AND action='purge'", a))
        assert detail["legal_hold_overridden"] is True

    run(with_store(registry_pg["dsn"], blobs, body))


# ---------------------------------------------------------------- I3
def test_i3_a_tombstone_keeps_at_least_one_day(registry_pg):
    blobs = FakeBlobs()

    async def body(store, conn):
        a, _ = await seed(store, blobs, "m")
        out = await store.delete_many([a], mode="tombstone", actor=OWNER, reason="r", hold_days=0)
        assert out[0]["result"] == "tombstoned"
        days = await conn.fetchval("SELECT extract(epoch FROM purge_after - now())/86400 FROM artifacts "
                                   "WHERE artifact_id=$1", a)
        assert days >= 0.99
        assert await store.purge_due() == [] or a not in [r["artifact_id"] for r in await store.purge_due()]

    run(with_store(registry_pg["dsn"], blobs, body))


# ---------------------------------------------------------------- I4
def test_i4_one_failing_id_never_hides_the_results_of_the_others(registry_pg):
    blobs = FakeBlobs()

    async def body(store, conn):
        a, _ = await seed(store, blobs, "a")
        b, _ = await seed(store, blobs, "b")
        await store.delete_many([b], mode="tombstone", actor=OWNER, reason="r", request_key="K")   # K now means tombstone
        out = await store.delete_many([a, b], mode="purge_now", actor=OWNER, reason="r", request_key="K")
        assert [r["result"] for r in out] == ["deleted", "refused"]
        # a database error on one id becomes an 'error' row, not an exception that discards the rest
        await conn.execute("CREATE OR REPLACE FUNCTION boom() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
                           "RAISE EXCEPTION 'boom'; END $$")
        c, _ = await seed(store, blobs, "c")
        d, _ = await seed(store, blobs, "d")
        await conn.execute(f"CREATE TRIGGER boom BEFORE UPDATE ON artifacts FOR EACH ROW WHEN (OLD.artifact_id='{c}') "
                           "EXECUTE FUNCTION boom()")
        try:
            out = await store.delete_many([c, d], mode="tombstone", actor=OWNER, reason="r")
        finally:
            await conn.execute("DROP TRIGGER boom ON artifacts")
        assert [r["result"] for r in out] == ["error", "tombstoned"]
        assert "boom" not in json.dumps(out)

    run(with_store(registry_pg["dsn"], blobs, body))


# ---------------------------------------------------------------- I6
def test_i6_bytes_staged_by_a_collaborator_or_held_in_a_fabric_blob_row_are_shared(registry_pg):
    blobs = FakeBlobs()

    async def body(store, conn):
        staged = b"staged " + uid("t").encode()
        a, va = await seed(store, blobs, "a", staged)
        await conn.execute(
            "INSERT INTO artifact_collab_uploads (owner_key, upload_id, artifact_id, name, sha256, size_bytes, "
            "storage_key, media_type, charged) VALUES ('agent','up1',$1,'x.md',$2,3,$3,'text/markdown',false)",
            "artifact-other", va["output_manifest"][0]["sha256"], key_of(va))
        out = await store.purge_now(a, actor=OWNER, reason="r")
        assert out["deleted"] == [] and out["retained_shared"] == [key_of(va)]
        assert blobs.head(key_of(va))
        fab, vf = await seed(store, blobs, "f")
        await conn.execute("INSERT INTO artifact_blobs (blob_sha256, scope, storage_key, size_bytes) "
                           "VALUES ($1,'fabric',$2,3)", vf["output_manifest"][0]["sha256"], key_of(vf))
        out = await store.purge_now(fab, actor=OWNER, reason="r")
        assert out["retained_shared"] == [key_of(vf)] and blobs.head(key_of(vf))

    run(with_store(registry_pg["dsn"], blobs, body))


def test_i6b_bytes_shared_with_a_held_or_restorable_tombstoned_artifact_are_kept(registry_pg):
    """Alice and Bob publish the same bytes; Bob's artifact is tombstoned (legal hold, then a plain hold window)."""
    blobs = FakeBlobs()

    async def body(store, conn):
        data = b"shared " + uid("t").encode()
        alice, va = await publish(store, blobs, owner="alice", name="a.md", media="text/markdown", data=data)
        bob, vb = await publish(store, blobs, owner="bob", name="b.md", media="text/markdown", data=data)
        assert key_of(va) == key_of(vb)
        await store.tombstone_artifact(bob, actor="o", reason="hold", legal_hold=True)
        out = await store.purge_now(alice, actor=OWNER, reason="r")
        assert out["deleted"] == [] and out["retained_shared"] == [key_of(va)]
        assert blobs.head(key_of(vb))
        assert (await store.get_version(vb["version_id"]))["purged_at"] is None
        await store.restore_artifact(bob, actor=OWNER, reason="r", override_legal_hold=True)
        assert blobs.head(key_of(vb))
        # once Bob is purged too, nothing references the bytes and they go
        await store.tombstone_artifact(bob, actor="o", reason="again")
        out = await store.purge_now(bob, actor=OWNER, reason="r")
        assert out["deleted"] == [key_of(vb)] and not blobs.head(key_of(vb))

    run(with_store(registry_pg["dsn"], blobs, body))


# ---------------------------------------------------------------- I7
def test_i7_a_tombstoned_or_purged_retracted_artifact_stays_retracted_after_a_reload(registry_pg):
    blobs = FakeBlobs()

    async def body(store, conn):
        a, va = await seed(store, blobs, "r")
        await store.record_retraction(a)
        b, vb = await seed(store, blobs, "v")
        await store.record_retraction(b, vb["version_id"])
        await store.delete_many([a], mode="tombstone", actor=OWNER, reason="r")
        marks = await store.load_retractions()
        assert {"artifact_id": a, "version_id": None} in marks
        await store.purge_now(b, actor=OWNER, reason="r")
        assert {"artifact_id": b, "version_id": vb["version_id"]} in await store.load_retractions()
        await store.purge_now(a, actor=OWNER, reason="r")
        assert {"artifact_id": a, "version_id": None} in await store.load_retractions()

    run(with_store(registry_pg["dsn"], blobs, body))


# ---------------------------------------------------------------- M7, M9, I2
def test_m7_restore_keeps_the_archived_state(registry_pg):
    blobs = FakeBlobs()

    async def body(store, conn):
        a, _ = await seed(store, blobs, "a")
        await store.archive_artifact(a, actor="o")
        await store.delete_many([a], mode="tombstone", actor=OWNER, reason="r")
        assert (await store.restore_many([a], actor=OWNER))[0]["result"] == "restored"
        assert await conn.fetchval("SELECT lifecycle FROM artifacts WHERE artifact_id=$1", a) == "archived"

    run(with_store(registry_pg["dsn"], blobs, body))


def test_m9_refused_attempts_are_recorded_in_the_audit_trail(registry_pg):
    blobs = FakeBlobs()

    async def body(store, conn):
        a, _ = await seed(store, blobs, "a")
        await store.tombstone_artifact(a, actor="o", reason="hold", legal_hold=True)
        await store.purge_now(a, actor="keyless:operator", reason="x")
        events = await store.list_lifecycle_events(a)
        assert events[-1]["action"] == "refused" and events[-1]["actor"] == "keyless:operator"
        assert events[-1]["reason"] == "x"

    run(with_store(registry_pg["dsn"], blobs, body))


def test_i2_a_tombstoned_artifact_refuses_reads_by_permission_and_every_write(registry_pg):
    blobs = FakeBlobs()

    async def body(store, conn):
        a, va = await seed(store, blobs, "a")
        assert await store.report_permission(a, "o", "read") is True
        await store.delete_many([a], mode="tombstone", actor=OWNER, reason="r")
        for permission in ("read", "edit", "admin", "share"):
            assert await store.report_permission(a, "o", permission) is False
        assert await store.get_record(a, principal_id="o") is None
        assert (await store.collab_state(a))["writable"] is False
        key, sha = blobs.put(b"late " + uid("l").encode())
        out = [{"name": "a.md", "storage_key": key, "sha256": sha, "size_bytes": 4, "media_type": "text/markdown"}]
        with pytest.raises(ArtifactError, match="deleted"):
            await store.commit_version(a, base_version_id=va["version_id"], source_manifest_blob=None,
                                       output_manifest=out, created_by="o")
        with pytest.raises(ArtifactError, match="deleted"):
            await store.merge_commit(a, base_version_id=va["version_id"], changes=[], message="m",
                                     collaborator_agent_id="agent", created_by="o")
        await store.restore_many([a], actor=OWNER)
        assert await store.report_permission(a, "o", "read") is True

    run(with_store(registry_pg["dsn"], blobs, body))


def test_i2_purge_blanks_annotation_author_identity(registry_pg):
    blobs = FakeBlobs()

    async def body(store, conn):
        a, va = await seed(store, blobs, "a")
        share = "share_" + uid("s")[:12]
        await conn.execute("INSERT INTO artifact_shares (share_id, artifact_id, version_id, audience, allow_emails, "
                           "created_by) VALUES ($1,$2,$3,'lab',ARRAY['x@example.org'],'o')", share, a, va["version_id"])
        ann = "ann_" + "A" * 22
        await conn.execute(
            "INSERT INTO artifact_annotations (annotation_id, artifact_id, version_id, share_id, file_name, anchor, body, "
            "author_identity, author_email, author_role, created_at) VALUES ($1,$2,$3,$4,'a.md','{}','hello',"
            "'ident','person@example.org','member', now())", ann, a, va["version_id"], share)
        await store.purge_now(a, actor=OWNER, reason="r")
        row = await conn.fetchrow("SELECT author_identity, author_email, body FROM artifact_annotations "
                                  "WHERE annotation_id=$1", ann)
        assert row["author_email"] == "purged" and row["author_identity"] == "purged"

    run(with_store(registry_pg["dsn"], blobs, body))
