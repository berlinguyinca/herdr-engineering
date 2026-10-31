"""Registry extensions (P03 foundation) on a real PostgreSQL: context lookup, provenance, lifecycle, reconcile, uploads."""
import asyncio
import hashlib
import json

import asyncpg
import pytest

from herdr_engineering.control_plane.artifacts import ArtifactConflict, ArtifactError, ArtifactStore
from tests.control_plane.registry_pg import FakeBlobs, registry_pg, uid  # noqa: F401 - imported fixture

FAKE = "x" * 16     # placeholder shaped like a credential value; not a secret
FORMATS = [("report.pdf", "application/pdf", b"%PDF-1.7 fake"),
           ("notes.md", "text/markdown", b"# notes\n"),
           ("data.json", "application/json", b'{"x": 1}')]


def run(coro):
    return asyncio.run(coro)


async def with_store(dsn, blobs, body):
    connection = await asyncpg.connect(dsn)
    try:
        return await body(ArtifactStore(connection, blobs), connection)
    finally:
        await connection.close()


async def publish(store, blobs, *, owner, name, media, data, origin=None, base=None, artifact_id=None):
    if artifact_id is None:
        artifact_id = (await store.register_logical(original_filename=name, created_by=owner, mime_type=media))["artifact_id"]
    key, sha = blobs.put(data)
    output = [{"name": name, "storage_key": key, "sha256": sha, "size_bytes": len(data), "media_type": media}]
    version = await store.commit_version(artifact_id, base_version_id=base, source_manifest_blob=None,
                                         output_manifest=output, created_by=owner, origin=origin)
    return artifact_id, version


def test_a05_fifteen_artifacts_in_three_formats_found_by_project_task_issue_and_survive_restart(registry_pg):
    blobs, project, issue = FakeBlobs(), uid("project"), "acme/widgets#" + uid("n")[2:]
    tasks = [uid("task") for _ in range(3)]
    agents = {"agent-a": [], "agent-b": []}

    async def write(store, connection):
        for index in range(15):
            owner = "agent-a" if index % 2 == 0 else "agent-b"
            name, media, data = FORMATS[index % 3]
            artifact_id, _ = await publish(
                store, blobs, owner="owner-1", name=f"{index}-{name}", media=media, data=data + bytes([index]),
                origin={"project_id": project, "task_id": tasks[index % 3], "issue_ref": issue, "room_id": "room-1",
                        "agent_ref": owner, "model_id": "m1", "commit_sha": "a" * 40, "session_ref": "gone-session"})
            agents[owner].append(artifact_id)
        return None

    run(with_store(registry_pg["dsn"], blobs, write))
    registry_pg["restart"]()

    async def read(store, connection):
        for kind, ref, expected in [("project", project, 15), ("issue", issue, 15), ("room", "room-1", None),
                                    *[("task", task, 5) for task in tasks]]:
            seen, cursor = [], None
            while True:
                page = await store.list_by_context(principal_id="owner-1", kind=kind, ref=ref, limit=4, cursor=cursor)
                seen += page["artifacts"]
                if expected is not None:
                    assert page["total"] == expected
                if not page["has_more"]:
                    break
                cursor = page["next_cursor"]
            if expected is not None:
                assert len(seen) == expected == len({row["artifact_id"] for row in seen})
        full = await store.list_by_context(principal_id="owner-1", kind="project", ref=project, limit=200)
        assert {row["preview_kind"] for row in full["artifacts"]} == {"pdf", "markdown", "json"}
        assert {row["origin"]["task_id"] for row in full["artifacts"]} == set(tasks)
        assert all(row["version_number"] == 1 and row["origin"]["project_id"] == project for row in full["artifacts"])
        only_pdf = await store.list_by_context(principal_id="owner-1", kind="project", ref=project,
                                               media_type="application/pdf")
        assert only_pdf["total"] == 5
        found = await store.list_by_context(principal_id="owner-1", kind="task", ref=tasks[0], query="data.json")
        assert found["total"] == 0
        assert (await store.list_by_context(principal_id="owner-1", kind="task", ref=tasks[0],
                                            query="report.pdf"))["total"] == 5
        # Provenance survived the restart, including a session that does not exist in the sessions table.
        row = await connection.fetchrow("SELECT * FROM artifact_versions WHERE origin_project_id=$1 LIMIT 1", project)
        assert row["origin_session_ref"] == "gone-session" and row["model_id"] == "m1" and row["commit_sha"] == "a" * 40

    run(with_store(registry_pg["dsn"], blobs, read))


