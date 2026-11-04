# ruff: noqa: F811  (pytest fixtures are imported by name from registry_pg)
"""P03: the artifact reconcile pass runs on a schedule from the worker (off unless the fleet file turns it on)."""
import asyncio

import asyncpg
import pytest

from herdr_engineering import cli
from herdr_engineering.control_plane import worker
from herdr_engineering.control_plane.artifacts import ArtifactStore
from herdr_engineering.control_plane.worker import ArtifactReconciler
from tests.control_plane.registry_pg import (  # noqa: F401 - imported fixture
    FakeBlobs,
    registry_pg,
    uid,
)


class Store:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    async def reconcile(self, **kw):
        self.calls.append(kw)
        if self.fail:
            raise RuntimeError("database went away")
        return {"orphans_marked": 0}


def test_the_tick_runs_at_most_once_per_interval_and_passes_its_limits():
    clock, store = [1000.0], Store()
    rec = ArtifactReconciler(lambda conn: store, interval=300, grace_seconds=30, orphan_ttl_seconds=7200, limit=7,
                             clock=lambda: clock[0])
    assert asyncio.run(rec.tick(object())) == {"orphans_marked": 0}
    assert asyncio.run(rec.tick(object())) == "skipped"
    clock[0] += 299
    assert asyncio.run(rec.tick(object())) == "skipped"
    clock[0] += 2
    assert asyncio.run(rec.tick(object())) == {"orphans_marked": 0}
    assert len(store.calls) == 2 and store.calls[0] == {"grace_seconds": 30, "orphan_ttl_seconds": 7200, "limit": 7}


def test_a_failing_reconcile_never_escapes_the_worker_pass():
    rec = ArtifactReconciler(lambda conn: Store(fail=True), interval=60)
    assert asyncio.run(rec.tick(object())) == "error"
    out = asyncio.run(worker.run_one_pass(object(), artifact_reconciler=rec))
    assert out["artifact_reconcile"] in ("error", "skipped")        # the pass itself returned a summary


def test_the_pass_reports_the_reconcile_only_when_one_is_configured():
    rec = ArtifactReconciler(lambda conn: Store(), interval=60)
    assert "artifact_reconcile" in asyncio.run(worker.run_one_pass(object(), artifact_reconciler=rec))
    assert "artifact_reconcile" not in asyncio.run(worker.run_one_pass(object()))


def test_settings_are_off_by_default_and_only_enabled_by_exactly_true():
    assert cli._artifact_reconcile_settings({}) is None
    assert cli._artifact_reconcile_settings({"artifact_reconcile": {"interval_seconds": 120}}) is None
    assert cli._artifact_reconcile_settings({"artifact_reconcile": {"enabled": "yes"}}) is None
    on = cli._artifact_reconcile_settings({"artifact_reconcile": {"enabled": True}})
    assert on == {"interval": 300.0, "grace_seconds": 60.0, "orphan_ttl_seconds": 86400.0, "limit": 100}
    tuned = cli._artifact_reconcile_settings({"artifact_reconcile": {
        "enabled": True, "interval_seconds": 900, "grace_seconds": 5, "orphan_ttl_hours": 2, "limit": 10}})
    assert tuned == {"interval": 900.0, "grace_seconds": 5.0, "orphan_ttl_seconds": 7200.0, "limit": 10}
    bad = cli._artifact_reconcile_settings({"artifact_reconcile": {"enabled": True, "interval_seconds": 5,
                                                                   "limit": True, "orphan_ttl_hours": -1}})
    assert bad == on                                                # out-of-range values fall back, never run hot
    with pytest.raises(SystemExit):
        cli._artifact_reconcile_settings({"artifact_reconcile": ["not", "a", "mapping"]})


def test_a_scheduled_tick_marks_an_orphaned_blob_in_a_real_database(registry_pg):
    blobs = FakeBlobs()

    async def body():
        conn = await asyncpg.connect(registry_pg["dsn"])
        try:
            store = ArtifactStore(conn, blobs)
            artifact_id = (await store.register_logical(original_filename="a.md", created_by="o",
                                                        mime_type="text/markdown"))["artifact_id"]
            data = b"stray upload " + uid("x").encode()
            key, sha = blobs.put(data)
            await store.register_report_blob(artifact_id, storage_key=key, sha256=sha, size_bytes=len(data),
                                             media_type="text/plain")
            await conn.execute("UPDATE artifact_blobs SET created_at=now() - interval '2 days' WHERE scope=$1 AND blob_sha256=$2",
                               artifact_id, sha)
            clock = [5000.0]
            rec = ArtifactReconciler(lambda c: ArtifactStore(c, blobs), interval=600, orphan_ttl_seconds=3600,
                                     clock=lambda: clock[0])
            first = await rec.tick(conn)
            assert first["orphans_marked"] >= 1 and blobs.head(key)             # marked, never deleted
            assert [o["blob_sha256"] for o in await store.list_orphan_blobs() if o["artifact_id"] == artifact_id] == [sha]
            assert await rec.tick(conn) == "skipped"                          # inside the interval
        finally:
            await conn.close()

    asyncio.run(body())
