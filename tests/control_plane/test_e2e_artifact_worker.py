"""Opt-in worker checks using real migrations/FKs in a dedicated test database."""
import asyncio
import hashlib
import os
import socket
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from herdr_engineering.control_plane.artifact_jobs import ArtifactJobManager
from herdr_engineering.control_plane.artifact_render_runner import ArtifactRenderRunner
from herdr_engineering.control_plane.artifact_renderers import RendererRegistry
from herdr_engineering.control_plane.artifacts import ArtifactError, ArtifactStore
from herdr_engineering.control_plane.db import Migrator
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.control_plane.worker import run_one_pass, run_worker

DSN = os.environ.get("HERDR_E2E_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="requires dedicated HERDR_E2E_PG_DSN")


class Blobs:
    def __init__(self):
        self.objects = {}

    def put(self, data):
        digest = hashlib.sha256(data).hexdigest()
        key = f"sha256/{digest[:2]}/{digest[2:4]}/{digest}"
        self.objects[key] = data
        return key, digest

    def head(self, key):
        return key in self.objects

    def get(self, key, digest):
        data = self.objects[key]
        assert hashlib.sha256(data).hexdigest() == digest
        return data


@pytest.fixture
def isolated_schema():
    import asyncpg

    schema = "artifact_worker_test_" + uuid4().hex

    async def setup():
        conn = await asyncpg.connect(DSN)
        try:
            await conn.execute(f"CREATE SCHEMA {schema}")
            await conn.execute(f"SET search_path TO {schema}")
            await Migrator(conn).apply(str(Path(__file__).parents[2] / "deploy/control-plane/migrations"))
        finally:
            await conn.close()

    async def connect():
        return await asyncpg.connect(DSN, server_settings={"search_path": schema})

    async def cleanup():
        conn = await asyncpg.connect(DSN)
        try:
            await conn.execute(f"DROP SCHEMA {schema} CASCADE")
        finally:
            await conn.close()

    try:
        asyncio.run(setup())
        yield connect
    finally:
        asyncio.run(cleanup())


def test_background_renderer_creates_real_host_and_commits_version(isolated_schema):
    async def main():
        conn = await isolated_schema()
        try:
            blobs = Blobs()
            store = ArtifactStore(conn, blobs)
            artifact = await store.register_logical(original_filename="rendered.html", created_by="owner")
            manager = ArtifactJobManager()
            job = manager.submit(profile="web", artifact_id=artifact["artifact_id"],
                                 base_version_id=None, request={"source": "<html>rendered</html>"},
                                 requested_by="owner")
            await store.record_job_event("created", job)
            assert await conn.fetchval("SELECT count(*) FROM hosts") == 0
            stop = asyncio.Event()
            renderer = ArtifactRenderRunner(store, RendererRegistry())

            async def runner(row):
                try:
                    return await renderer(row)
                finally:
                    stop.set()

            summary_before = await store.get_job(job["job_id"])
            assert summary_before["status"] == "queued"
            await asyncio.wait_for(run_worker(conn, stop=stop, interval=0.01,
                                               worker_id=socket.gethostname(),
                                               artifact_job_runner=runner), timeout=3)
            current = await store.get_job(job["job_id"])
            assert current["status"] == "completed", current
            assert current["started_at"] and current["completed_at"]
            host = await conn.fetchrow("SELECT * FROM hosts WHERE host_id=$1", socket.gethostname())
            assert host["hostname"] == socket.gethostname()
            assert host["status"] == "unknown"
            version = await store.get_version(current["result_version_id"])
            assert version["host_id"] == host["host_id"]
            assert version["job_id"] == job["job_id"]
            output = version["output_manifest"][0]
            assert blobs.get(output["storage_key"], output["sha256"]) == b"<html>rendered</html>"
            manager.load([current])
            assert manager.get(job["job_id"])["status"] == "completed"
            await conn.execute("UPDATE hosts SET status='degraded', hostname='inventory-name'")
            await run_one_pass(conn, worker_id=socket.gethostname(), artifact_job_runner=runner)
            host = await conn.fetchrow("SELECT * FROM hosts")
            assert host["status"] == "degraded" and host["hostname"] == "inventory-name"
            # Exercise the real asyncpg timestamp bindings on manager-originated events.
            second = manager.submit(profile="web", artifact_id=artifact["artifact_id"],
                                    base_version_id=None, request={}, requested_by="owner")
            await store.record_job_event("created", second)
            claimed = manager.claim(second["job_id"], host_id=host["host_id"])
            await store.record_job_event("claimed", claimed)
            assert isinstance(await conn.fetchval("SELECT started_at FROM artifact_build_jobs WHERE job_id=$1", second["job_id"]), datetime)
            cancelled = manager.cancel(second["job_id"])
            await store.record_job_event("cancelled", cancelled)
            assert (await store.get_job(second["job_id"]))["status"] == "cancelled"
            await store.record_acl_event("grant", {"artifact_id": artifact["artifact_id"],
                "principal_id": "owner", "permissions": ["read"], "expires_at": 1900000000.0})
            assert await conn.fetchval("SELECT expires_at FROM artifact_acl") == datetime.fromtimestamp(1900000000, UTC)
        finally:
            await conn.close()
    asyncio.run(main())


def test_binding_context_persists_only_observed_canonical_records(isolated_schema):
    async def main():
        conn = await isolated_schema()
        try:
            repo = ControlPlaneRepo(now=lambda: 1767225600.0)
            mission = repo.upsert_mission("shared", title="Observed mission", stage="testing", source="autospec")
            session = repo.upsert_session("observed-session", mission_id="shared", source="autospec",
                                          host_id="observed-event-only-host", status="idle")
            store = ArtifactStore(conn, Blobs())
            await store.ensure_binding_context(mission=mission, session=session)
            mission_row = await conn.fetchrow("SELECT * FROM missions")
            session_row = await conn.fetchrow("SELECT * FROM sessions")
            assert mission_row["mission_id"] == "autospec:shared"
            assert mission_row["status"] == mission["status"]
            assert mission_row["created_at"] == mission_row["updated_at"] == datetime(2026, 1, 1, tzinfo=UTC)
            assert session_row["mission_id"] == "autospec:shared"
            assert session_row["host_id"] is None
            assert session_row["status"] == "idle"
            assert session_row["started_at"] == session_row["last_activity"] == datetime(2026, 1, 1, tzinfo=UTC)
            assert await conn.fetchval("SELECT count(*) FROM hosts") == 0
            aid = await store.register(original_filename="linked.txt", mime_type="text/plain", data=b"bound bytes",
                                       mission_id="autospec:shared", session_id="observed-session")
            assert await store.download(aid) == b"bound bytes"
            assert await conn.fetchval("SELECT session_id FROM artifact_bindings WHERE artifact_id=$1", aid) == "observed-session"
            await conn.execute("UPDATE missions SET status='completed'")
            await conn.execute("UPDATE sessions SET status='stopped'")
            await store.ensure_binding_context(mission=mission, session=session)
            assert await conn.fetchval("SELECT status FROM missions") == "completed"
            assert await conn.fetchval("SELECT status FROM sessions") == "stopped"
            await store.ensure_binding_context()
            assert await conn.fetchval("SELECT count(*) FROM missions") == 1
        finally:
            await conn.close()
    asyncio.run(main())


def test_job_cas_rejects_stale_manager_and_worker_preserves_cancellation(isolated_schema):
    async def main():
        conn = await isolated_schema()
        peer = await isolated_schema()
        try:
            store = ArtifactStore(conn, Blobs())
            artifact = await store.register_logical(original_filename="race.html")
            manager = ArtifactJobManager()
            original = manager.submit(profile="web", artifact_id=artifact["artifact_id"],
                                      base_version_id=None, request={}, requested_by="owner")
            await store.record_job_event("created", original)
            entered, release = asyncio.Event(), asyncio.Event()

            async def runner(row):
                entered.set()
                await release.wait()
                raise ValueError("late renderer failure")

            task = asyncio.create_task(run_one_pass(conn, worker_id="observed-worker-a", artifact_job_runner=runner))
            try:
                await asyncio.wait_for(entered.wait(), timeout=2)
                contender = await run_one_pass(peer, worker_id="observed-worker-b", artifact_job_runner=runner)
                assert contender["artifact_jobs_claimed"] == 0
                peer_store = ArtifactStore(peer, Blobs())
                await peer_store.record_job_event("cancelled", manager.cancel(original["job_id"]))
            finally:
                release.set()
                result = await task
            assert result["artifact_jobs_failed"] == 0
            assert (await store.get_job(original["job_id"]))["status"] == "cancelled"
            # Stale queued/running snapshots cannot resurrect or replace a terminal job.
            for action, state in (("created", "queued"), ("claimed", "running"),
                                  ("completed", "completed"), ("failed", "failed"),
                                  ("cancelled", "cancelled")):
                with pytest.raises(ArtifactError, match="conflict"):
                    await store.record_job_event(action, {**original, "status": state})
            assert (await store.get_job(original["job_id"]))["status"] == "cancelled"
            # Completion winning first must also reject stale cancellation/failure.
            await conn.execute("UPDATE artifact_build_jobs SET status='completed' WHERE job_id=$1", original["job_id"])
            with pytest.raises(ArtifactError, match="conflict"):
                await store.record_job_event("cancelled", {**original, "status": "cancelled"})
            assert (await store.get_job(original["job_id"]))["status"] == "completed"
        finally:
            await peer.close()
            await conn.close()
    asyncio.run(main())


def test_job_event_allowed_source_states_are_enforced_in_sql(isolated_schema):
    async def main():
        conn = await isolated_schema()
        try:
            store = ArtifactStore(conn, Blobs())
            artifact = await store.register_logical(original_filename="transitions.html")
            targets = {"claimed": "running", "completed": "completed", "failed": "failed", "cancelled": "cancelled"}
            allowed = {"claimed": {"queued"}, "completed": {"running"},
                       "failed": {"queued", "running"}, "cancelled": {"queued", "running"}}
            for action, target in targets.items():
                for previous in ("queued", "running", "completed", "failed", "cancelled"):
                    job = {"job_id": "job_" + uuid4().hex, "artifact_id": artifact["artifact_id"],
                           "base_version_id": None, "profile": "web", "request": {},
                           "status": "queued", "requested_by": "owner", "created_at": 1767225600.0}
                    await store.record_job_event("created", job)
                    await conn.execute("UPDATE artifact_build_jobs SET status=$1 WHERE job_id=$2", previous, job["job_id"])
                    candidate = {**job, "status": target, "started_at": 1767225601.0,
                                 "completed_at": 1767225602.0, "error": "renderer error"}
                    if previous in allowed[action]:
                        await store.record_job_event(action, candidate)
                        assert (await store.get_job(job["job_id"]))["status"] == target
                    else:
                        with pytest.raises(ArtifactError, match="conflict"):
                            await store.record_job_event(action, candidate)
                        assert (await store.get_job(job["job_id"]))["status"] == previous
                    with pytest.raises(ArtifactError, match="conflict"):
                        await store.record_job_event("created", job)
            assert await conn.fetchval("SELECT created_at FROM artifact_build_jobs LIMIT 1") == datetime(2026, 1, 1, tzinfo=UTC)
        finally:
            await conn.close()
    asyncio.run(main())


def test_session_without_mission_projection_keeps_provenance_and_allows_binding(isolated_schema):
    import json

    async def main():
        conn = await isolated_schema()
        try:
            store = ArtifactStore(conn, Blobs())
            session = {"session_id": "early-session", "mission_id": "not-yet-projected",
                       "mission_key": "autospec:not-yet-projected", "mission_source": "autospec",
                       "status": "active", "created_at": "2026-01-01T00:00:00+00:00",
                       "updated_at": "2026-01-01T00:00:01+00:00"}
            await store.ensure_binding_context(session=session)
            row = await conn.fetchrow("SELECT * FROM sessions WHERE session_id=$1", session["session_id"])
            assert row["mission_id"] is None
            assert json.loads(row["payload"]) == session
            assert await conn.fetchval("SELECT count(*) FROM missions") == 0
            aid = await store.register(original_filename="early.txt", mime_type="text/plain",
                                       data=b"early session upload", session_id=session["session_id"])
            assert await store.download(aid) == b"early session upload"
            assert await conn.fetchval("SELECT session_id FROM artifact_bindings WHERE artifact_id=$1", aid) == session["session_id"]
            # A subsequent session can reference the exact SQL mission once it exists.
            await store.ensure_binding_context(mission={"mission_id": "not-yet-projected",
                "mission_key": "autospec:not-yet-projected", "title": "Now observed",
                "status": "active", "created_at": session["created_at"], "updated_at": session["updated_at"]})
            later = {**session, "session_id": "later-session"}
            await store.ensure_binding_context(session=later)
            assert await conn.fetchval("SELECT mission_id FROM sessions WHERE session_id=$1", later["session_id"]) == session["mission_key"]
            assert await conn.fetchval("SELECT count(*) FROM missions") == 1
        finally:
            await conn.close()
    asyncio.run(main())


def test_review_notes_survive_durable_manager_reload(isolated_schema):
    from herdr_engineering.control_plane.artifact_reviews import ArtifactReviewManager

    async def main():
        conn = await isolated_schema()
        try:
            store = ArtifactStore(conn, Blobs())
            artifact = await store.register_logical(original_filename="reviewed.txt", created_by="owner")
            version = await store.commit_version(artifact["artifact_id"], base_version_id=None,
                                                  source_manifest_blob=None, output_manifest=[], created_by="owner")
            manager = ArtifactReviewManager()
            review = manager.request(artifact_id=artifact["artifact_id"], version_id=version["version_id"],
                                     requested_by="owner")
            await store.record_review_event("request", review)
            notes = ["Approved after checking outputs", "Retain the source manifest"]
            completed = manager.complete(review["review_id"], reviewer="reviewer", decision="approved", comments=notes)
            await store.record_review_event("complete", completed)
        finally:
            await conn.close()
        # Reopen the DB connection as well as rebuilding the manager.
        conn = await isolated_schema()
        try:
            restored = ArtifactReviewManager()
            restored.load(await ArtifactStore(conn, Blobs()).load_reviews())
            review = restored.get(review["review_id"])
            assert review["comments"] == notes
            assert review["state"] == "approved"
            assert review["version_id"] == version["version_id"]
            assert review["completed_at"]
        finally:
            await conn.close()
    asyncio.run(main())