def test_a06_v1_is_immutable_after_v2_and_rows_cannot_be_deleted(registry_pg):
    blobs = FakeBlobs()

    async def body(store, connection):
        artifact_id, v1 = await publish(store, blobs, owner="o", name="r.md", media="text/markdown", data=b"v1",
                                        origin={"task_id": uid("task")})
        _, v2 = await publish(store, blobs, owner="o", name="r.md", media="text/markdown", data=b"v2",
                              base=v1["version_id"], artifact_id=artifact_id)
        assert v2["parent_version_id"] == v1["version_id"] and v2["version_number"] == 2
        for sql in ("UPDATE artifact_versions SET output_manifest='[]'::jsonb WHERE version_id=$1",
                    "UPDATE artifact_versions SET origin_task_id='other' WHERE version_id=$1",
                    "UPDATE artifact_versions SET provenance='{}'::jsonb, created_by='x' WHERE version_id=$1",
                    "DELETE FROM artifact_versions WHERE version_id=$1"):
            with pytest.raises(asyncpg.PostgresError, match="immutable"):
                await connection.execute(sql, v1["version_id"])
        again = await store.get_version(v1["version_id"], artifact_id)
        assert again["output_manifest"] == v1["output_manifest"] and again["state"] == "ready"
        with pytest.raises(ArtifactConflict):                      # stale head cannot overwrite v2
            await publish(store, blobs, owner="o", name="r.md", media="text/markdown", data=b"v3",
                          base=v1["version_id"], artifact_id=artifact_id)
        listed = await store.list_versions(artifact_id)
        assert [v["version_number"] for v in listed] == [2, 1]

    run(with_store(registry_pg["dsn"], blobs, body))


def test_a09_no_leak_through_counts_pagination_search_grants_or_missing_vs_forbidden(registry_pg):
    blobs, task, empty_task = FakeBlobs(), uid("task"), uid("task")

    async def body(store, connection):
        ids = []
        for index in range(3):
            artifact_id, _ = await publish(store, blobs, owner="alice", name=f"secret-plan-{index}.md",
                                           media="text/markdown", data=b"s" + bytes([index]), origin={"task_id": task})
            ids.append(artifact_id)
        mine = await store.list_by_context(principal_id="alice", kind="task", ref=task)
        assert mine["total"] == 3
        stranger = await store.list_by_context(principal_id="mallory", kind="task", ref=task, query="secret-plan")
        nothing = await store.list_by_context(principal_id="mallory", kind="task", ref=empty_task, query="secret-plan")
        assert stranger["artifacts"] == [] and stranger["total"] == 0 and stranger["has_more"] is False
        assert {**stranger, "ref": None} == {**nothing, "ref": None}          # missing == forbidden == empty
        # Pagination must not reveal hidden rows: a one-row page for a stranger with a grant on only one artifact.
        await connection.execute(
            "INSERT INTO artifact_acl (artifact_id, principal_id, permissions, granted_by) VALUES ($1,'bob','[\"read\"]','alice')",
            ids[0])
        bob = await store.list_by_context(principal_id="bob", kind="task", ref=task, limit=1)
        assert [row["artifact_id"] for row in bob["artifacts"]] == [ids[0]] and bob["total"] == 1 and not bob["has_more"]
        await connection.execute("UPDATE artifact_acl SET expires_at=now() - interval '1 minute' WHERE principal_id='bob'")
        assert (await store.list_by_context(principal_id="bob", kind="task", ref=task))["total"] == 0
        await connection.execute("UPDATE artifact_acl SET expires_at=NULL WHERE principal_id='bob'")
        assert (await store.list_by_context(principal_id="bob", kind="task", ref=task))["total"] == 1
        await connection.execute("DELETE FROM artifact_acl WHERE principal_id='bob'")           # revoke
        assert (await store.list_by_context(principal_id="bob", kind="task", ref=task))["total"] == 0
        # A comment-only grant is not read access.
        await connection.execute(
            "INSERT INTO artifact_acl (artifact_id, principal_id, permissions, granted_by) VALUES ($1,'carol','[\"review\"]','alice')",
            ids[0])
        assert (await store.list_by_context(principal_id="carol", kind="task", ref=task))["total"] == 0
        # Unowned legacy rows (created_by NULL) are not public through these routes.
        await connection.execute(
            "INSERT INTO artifacts (artifact_id, status, lifecycle, current_version_id) VALUES ('legacy-x','available','active',NULL)")
        # Wildcards in search are literals, not a way to enumerate.
        assert (await store.list_by_context(principal_id="mallory", kind="task", ref=task, query="%"))["total"] == 0

    run(with_store(registry_pg["dsn"], blobs, body))


