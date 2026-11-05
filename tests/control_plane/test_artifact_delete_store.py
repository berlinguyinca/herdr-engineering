"""Owner delete: tombstone/purge-now/restore in bulk on a real PostgreSQL registry (no mocks)."""
import asyncio

import asyncpg
import pytest

from herdr_engineering.control_plane.artifact_lifecycle import MAX_BULK
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


def test_purge_now_tombstones_then_purges_and_keeps_evidence(registry_pg):
    blobs = FakeBlobs()

    async def body(store, connection):
        a, va = await publish(store, blobs, owner="o", name="a.md", media="text/markdown", data=b"only a " + uid("x").encode())
        out = await store.purge_now(a, actor=OWNER, reason="cleanup", request_key="k1")
        assert out["result"] == "deleted" and out["replayed"] is False
        assert not blobs.head(key_of(va))
        version = await store.get_version(va["version_id"])
        assert version["state"] == "purged" and version["output_manifest"] == va["output_manifest"]
        events = await store.list_lifecycle_events(a)
        assert [e["action"] for e in events] == ["tombstone", "purge"]
        assert events[1]["actor"] == OWNER and events[1]["reason"] == "cleanup"
        with pytest.raises(ArtifactError, match="purged"):
            await store.restore_artifact(a, actor=OWNER)
        again = await store.purge_now(a, actor=OWNER, reason="cleanup", request_key="k1")
        assert again["result"] == "already_purged" and again["replayed"] is True
        other_key = await store.purge_now(a, actor=OWNER, reason="cleanup", request_key="k2")
        assert other_key["result"] == "already_purged"
        assert len(await store.list_lifecycle_events(a)) == 2

    run(with_store(registry_pg["dsn"], blobs, body))


def test_purge_now_never_deletes_bytes_another_artifact_uses_even_a_tombstoned_one(registry_pg):
    blobs = FakeBlobs()

    async def body(store, connection):
        shared = b"shared bytes " + uid("s").encode()
        a, va = await publish(store, blobs, owner="o", name="a.md", media="text/markdown", data=shared)
        b, vb = await publish(store, blobs, owner="o", name="b.md", media="text/markdown", data=shared)
        await store.tombstone_artifact(b, actor=OWNER, reason="later")
        out = await store.purge_now(a, actor=OWNER, reason="x")
        assert out["result"] == "deleted" and out["deleted"] == [] and out["retained_shared"] == [key_of(va)]
        assert blobs.head(key_of(vb))                             # b is tombstoned, not purged: restorable
        assert (await store.restore_artifact(b, actor=OWNER))["lifecycle"] == "active"
        assert blobs.head(key_of(vb))
        await store.purge_now(b, actor=OWNER, reason="x")        # a is purged now: nobody else uses the bytes
        assert not blobs.head(key_of(vb))

    run(with_store(registry_pg["dsn"], blobs, body))


def test_purge_now_refuses_a_legal_hold_unless_the_override_is_passed(registry_pg):
    blobs = FakeBlobs()

    async def body(store, connection):
        a, va = await publish(store, blobs, owner="o", name="h.md", media="text/markdown", data=b"held " + uid("h").encode())
        await store.tombstone_artifact(a, actor=OWNER, reason="legal", legal_hold=True)
        out = await store.purge_now(a, actor=OWNER, reason="x")
        assert out["result"] == "refused" and out["reason"] == "legal_hold"
        assert blobs.head(key_of(va))
        out = await store.purge_now(a, actor=OWNER, reason="x", override_legal_hold=True)
        assert out["result"] == "deleted" and not blobs.head(key_of(va))
        events = await store.list_lifecycle_events(a)
        assert events[-1]["action"] == "purge"

    run(with_store(registry_pg["dsn"], blobs, body))