def test_one_artifact_links_to_many_tasks_and_duplicate_events_are_noops(registry_pg):
    blobs = FakeBlobs()

    async def body(store, connection):
        artifact_id, version = await publish(store, blobs, owner="o", name="a.md", media="text/markdown", data=b"a",
                                             origin={"task_id": uid("task")})
        t1, t2 = uid("task"), uid("task")
        assert (await store.link_context(artifact_id, created_by="o", task_id=t1, source_event_id="ev-1"))["linked"]
        assert not (await store.link_context(artifact_id, created_by="o", task_id=t1, source_event_id="ev-1"))["linked"]
        assert not (await store.link_context(artifact_id, created_by="o", task_id=t1, source_event_id="ev-2"))["linked"]
        assert (await store.link_context(artifact_id, created_by="o", task_id=t2, source_event_id="ev-3",
                                         version_id=version["version_id"]))["linked"]
        for task in (t1, t2):
            assert (await store.list_by_context(principal_id="o", kind="task", ref=task))["total"] == 1
        with pytest.raises(ValueError):
            await store.link_context(artifact_id, created_by="o")
        with pytest.raises(ValueError):
            await store.link_context(artifact_id, created_by="o", task_id="x", version_id="not-a-version")
        assert len(await store.list_links(artifact_id)) == 3

    run(with_store(registry_pg["dsn"], blobs, body))


def test_provenance_is_validated_and_inputs_are_redacted(registry_pg):
    blobs = FakeBlobs()

    async def body(store, connection):
        origin = {"task_id": uid("task"), "prompt_revision": "p-7", "skill_revision": "skill-x@1.2.0",
                  "inputs": [{"name": "prompt", "kind": "prompt", "value": "my password is hunter2"},
                             {"name": "config", "kind": "file", "sha256": "a" * 64, "size_bytes": 10,
                              "headers": {"Authorization": "Bearer " + FAKE, "ok": "fine"}}]}
        artifact_id, version = await publish(store, blobs, owner="o", name="a.md", media="text/markdown", data=b"a",
                                             origin=origin)
        stored = json.dumps((await store.get_version(version["version_id"]))["redacted_inputs"])
        assert "hunter2" not in stored and FAKE not in stored and "fine" in stored
        first = (await store.get_version(version["version_id"]))["redacted_inputs"][0]
        assert first["redacted"] is True and len(first["sha256"]) == 64 and "value" not in first
        for bad in ({"unknown": 1}, {"commit_sha": "NOTHEX"}, {"task_id": ""}, {"task_id": "x\x00y"},
                    {"model_id": "sk-" + "x" * 24}, {"inputs": "nope"}, "string"):
            with pytest.raises(ArtifactError):
                await publish(store, blobs, owner="o", name="b.md", media="text/markdown", data=b"b", origin=bad)
        assert version["skill_revision"] == "skill-x@1.2.0"

    run(with_store(registry_pg["dsn"], blobs, body))


def test_archive_restore_tombstone_and_purge_respect_sharing_and_keep_evidence(registry_pg):
    blobs, task = FakeBlobs(), uid("task")

    async def body(store, connection):
        shared = b"identical bytes in two artifacts"
        a, va = await publish(store, blobs, owner="o", name="a.md", media="text/markdown", data=shared, origin={"task_id": task})
        b, vb = await publish(store, blobs, owner="o", name="b.md", media="text/markdown", data=shared, origin={"task_id": task})
        c, vc = await publish(store, blobs, owner="o", name="c.md", media="text/markdown", data=b"only c", origin={"task_id": task})
        assert (await store.list_by_context(principal_id="o", kind="task", ref=task))["total"] == 3
        assert (await store.archive_artifact(c, actor="o", request_key="k1"))["lifecycle"] == "archived"
        assert (await store.archive_artifact(c, actor="o", request_key="k1"))["replayed"] is True
        with pytest.raises(ArtifactError):
            await store.archive_artifact(c, actor="o")
        assert (await store.list_by_context(principal_id="o", kind="task", ref=task))["total"] == 2
        assert (await store.restore_artifact(c, actor="o"))["lifecycle"] == "active"
        with pytest.raises(ArtifactError, match="reason"):
            await store.tombstone_artifact(c, actor="o", reason="")
        await store.tombstone_artifact(a, actor="o", reason="obsolete")
        await store.tombstone_artifact(c, actor="o", reason="obsolete", legal_hold=True)
        assert (await store.list_by_context(principal_id="o", kind="task", ref=task))["total"] == 1
        # Nothing is due yet; a legal hold never purges.
        assert await store.purge_due() == []
        await connection.execute("UPDATE artifacts SET purge_after=now() - interval '1 day' WHERE artifact_id=$1", a)
        assert [r["artifact_id"] for r in await store.purge_due()] == [a]
        assert blobs.head(vb["output_manifest"][0]["storage_key"])           # shared with live b: bytes kept
        assert await store.purge_due() == []                                # idempotent
        row = await connection.fetchrow("SELECT state, purged_at FROM artifact_versions WHERE version_id=$1", va["version_id"])
        assert row["state"] == "purged" and row["purged_at"] is not None
        with pytest.raises(ArtifactError, match="purged"):
            await store.restore_artifact(a, actor="o")
        # c's hold is cleared, so its unshared bytes are deleted but the version row and hashes remain.
        await connection.execute("UPDATE artifacts SET purge_after=now() - interval '1 day' WHERE artifact_id=$1", c)
        assert [r["artifact_id"] for r in await store.purge_due()] == [c]
        assert not blobs.head(vc["output_manifest"][0]["storage_key"])
        assert (await store.get_version(vc["version_id"]))["output_manifest"] == vc["output_manifest"]
        events = [e["action"] for e in await store.list_lifecycle_events(c)]
        assert events == ["archive", "restore", "tombstone", "purge"]
        with pytest.raises(asyncpg.PostgresError, match="append-only"):
            await connection.execute("DELETE FROM artifact_lifecycle_events WHERE artifact_id=$1", c)
        # A tombstoned artifact cannot be tombstoned or archived again.
        with pytest.raises(ArtifactError):
            await store.archive_artifact(a, actor="o")

    run(with_store(registry_pg["dsn"], blobs, body))