def test_delete_many_returns_one_fixed_result_per_id_and_is_idempotent(registry_pg):
    blobs = FakeBlobs()

    async def body(store, connection):
        a, va = await publish(store, blobs, owner="o", name="a.md", media="text/markdown", data=b"a " + uid("a").encode())
        b, vb = await publish(store, blobs, owner="o", name="b.md", media="text/markdown", data=b"b " + uid("b").encode())
        held, vh = await publish(store, blobs, owner="o", name="h.md", media="text/markdown", data=b"h " + uid("h").encode())
        await store.tombstone_artifact(held, actor=OWNER, reason="legal", legal_hold=True)
        ids = [a, "art-missing", b, a, held]
        out = await store.delete_many(ids, mode="purge_now", actor=OWNER, reason="r", request_key="bulk1")
        assert [r["artifact_id"] for r in out] == [a, "art-missing", b, held]       # duplicates collapse, order kept
        assert [r["result"] for r in out] == ["deleted", "not_found", "deleted", "refused"]
        assert out[3]["reason"] == "legal_hold"
        replay = await store.delete_many(ids, mode="purge_now", actor=OWNER, reason="r", request_key="bulk1")
        assert [r["result"] for r in replay] == ["already_purged", "not_found", "already_purged", "refused"]
        assert all(r["replayed"] for r in replay if r["result"] == "already_purged")
        tomb = await store.delete_many([held], mode="tombstone", actor=OWNER, reason="r", request_key="t1")
        assert tomb[0]["result"] == "tombstoned" and tomb[0]["unchanged"] is True

    run(with_store(registry_pg["dsn"], blobs, body))


def test_delete_many_tombstone_mode_hides_and_restore_many_brings_back(registry_pg):
    blobs = FakeBlobs()

    async def body(store, connection):
        a, va = await publish(store, blobs, owner="o", name="a.md", media="text/markdown", data=b"a " + uid("a").encode())
        r, vr = await publish(store, blobs, owner="o", name="r.md", media="text/markdown", data=b"r " + uid("r").encode())
        await store.record_retraction(r)
        out = await store.delete_many([a, r], mode="tombstone", actor=OWNER, reason="r", request_key="t", hold_days=7)
        assert [x["result"] for x in out] == ["tombstoned", "tombstoned"]
        assert blobs.head(key_of(va))
        assert a not in [x["artifact_id"] for x in await store.list_records(principal_id="o")]
        tombstoned = await store.list_tombstoned()
        assert {a, r} <= {x["artifact_id"] for x in tombstoned}
        row = next(x for x in tombstoned if x["artifact_id"] == a)
        assert row["purge_after"] is not None and row["original_filename"] == "a.md"
        purged, _ = await publish(store, blobs, owner="o", name="p.md", media="text/markdown", data=b"p " + uid("p").encode())
        await store.purge_now(purged, actor=OWNER, reason="x")
        live, _ = await publish(store, blobs, owner="o", name="l.md", media="text/markdown", data=b"l " + uid("l").encode())
        res = await store.restore_many([a, r, purged, live, "art-missing"], actor=OWNER, request_key="rs")
        assert [x["result"] for x in res] == ["restored", "restored", "refused", "refused", "not_found"]
        assert [x.get("reason") for x in res] == [None, None, "purged", "not_tombstoned", None]
        assert a in [x["artifact_id"] for x in await store.list_records(principal_id="o")]
        # A retracted artifact stays retracted (hidden) when it is restored from a tombstone.
        state = await connection.fetchval("SELECT lifecycle FROM artifacts WHERE artifact_id=$1", r)
        assert state == "retracted"
        replay = await store.restore_many([a], actor=OWNER, request_key="rs")
        assert replay[0]["result"] == "restored" and replay[0]["replayed"] is True

    run(with_store(registry_pg["dsn"], blobs, body))


def test_bulk_is_capped(registry_pg):
    blobs = FakeBlobs()

    async def body(store, connection):
        ids = [f"art-{n}" for n in range(MAX_BULK + 1)]
        with pytest.raises(ArtifactError, match="at most"):
            await store.delete_many(ids, mode="tombstone", actor=OWNER, reason="r")
        with pytest.raises(ArtifactError, match="at most"):
            await store.restore_many(ids, actor=OWNER)
        with pytest.raises(ArtifactError, match="mode"):
            await store.delete_many(["x"], mode="shred", actor=OWNER, reason="r")
        with pytest.raises(ArtifactError, match="reason"):
            await store.delete_many(["x"], mode="tombstone", actor=OWNER, reason=" ")

    run(with_store(registry_pg["dsn"], blobs, body))


def test_purge_now_events_are_append_only(registry_pg):
    blobs = FakeBlobs()

    async def body(store, connection):
        a, _ = await publish(store, blobs, owner="o", name="a.md", media="text/markdown", data=b"z " + uid("z").encode())
        await store.purge_now(a, actor=OWNER, reason="x")
        with pytest.raises(asyncpg.PostgresError, match="append-only"):
            await connection.execute("DELETE FROM artifact_lifecycle_events WHERE artifact_id=$1", a)

    run(with_store(registry_pg["dsn"], blobs, body))