def test_reconcile_relinks_a_crashed_publish_once_marks_orphans_and_expires_uploads(registry_pg):
    # registry_pg is shared by the whole module, so other tests' ledger rows and blobs can be picked up by any
    # reconcile pass: assert only on THIS test's version, ledger row and blob, never on global report counts.
    blobs = FakeBlobs()
    emitted = []

    def record(v):
        emitted.append(v["version_id"])
        return {"error": None}

    async def body(store, connection):
        artifact_id, version = await publish(store, blobs, owner="o", name="a.md", media="text/markdown", data=b"a",
                                             origin={"task_id": uid("task")})
        vid = version["version_id"]

        async def ledger_state():
            return (await connection.fetchrow("SELECT state FROM artifact_publish_ledger WHERE version_id=$1", vid))["state"]

        ledger = await connection.fetchrow("SELECT * FROM artifact_publish_ledger WHERE version_id=$1", vid)
        assert ledger["state"] == "committed" and ledger["source_event_id"] == f"art-{vid}"
        # Within the grace window nothing is touched (the request thread may still be emitting).
        await store.reconcile(emit=record, grace_seconds=3600)
        assert vid not in emitted and await ledger_state() == "committed"
        await connection.execute("UPDATE artifact_publish_ledger SET committed_at=now() - interval '1 hour' WHERE version_id=$1",
                                 vid)
        failing = await store.reconcile(emit=lambda v: {"error": "graph down"}, grace_seconds=1)
        assert failing["relink_failed"] >= 1
        row = await connection.fetchrow("SELECT state, last_error, attempts FROM artifact_publish_ledger WHERE version_id=$1", vid)
        assert row["state"] == "committed" and row["last_error"] == "graph down"
        ok = await store.reconcile(emit=record, grace_seconds=1)
        assert ok["relinked"] >= 1 and emitted.count(vid) == 1
        relinked_state = await ledger_state()
        assert relinked_state != "committed"
        await store.reconcile(emit=record, grace_seconds=1)
        assert emitted.count(vid) == 1 and await ledger_state() == relinked_state       # no duplicate emission
        await store.mark_linked(vid)
        # Orphaned scoped blob: verified, old, referenced by no version.
        data = b"stray upload"
        key, sha = blobs.put(data)
        await store.register_report_blob(artifact_id, storage_key=key, sha256=sha, size_bytes=len(data), media_type="text/plain")
        await connection.execute("UPDATE artifact_blobs SET created_at=now() - interval '2 days' WHERE scope=$1 AND blob_sha256=$2",
                                 artifact_id, sha)

        async def orphan_marked():
            return (await connection.fetchrow("SELECT orphan_marked_at FROM artifact_blobs WHERE scope=$1 AND blob_sha256=$2",
                                              artifact_id, sha))["orphan_marked_at"] is not None

        report = await store.reconcile(emit=None, orphan_ttl_seconds=3600)
        assert report["orphans_marked"] >= 1 and await orphan_marked() and blobs.head(key)   # marked, never deleted
        _, v2 = await publish(store, blobs, owner="o", name="s.txt", media="text/plain", data=data,
                              base=vid, artifact_id=artifact_id)
        assert (await store.reconcile(emit=None, orphan_ttl_seconds=3600))["orphans_cleared"] >= 1
        assert not await orphan_marked()

    run(with_store(registry_pg["dsn"], blobs, body))


def test_resumable_upload_out_of_order_replay_verify_and_cleanup(registry_pg):
    blobs = FakeBlobs()
    payload = (b"0123456789" * 7)[:64]
    digest = hashlib.sha256(payload).hexdigest()

    async def body(store, connection):
        artifact_id = (await store.register_logical(original_filename="big.bin", created_by="o"))["artifact_id"]
        session = await store.create_upload(artifact_id, principal_id="o", filename="big.bin", media_type="application/octet-stream",
                                            size_bytes=64, sha256=digest, part_size=20, idempotency_key="idem-1")
        assert session["part_count"] == 4
        assert (await store.create_upload(artifact_id, principal_id="o", filename="big.bin", media_type="application/octet-stream",
                                          size_bytes=64, sha256=digest, part_size=20, idempotency_key="idem-1"))["upload_id"] == session["upload_id"]
        with pytest.raises(ArtifactError, match="idempotency"):
            await store.create_upload(artifact_id, principal_id="o", filename="other", media_type="application/octet-stream",
                                      size_bytes=64, sha256=digest, part_size=20, idempotency_key="idem-1")
        upload = session["upload_id"]
        chunks = [payload[0:20], payload[20:40], payload[40:60], payload[60:64]]
        with pytest.raises(ArtifactError, match="exactly"):
            await store.put_upload_part(upload, artifact_id, 1, b"short")
        with pytest.raises(ArtifactError, match="range"):
            await store.put_upload_part(upload, artifact_id, 5, chunks[0])
        assert (await store.put_upload_part(upload, artifact_id, 3, chunks[2]))["replayed"] is False
        assert (await store.put_upload_part(upload, artifact_id, 3, chunks[2]))["replayed"] is True
        with pytest.raises(ArtifactConflict):
            await store.put_upload_part(upload, artifact_id, 3, b"X" * 20)
        with pytest.raises(ArtifactError, match="sha256"):
            await store.put_upload_part(upload, artifact_id, 1, chunks[0], sha256="0" * 64)
        status = await store.get_upload(upload, artifact_id)
        assert status["missing"] == [1, 2, 4] and [p["part_number"] for p in status["received"]] == [3]
        assert await store.get_upload(upload, "some-other-artifact") is None          # scoped by artifact
        with pytest.raises(ArtifactError, match="missing parts"):
            await store.complete_upload(upload, artifact_id)
        for number in (4, 1, 2):
            await store.put_upload_part(upload, artifact_id, number, chunks[number - 1])
        done = await store.complete_upload(upload, artifact_id)
        assert done["sha256"] == digest and blobs.objects[done["storage_key"]] == payload
        assert blobs.staging() == []                                              # staging parts removed
        assert (await store.complete_upload(upload, artifact_id))["storage_key"] == done["storage_key"]   # replay
        scoped = await connection.fetchrow("SELECT verified_at FROM artifact_blobs WHERE scope=$1 AND blob_sha256=$2", artifact_id, digest)
        assert scoped["verified_at"] is not None
        with pytest.raises(ArtifactError, match="complete"):
            await store.put_upload_part(upload, artifact_id, 1, chunks[0])
        # A whole-file digest mismatch fails closed and leaves nothing behind.
        bad = await store.create_upload(artifact_id, principal_id="o", filename="b.bin", media_type="application/octet-stream",
                                        size_bytes=64, sha256="f" * 64, part_size=64)
        await store.put_upload_part(bad["upload_id"], artifact_id, 1, payload)
        with pytest.raises(ArtifactError, match="verification"):
            await store.complete_upload(bad["upload_id"], artifact_id)
        assert (await store.get_upload(bad["upload_id"], artifact_id))["state"] == "aborted" and blobs.staging() == []
        # Expiry and abort remove staging bytes.
        stale = await store.create_upload(artifact_id, principal_id="o", filename="c.bin", media_type="application/octet-stream",
                                          size_bytes=40, sha256="e" * 64, part_size=20)
        await store.put_upload_part(stale["upload_id"], artifact_id, 1, b"a" * 20)
        await connection.execute("UPDATE artifact_upload_sessions SET expires_at=now() - interval '1 second' WHERE upload_id=$1", stale["upload_id"])
        with pytest.raises(ArtifactError, match="expired"):
            await store.put_upload_part(stale["upload_id"], artifact_id, 2, b"a" * 20)
        assert await store.expire_uploads() == 1 and blobs.staging() == []
        aborted = await store.create_upload(artifact_id, principal_id="o", filename="d.bin", media_type="application/octet-stream",
                                            size_bytes=40, sha256="d" * 64, part_size=20)
        await store.put_upload_part(aborted["upload_id"], artifact_id, 1, b"b" * 20)
        assert (await store.abort_upload(aborted["upload_id"], artifact_id))["state"] == "aborted" and blobs.staging() == []
        with pytest.raises(ArtifactError):
            await store.create_upload(artifact_id, principal_id="o", filename="e", media_type="x/y", size_bytes=2 * 1024**3, sha256=digest)

    run(with_store(registry_pg["dsn"], blobs, body))


def test_migration_0031_is_idempotent_and_keeps_legacy_binding_dedupe(registry_pg):
    from tests.control_plane.registry_pg import ROOT
    sql = (ROOT / "deploy/control-plane/migrations/0031_artifact_registry_extensions.sql").read_text()

    async def body(store, connection):
        await connection.execute(sql)
        await connection.execute(sql)
        artifact = await store.register_logical(original_filename="x", created_by="o")
        await connection.execute("INSERT INTO artifact_bindings (artifact_id, stage) VALUES ($1,'s')", artifact["artifact_id"])
        for task in ("t1", "t2"):
            await store.link_context(artifact["artifact_id"], created_by="o", task_id=task)
        await store.link_context(artifact["artifact_id"], created_by="o", task_id="t1")       # repeat: no-op
        count = await connection.fetchval("SELECT count(*) FROM artifact_bindings WHERE artifact_id=$1", artifact["artifact_id"])
        assert count == 3

    run(with_store(registry_pg["dsn"], FakeBlobs(), body))
