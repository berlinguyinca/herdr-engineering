# ruff: noqa: F811  (pytest fixtures are imported by name from registry_pg)
"""CollabMirror: the read-only collaborator mirror the worker publishes for collab-public (Task 4)."""
import asyncio
import concurrent.futures
import hashlib
import json
import os
import stat
import threading
import time
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from herdr_engineering.control_plane.artifact_collab_store import CollabGrantStore
from herdr_engineering.control_plane.artifact_scope import ArtifactGraphGate
from herdr_engineering.control_plane.artifacts import ArtifactStore
from herdr_engineering.control_plane.collab_bridge_files import BridgeLimits
from herdr_engineering.control_plane.collab_mirror import CollabMirror
from herdr_engineering.control_plane.rustfs import IntegrityError
from tests.control_plane.registry_pg import FakeBlobs, registry_pg  # noqa: F401 - imported fixture

OWNER = "owner@example.org"
AGENT = "agent-m1"


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class SlowBlobs(FakeBlobs):
    """``iter_get`` that sleeps per chunk (blocking, as the real client does)."""

    def __init__(self, delay=0.05):
        super().__init__()
        self.delay = delay
        self.fetches = []

    def iter_get(self, key, expected_sha256, *, chunk_size=1 << 20):
        self.fetches.append(expected_sha256)
        for chunk in super().iter_get(key, expected_sha256, chunk_size=4):
            time.sleep(self.delay)
            yield chunk


class Crash(BaseException):
    """A simulated process death (not an ``Exception``): nothing may catch it."""


class CrashBlobs(FakeBlobs):
    """``iter_get`` that dies mid-stream for the digests in ``crash``."""

    def __init__(self):
        super().__init__()
        self.crash = set()

    def iter_get(self, key, expected_sha256, *, chunk_size=1 << 20):
        body = self.objects[key]
        if expected_sha256 in self.crash:
            yield body[:1]
            raise Crash("simulated crash mid-refresh")
        yield from super().iter_get(key, expected_sha256, chunk_size=chunk_size)


class CountingBlobs(FakeBlobs):
    """Counts fetches; ``hooks[sha]()`` runs (in the fetching thread) when that digest is fetched."""

    def __init__(self):
        super().__init__()
        self.fetches = []
        self.hooks = {}
        self.fail = set()

    def iter_get(self, key, expected_sha256, *, chunk_size=1 << 20):
        self.fetches.append(expected_sha256)
        hook = self.hooks.get(expected_sha256)
        if hook is not None:
            hook()
        if expected_sha256 in self.fail:
            raise IntegrityError("simulated RustFS failure")
        body = self.objects[key]                                 # like RustFSClient: verify only once fully read
        for offset in range(0, len(body), 4):
            yield body[offset:offset + 4]
        if hashlib.sha256(body).hexdigest() != expected_sha256:
            raise IntegrityError("sha256 mismatch")


def entry(blobs, name, data, media_type="text/plain"):
    key, digest = blobs.put(data)
    return {"name": name, "media_type": media_type, "sha256": digest, "size_bytes": len(data), "storage_key": key}


async def make_artifact(store, blobs, files, *, name="doc.md"):
    aid = (await store.register_logical(original_filename=name, created_by=OWNER))["artifact_id"]
    v = await store.commit_version(aid, base_version_id=None, source_manifest_blob=None,
                                   output_manifest=[entry(blobs, n, d) for n, d in files.items()],
                                   created_by=OWNER, provenance={"message": "first"})
    return aid, v


async def commit(store, blobs, aid, base, files, **kw):
    return await store.commit_version(aid, base_version_id=base, source_manifest_blob=None,
                                      output_manifest=[entry(blobs, n, d) for n, d in files.items()],
                                      created_by=OWNER, **kw)


async def grant(conn, aid, agent=AGENT, permission="write"):
    await CollabGrantStore(conn).grant(aid, agent, permission, expires_at=datetime.now(UTC) + timedelta(days=1),
                                       granted_by=OWNER)


async def gate_for(store):
    gate = ArtifactGraphGate(None)
    gate.load_retractions(await store.load_retractions())
    return gate


def run(pg, body, blobs=None):
    blobs = blobs if blobs is not None else FakeBlobs()

    async def go():
        conn = await asyncpg.connect(pg["dsn"])
        try:
            # the database is shared per module: start every test with no collaborator grants, so sync sees only its own
            await conn.execute("DELETE FROM artifact_acl WHERE principal_id LIKE 'collab:%'")
            return await body(ArtifactStore(conn, blobs), conn, blobs)
        finally:
            await conn.close()
    return asyncio.run(go())


def head_doc(root, aid):
    return json.loads((root / "artifacts" / aid / "head.json").read_text())


def blob_names(root, aid):
    d = root / "artifacts" / aid / "blobs"
    return sorted(os.listdir(d)) if d.exists() else []


def mirror_for(tmp_path, blobs, **limits):
    return CollabMirror(tmp_path / "mirror", rustfs=blobs, limits=BridgeLimits(**limits))


# ----------------------------------------------------------------------------- content

def test_plain_artifact_gets_head_and_verified_blobs(registry_pg, tmp_path):
    async def body(store, conn, blobs):
        aid, v1 = await make_artifact(store, blobs, {"doc.md": b"hello\n", "notes.txt": "café\n".encode()})
        await grant(conn, aid)
        mirror = mirror_for(tmp_path, blobs)
        doc = await mirror.refresh(store, aid, gate=await gate_for(store))
        root = tmp_path / "mirror"
        assert head_doc(root, aid) == doc
        assert doc["artifact_id"] == aid and doc["title"] == "doc.md" and doc["writable"] is True
        assert doc["writable_reason"] is None
        assert doc["head"] == {"version_id": v1["version_id"], "version_number": v1["version_number"]}
        [ver] = doc["versions"]
        assert ver["author_label"] == "owner" and ver["message"] == "first" and ver["merge"] is False
        assert ver["mirrored"] is True and ver["created_via"] == "owner"
        for f in ver["files"]:
            assert f["text"] is True and f["mirrored"] is True
            data = (root / "artifacts" / aid / "blobs" / f["sha256"]).read_bytes()
            assert hashlib.sha256(data).hexdigest() == f["sha256"] and len(data) == f["size_bytes"]
            assert set(f) == {"name", "size_bytes", "sha256", "media_type", "text", "mirrored"}
    run(registry_pg, body)


def test_binary_and_non_utf8_files_are_listed_without_blobs(registry_pg, tmp_path):
    async def body(store, conn, blobs):
        aid, _ = await make_artifact(store, blobs, {"doc.md": b"ok\n", "img.png": b"\x89PNG\r\n\x1a\n\x00\xff",
                                                    "latin.txt": b"caf\xe9\n"})
        mirror = mirror_for(tmp_path, blobs)
        doc = await mirror.refresh(store, aid, gate=await gate_for(store))
        files = {f["name"]: f for f in doc["versions"][0]["files"]}
        assert files["doc.md"]["text"] is True and files["doc.md"]["mirrored"] is True
        for name in ("img.png", "latin.txt"):
            assert files[name]["text"] is False and files[name]["mirrored"] is False
            assert files[name]["sha256"] not in blob_names(tmp_path / "mirror", aid)
        assert blob_names(tmp_path / "mirror", aid) == [files["doc.md"]["sha256"]]
    run(registry_pg, body)


def test_file_over_mirror_max_file_bytes_is_not_mirrored(registry_pg, tmp_path):
    async def body(store, conn, blobs):
        aid, _ = await make_artifact(store, blobs, {"small.md": b"abc\n", "big.md": b"x" * 100})
        mirror = mirror_for(tmp_path, blobs, mirror_max_file_bytes=50)
        doc = await mirror.refresh(store, aid, gate=await gate_for(store))
        files = {f["name"]: f for f in doc["versions"][0]["files"]}
        assert files["big.md"]["mirrored"] is False and files["small.md"]["mirrored"] is True
        assert blob_names(tmp_path / "mirror", aid) == [files["small.md"]["sha256"]]
    run(registry_pg, body)


def test_artifact_byte_cap_stops_adding_blobs(registry_pg, tmp_path):
    async def body(store, conn, blobs):
        aid, _ = await make_artifact(store, blobs, {"a.md": b"a" * 40, "b.md": b"b" * 40})
        mirror = mirror_for(tmp_path, blobs, mirror_max_artifact_bytes=60)
        doc = await mirror.refresh(store, aid, gate=await gate_for(store))
        mirrored = [f for f in doc["versions"][0]["files"] if f["mirrored"]]
        assert len(mirrored) == 1 and len(blob_names(tmp_path / "mirror", aid)) == 1
    run(registry_pg, body)


def test_history_window_and_manifest_cap(registry_pg, tmp_path):
    async def body(store, conn, blobs):
        aid, v = await make_artifact(store, blobs, {"doc.md": b"v1\n"})
        versions = [v]
        for i in range(2, 9):
            v = await commit(store, blobs, aid, v["version_id"], {"doc.md": f"v{i}\n".encode()})
            versions.append(v)
        root = tmp_path / "mirror"
        doc = await mirror_for(tmp_path, blobs, mirror_history_versions=5).refresh(
            store, aid, gate=await gate_for(store))
        assert [x["version_number"] for x in doc["versions"]] == [x["version_number"] for x in versions[::-1]]
        seventh = doc["versions"][6]
        assert seventh["mirrored"] is False and all(not f["mirrored"] for f in seventh["files"])
        assert all(x["mirrored"] for x in doc["versions"][:6])
        assert seventh["files"][0]["sha256"] not in blob_names(root, aid)
        assert len(blob_names(root, aid)) == 6
        capped = await mirror_for(tmp_path, blobs, mirror_max_manifests=3).refresh(
            store, aid, gate=await gate_for(store))
        assert [x["version_id"] for x in capped["versions"]] == [x["version_id"] for x in versions[::-1][:3]]
        # the shrunken window also shrinks blobs/: unreferenced blobs are collected after the swap
        assert len(blob_names(root, aid)) == 3
    run(registry_pg, body)


def test_report_artifact_is_read_only(registry_pg, tmp_path):
    async def body(store, conn, blobs):
        aid, _ = await make_artifact(store, blobs, {"doc.md": b"r\n"})
        await conn.execute("UPDATE artifacts SET artifact_type='report' WHERE artifact_id=$1", aid)
        doc = await mirror_for(tmp_path, blobs).refresh(store, aid, gate=await gate_for(store))
        assert doc["writable"] is False
        assert doc["writable_reason"] == "report artifacts are read-only for collaborators"
    run(registry_pg, body)


def test_collaborator_version_labels_and_merge_flag(registry_pg, tmp_path):
    async def body(store, conn, blobs):
        aid, v1 = await make_artifact(store, blobs, {"doc.md": b"one\n"})
        v2 = await commit(store, blobs, aid, v1["version_id"], {"doc.md": b"two\n"})
        v3 = await store.commit_version(
            aid, base_version_id=v2["version_id"], source_manifest_blob=None,
            output_manifest=[entry(blobs, "doc.md", b"three\n")], created_by="collab:" + AGENT,
            provenance={"message": "m" * 5000}, parent_version_ids=[v2["version_id"], v1["version_id"]],
            created_via="collaborator", collaborator_agent_id=AGENT)
        doc = await mirror_for(tmp_path, blobs).refresh(store, aid, gate=await gate_for(store))
        top = doc["versions"][0]
        assert top["version_id"] == v3["version_id"] and top["merge"] is True
        assert top["author_label"] == f"collaborator {AGENT}" and top["collaborator_agent_id"] == AGENT
        assert top["parent_version_ids"] == [v2["version_id"], v1["version_id"]]
        assert len(top["message"]) == 1000
    run(registry_pg, body)


# ----------------------------------------------------------------------------- retraction and revocation

def test_retracted_version_is_absent_and_retracted_artifact_removed(registry_pg, tmp_path):
    async def body(store, conn, blobs):
        aid, v1 = await make_artifact(store, blobs, {"doc.md": b"one\n"})
        v2 = await commit(store, blobs, aid, v1["version_id"], {"doc.md": b"two\n"})
        await commit(store, blobs, aid, v2["version_id"], {"doc.md": b"three\n"})
        await grant(conn, aid)
        mirror = mirror_for(tmp_path, blobs)
        await mirror.sync(store, gate=await gate_for(store))
        root = tmp_path / "mirror"
        assert len(head_doc(root, aid)["versions"]) == 3
        await store.record_retraction(aid, v2["version_id"])
        await mirror.sync(store, gate=await gate_for(store))       # the head did not move: still refreshed
        doc = head_doc(root, aid)
        assert v2["version_id"] not in [x["version_id"] for x in doc["versions"]]
        assert hashlib.sha256(b"two\n").hexdigest() not in blob_names(root, aid)
        await store.record_retraction(aid)
        await mirror.sync(store, gate=await gate_for(store))
        assert not (root / "artifacts" / aid).exists()
    run(registry_pg, body)


def test_gate_retraction_alone_hides_artifact(registry_pg, tmp_path):
    async def body(store, conn, blobs):
        aid, v1 = await make_artifact(store, blobs, {"doc.md": b"one\n"})
        await grant(conn, aid)
        mirror = mirror_for(tmp_path, blobs)
        gate = await gate_for(store)
        await mirror.sync(store, gate=gate)
        gate.retract(aid)
        await mirror.sync(store, gate=gate)
        assert not (tmp_path / "mirror" / "artifacts" / aid).exists()
    run(registry_pg, body)


def test_gate_version_retraction_alone_hides_version(registry_pg, tmp_path):
    async def body(store, conn, blobs):
        aid, v1 = await make_artifact(store, blobs, {"doc.md": b"one\n"})
        await commit(store, blobs, aid, v1["version_id"], {"doc.md": b"two\n"})
        await grant(conn, aid)
        mirror = mirror_for(tmp_path, blobs)
        gate = await gate_for(store)
        await mirror.sync(store, gate=gate)
        gate.retract(aid, v1["version_id"])                       # worker-local gate only, not yet durable
        await mirror.sync(store, gate=gate)
        doc = head_doc(tmp_path / "mirror", aid)
        assert [x["version_id"] for x in doc["versions"]] != [] and v1["version_id"] not in [
            x["version_id"] for x in doc["versions"]]
    run(registry_pg, body)


def test_retracted_head_version_fails_closed(registry_pg, tmp_path):
    async def body(store, conn, blobs):
        aid, v1 = await make_artifact(store, blobs, {"doc.md": b"one\n"})
        v2 = await commit(store, blobs, aid, v1["version_id"], {"doc.md": b"two\n"})
        await grant(conn, aid)
        mirror = mirror_for(tmp_path, blobs)
        await mirror.sync(store, gate=await gate_for(store))
        await store.record_retraction(aid, v2["version_id"])
        await mirror.sync(store, gate=await gate_for(store))
        assert not (tmp_path / "mirror" / "artifacts" / aid).exists()
    run(registry_pg, body)


def test_revoking_last_grant_removes_dir_on_next_sync(registry_pg, tmp_path):
    async def body(store, conn, blobs):
        aid, _ = await make_artifact(store, blobs, {"doc.md": b"one\n"})
        other, _ = await make_artifact(store, blobs, {"x.md": b"x\n"})
        await grant(conn, aid, "a1", "read")
        await grant(conn, aid, "a2", "write")
        mirror = mirror_for(tmp_path, blobs)
        await mirror.sync(store, gate=await gate_for(store))
        root = tmp_path / "mirror"
        assert (root / "artifacts" / aid / "head.json").exists()
        assert not (root / "artifacts" / other).exists()            # never granted: never mirrored
        await CollabGrantStore(conn).revoke(aid, "a1")
        await mirror.sync(store, gate=await gate_for(store))
        assert (root / "artifacts" / aid / "head.json").exists()
        await CollabGrantStore(conn).revoke(aid, "a2")
        await mirror.sync(store, gate=await gate_for(store))
        assert not (root / "artifacts" / aid).exists()
    run(registry_pg, body)


def test_sync_refreshes_on_new_head_and_force(registry_pg, tmp_path):
    async def body(store, conn, blobs):
        aid, v1 = await make_artifact(store, blobs, {"doc.md": b"one\n"})
        await grant(conn, aid)
        mirror = mirror_for(tmp_path, blobs)
        first = await mirror.sync(store, gate=await gate_for(store))
        assert aid in first["refreshed"]
        again = await mirror.sync(store, gate=await gate_for(store))
        assert aid not in again["refreshed"]
        assert aid in (await mirror.sync(store, gate=await gate_for(store), force={aid}))["refreshed"]
        v2 = await commit(store, blobs, aid, v1["version_id"], {"doc.md": b"two\n"})
        assert aid in (await mirror.sync(store, gate=await gate_for(store)))["refreshed"]
        assert head_doc(tmp_path / "mirror", aid)["head"]["version_id"] == v2["version_id"]
    run(registry_pg, body)


def test_stale_dir_without_grant_is_removed_after_restart(registry_pg, tmp_path):
    async def body(store, conn, blobs):
        aid, _ = await make_artifact(store, blobs, {"doc.md": b"one\n"})
        await grant(conn, aid)
        await mirror_for(tmp_path, blobs).sync(store, gate=await gate_for(store))
        await CollabGrantStore(conn).revoke(aid, AGENT)
        fresh = mirror_for(tmp_path, blobs)                         # a restarted worker: no in-memory state
        await fresh.sync(store, gate=await gate_for(store))
        assert not (tmp_path / "mirror" / "artifacts" / aid).exists()
    run(registry_pg, body)


# ----------------------------------------------------------------------------- grants.json

def test_grants_json_shape_limits_and_heartbeat(registry_pg, tmp_path):
    async def body(store, conn, blobs):
        aid, _ = await make_artifact(store, blobs, {"doc.md": b"one\n"})
        await grant(conn, aid, "a1", "write")
        limits = BridgeLimits(daily_bytes_per_agent=12345, mirror_history_versions=2)
        clock = Clock()
        mirror = CollabMirror(tmp_path / "mirror", rustfs=blobs, limits=limits, clock=clock)
        grants = CollabGrantStore(conn)
        path = tmp_path / "mirror" / "grants.json"
        assert (await mirror.publish_grants(grants, store)).written is True
        doc = json.loads(path.read_text())
        assert doc["limits"] == limits.to_dict()
        assert BridgeLimits.from_mapping(doc["limits"]) == limits
        assert doc["generated_at"] == clock.t
        mine = {agent for agent, arts in doc["agents"].items() if aid in arts}   # the database is shared per module
        assert mine == {"a1"}
        assert doc["agents"]["a1"][aid]["permission"] == "write"
        assert isinstance(doc["agents"]["a1"][aid]["expires_at"], float)
        clock.t += 29
        assert (await mirror.publish_grants(grants, store)).written is False
        assert json.loads(path.read_text())["generated_at"] == 1_000_000.0
        await grant(conn, aid, "a2", "read")                     # a grant change is written at once
        assert (await mirror.publish_grants(grants, store)).written is True
        doc = json.loads(path.read_text())
        assert doc["agents"]["a2"][aid]["permission"] == "read" and doc["generated_at"] == clock.t
        clock.t += 29.9
        assert (await mirror.publish_grants(grants, store)).written is False
        clock.t += 0.2                                            # 30 s since the last write
        assert (await mirror.publish_grants(grants, store)).written is True
        assert json.loads(path.read_text())["generated_at"] == clock.t
        await CollabGrantStore(conn).revoke(aid, "a1")
        assert (await mirror.publish_grants(grants, store)).written is True
        assert aid not in json.loads(path.read_text())["agents"].get("a1", {})
    run(registry_pg, body)


def test_grants_json_drops_retracted_artifacts(registry_pg, tmp_path):
    async def body(store, conn, blobs):
        aid, _ = await make_artifact(store, blobs, {"doc.md": b"one\n"})
        await grant(conn, aid, "a1", "read")
        mirror = mirror_for(tmp_path, blobs)
        await mirror.publish_grants(CollabGrantStore(conn), store)
        await store.record_retraction(aid)
        assert await mirror.publish_grants(CollabGrantStore(conn), store)
        agents = json.loads((tmp_path / "mirror" / "grants.json").read_text())["agents"]
        assert all(aid not in arts for arts in agents.values())
    run(registry_pg, body)


# ----------------------------------------------------------------------------- concurrency

def test_concurrent_refreshes_leave_every_referenced_blob(registry_pg, tmp_path):
    blobs = SlowBlobs(delay=0.01)

    async def body(store, conn, blobs):
        aid, v1 = await make_artifact(store, blobs, {"doc.md": b"first version body\n"})
        mirror = mirror_for(tmp_path, blobs)
        conn2 = await asyncpg.connect(registry_pg["dsn"])
        try:
            store2 = ArtifactStore(conn2, blobs)
            gate = await gate_for(store)
            first = asyncio.create_task(mirror.refresh(store, aid, gate=gate))
            await asyncio.sleep(0.02)                             # first refresh is streaming its blob
            await commit(store2, blobs, aid, v1["version_id"], {"doc.md": b"second version body\n"})
            second = asyncio.create_task(mirror.refresh(store2, aid, gate=gate))
            await asyncio.gather(first, second)
        finally:
            await conn2.close()
        root = tmp_path / "mirror"
        doc = head_doc(root, aid)
        assert doc["head"]["version_number"] == 2
        for ver in doc["versions"]:
            for f in ver["files"]:
                if f["mirrored"]:
                    assert (root / "artifacts" / aid / "blobs" / f["sha256"]).is_file()
    run(registry_pg, body, blobs)


def test_slow_refresh_does_not_block_event_loop(registry_pg, tmp_path):
    blobs = SlowBlobs(delay=0.02)

    async def body(store, conn, blobs):
        aid, _ = await make_artifact(store, blobs, {"doc.md": b"0123456789abcdef0123456789\n"})
        mirror = mirror_for(tmp_path, blobs)
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1
        t = asyncio.create_task(ticker())
        await mirror.refresh(store, aid, gate=await gate_for(store))
        t.cancel()
        assert ticks >= 5
        assert threading.main_thread() is threading.current_thread()
    run(registry_pg, body, blobs)


def test_annotations_survive_refresh_and_remove_deletes_them(registry_pg, tmp_path):
    async def body(store, conn, blobs):
        aid, v1 = await make_artifact(store, blobs, {"doc.md": b"one\n"})
        mirror = mirror_for(tmp_path, blobs)
        await mirror.refresh(store, aid, gate=await gate_for(store))
        adir = tmp_path / "mirror" / "artifacts" / aid
        (adir / "annotations.json").write_text("{}")
        await commit(store, blobs, aid, v1["version_id"], {"doc.md": b"two\n"})
        await mirror.refresh(store, aid, gate=await gate_for(store))
        assert (adir / "annotations.json").read_text() == "{}"
        await mirror.remove(aid)
        assert not adir.exists()
    run(registry_pg, body)


# ----------------------------------------------------------------------------- safety

def test_crash_mid_refresh_keeps_previous_head(registry_pg, tmp_path):
    blobs = CrashBlobs()

    async def body(store, conn, blobs):
        aid, v1 = await make_artifact(store, blobs, {"doc.md": b"one\n"})
        mirror = mirror_for(tmp_path, blobs)
        await mirror.refresh(store, aid, gate=await gate_for(store))
        root = tmp_path / "mirror"
        before = (root / "artifacts" / aid / "head.json").read_bytes()
        blobs.crash.add(hashlib.sha256(b"two\n").hexdigest())
        await commit(store, blobs, aid, v1["version_id"], {"doc.md": b"two\n"})
        with pytest.raises(Crash):
            await mirror.refresh(store, aid, gate=await gate_for(store))
        assert (root / "artifacts" / aid / "head.json").read_bytes() == before
        assert blob_names(root, aid) == [hashlib.sha256(b"one\n").hexdigest()]   # no partial, no tmp left
    run(registry_pg, body, blobs)


def test_corrupt_blob_still_writes_head_and_is_not_refetched_every_sync(registry_pg, tmp_path):
    blobs = CountingBlobs()

    async def body(store, conn, blobs):
        aid, _ = await make_artifact(store, blobs, {"doc.md": b"one\n", "ok.md": b"fine\n"})
        await grant(conn, aid)
        bad = hashlib.sha256(b"one\n").hexdigest()
        blobs.objects[blobs.key(bad)] = b"tampered\n"
        clock = Clock()
        mirror = CollabMirror(tmp_path / "mirror", rustfs=blobs, limits=BridgeLimits(), clock=clock)
        out = await mirror.sync(store, gate=await gate_for(store))
        assert aid in out["refreshed"]
        files = {f["name"]: f for f in head_doc(tmp_path / "mirror", aid)["versions"][0]["files"]}
        assert files["doc.md"]["mirrored"] is False and files["ok.md"]["mirrored"] is True
        assert blob_names(tmp_path / "mirror", aid) == [files["ok.md"]["sha256"]]
        assert blobs.fetches.count(bad) == 1
        for _ in range(3):
            clock.t += 30
            await mirror.sync(store, gate=await gate_for(store))
        assert blobs.fetches.count(bad) == 1                      # the bad sha is not re-downloaded every sync
        blobs.objects[blobs.key(bad)] = b"one\n"                  # repaired; after the TTL it is retried
        clock.t += 3600
        await mirror.sync(store, gate=await gate_for(store))
        assert blobs.fetches.count(bad) == 2
        files = {f["name"]: f for f in head_doc(tmp_path / "mirror", aid)["versions"][0]["files"]}
        assert files["doc.md"]["mirrored"] is True
    run(registry_pg, body, blobs)


def test_no_owner_email_anywhere_in_mirror(registry_pg, tmp_path):
    async def body(store, conn, blobs):
        aid, v1 = await make_artifact(store, blobs, {"doc.md": b"one\n"})
        await commit(store, blobs, aid, v1["version_id"], {"doc.md": b"two\n"}, provenance={"message": "hi"})
        await grant(conn, aid)
        mirror = mirror_for(tmp_path, blobs)
        await mirror.sync(store, gate=await gate_for(store))
        await mirror.publish_grants(CollabGrantStore(conn), store)
        files = [p for p in (tmp_path / "mirror").rglob("*") if p.is_file()]
        assert files
        for p in files:
            assert OWNER.encode() not in p.read_bytes(), p
    run(registry_pg, body)


def test_symlinked_blobs_dir_is_never_followed(registry_pg, tmp_path):
    async def body(store, conn, blobs):
        aid, _ = await make_artifact(store, blobs, {"doc.md": b"one\n"})
        outside = tmp_path / "outside"
        outside.mkdir()
        adir = tmp_path / "mirror" / "artifacts" / aid
        adir.mkdir(parents=True)
        (adir / "blobs").symlink_to(outside)
        with pytest.raises(Exception):
            await mirror_for(tmp_path, blobs).refresh(store, aid, gate=await gate_for(store))
        assert list(outside.iterdir()) == []
    run(registry_pg, body)


# ----------------------------------------------------------------------------- review fixes

def test_retraction_reaches_mirror_even_when_new_head_blob_fails(registry_pg, tmp_path):
    blobs = CountingBlobs()

    async def body(store, conn, blobs):
        aid, v1 = await make_artifact(store, blobs, {"doc.md": b"one\n"})
        v2 = await commit(store, blobs, aid, v1["version_id"], {"doc.md": b"two\n"})
        await grant(conn, aid)
        mirror = mirror_for(tmp_path, blobs)
        await mirror.sync(store, gate=await gate_for(store))
        root = tmp_path / "mirror"
        await store.record_retraction(aid, v2["version_id"])
        blobs.fail.add(hashlib.sha256(b"three\n").hexdigest())
        v3 = await commit(store, blobs, aid, v2["version_id"], {"doc.md": b"three\n"})
        await mirror.sync(store, gate=await gate_for(store))
        doc = head_doc(root, aid)
        assert v2["version_id"] not in [x["version_id"] for x in doc["versions"]]
        assert hashlib.sha256(b"two\n").hexdigest() not in blob_names(root, aid)
        assert doc["head"]["version_id"] == v3["version_id"]
        assert doc["versions"][0]["files"][0]["mirrored"] is False
    run(registry_pg, body, blobs)


def test_shrunk_version_set_is_pruned_before_any_fetch(registry_pg, tmp_path):
    blobs = CountingBlobs()

    async def body(store, conn, blobs):
        aid, v1 = await make_artifact(store, blobs, {"doc.md": b"one\n"})
        v2 = await commit(store, blobs, aid, v1["version_id"], {"doc.md": b"two\n"})
        mirror = mirror_for(tmp_path, blobs)
        await mirror.refresh(store, aid, gate=await gate_for(store))
        root = tmp_path / "mirror"
        seen = []
        blobs.hooks[hashlib.sha256(b"three\n").hexdigest()] = lambda: seen.append(
            ([x["version_id"] for x in head_doc(root, aid)["versions"]], blob_names(root, aid)))
        await store.record_retraction(aid, v2["version_id"])
        await commit(store, blobs, aid, v2["version_id"], {"doc.md": b"three\n"})
        await mirror.refresh(store, aid, gate=await gate_for(store))
        [(listed, present)] = seen
        assert v2["version_id"] not in listed and hashlib.sha256(b"two\n").hexdigest() not in present
    run(registry_pg, body, blobs)


def test_sync_prunes_retractions_before_slow_downloads(registry_pg, tmp_path):
    blobs = CountingBlobs()

    async def body(store, conn, blobs):
        a, a1 = await make_artifact(store, blobs, {"doc.md": b"a-one\n"})
        b, b1 = await make_artifact(store, blobs, {"doc.md": b"b-one\n"})
        b2 = await commit(store, blobs, b, b1["version_id"], {"doc.md": b"b-two\n"})
        await grant(conn, a)
        await grant(conn, b)
        mirror = mirror_for(tmp_path, blobs)
        await mirror.sync(store, gate=await gate_for(store))
        root = tmp_path / "mirror"
        seen = []
        blobs.hooks[hashlib.sha256(b"a-two\n").hexdigest()] = lambda: seen.append(
            [x["version_id"] for x in head_doc(root, b)["versions"]])
        await commit(store, blobs, a, a1["version_id"], {"doc.md": b"a-two\n"})
        await store.record_retraction(b, b1["version_id"])
        await mirror.sync(store, gate=await gate_for(store))
        assert seen == [[b2["version_id"]]]                     # b's retraction landed before a's download
    run(registry_pg, body, blobs)


def test_failed_stream_does_not_charge_the_artifact_budget(registry_pg, tmp_path):
    async def body(store, conn, blobs):
        aid, _ = await make_artifact(store, blobs, {"a.bin": b"\xff" * 50, "b.md": b"b" * 40})
        for _ in range(2):                                       # second pass: a restarted worker, empty caches
            mirror = mirror_for(tmp_path, blobs, mirror_max_artifact_bytes=60)
            doc = await mirror.refresh(store, aid, gate=await gate_for(store))
            files = {f["name"]: f for f in doc["versions"][0]["files"]}
            assert files["b.md"]["mirrored"] is True and files["a.bin"]["mirrored"] is False
    run(registry_pg, body)


def test_mirror_dirs_and_files_ignore_the_umask(registry_pg, tmp_path):
    async def body(store, conn, blobs):
        aid, _ = await make_artifact(store, blobs, {"doc.md": b"one\n"})
        await grant(conn, aid)
        mirror = mirror_for(tmp_path, blobs)
        old = os.umask(0o077)
        try:
            await mirror.sync(store, gate=await gate_for(store))
            await mirror.publish_grants(CollabGrantStore(conn), store)
        finally:
            os.umask(old)
        root = tmp_path / "mirror"
        for d in (root, root / "artifacts", root / "artifacts" / aid, root / "artifacts" / aid / "blobs"):
            assert stat.S_IMODE(os.stat(d).st_mode) == 0o755, d
        for f in [root / "grants.json", root / "artifacts" / aid / "head.json",
                  *(root / "artifacts" / aid / "blobs").iterdir()]:
            assert stat.S_IMODE(os.stat(f).st_mode) == 0o644, f
    run(registry_pg, body)


def test_durable_state_retraction_hides_version_with_a_stale_gate(registry_pg, tmp_path):
    async def body(store, conn, blobs):
        aid, v1 = await make_artifact(store, blobs, {"doc.md": b"one\n"})
        v2 = await commit(store, blobs, aid, v1["version_id"], {"doc.md": b"two\n"})
        await grant(conn, aid)
        gate = await gate_for(store)                             # loaded BEFORE the retraction
        mirror = mirror_for(tmp_path, blobs)
        await mirror.sync(store, gate=gate)
        await store.record_retraction(aid, v1["version_id"])
        await mirror.sync(store, gate=gate)
        assert [x["version_id"] for x in head_doc(tmp_path / "mirror", aid)["versions"]] == [v2["version_id"]]
    run(registry_pg, body)


def test_publish_grants_reports_changed_artifacts_and_uses_a_given_gate(registry_pg, tmp_path):
    async def body(store, conn, blobs):
        aid, _ = await make_artifact(store, blobs, {"doc.md": b"one\n"})
        await grant(conn, aid, "g1", "read")
        clock = Clock()
        mirror = CollabMirror(tmp_path / "mirror", rustfs=blobs, limits=BridgeLimits(), clock=clock)
        grants = CollabGrantStore(conn)
        first = await mirror.publish_grants(grants, store)
        assert first and aid in first.changed
        clock.t += 31
        beat = await mirror.publish_grants(grants, store)
        assert beat and beat.changed == frozenset()
        await grant(conn, aid, "g1", "write")
        changed = await mirror.publish_grants(grants, store)
        assert changed.changed == {aid}
        gate = ArtifactGraphGate(None)
        gate.retract(aid)                                         # a pre-loaded gate replaces load_retractions()
        dropped = await mirror.publish_grants(grants, None, gate=gate)
        assert aid in dropped.changed
        agents = json.loads((tmp_path / "mirror" / "grants.json").read_text())["agents"]
        assert all(aid not in arts for arts in agents.values())
    run(registry_pg, body)


def test_publish_grants_does_not_queue_behind_the_default_executor(registry_pg, tmp_path):
    async def body(store, conn, blobs):
        aid, _ = await make_artifact(store, blobs, {"doc.md": b"one\n"})
        await grant(conn, aid, "h1", "read")
        loop = asyncio.get_running_loop()
        loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=1))
        release = threading.Event()
        blocker = loop.run_in_executor(None, release.wait, 10)   # the default pool is saturated
        try:
            result = await asyncio.wait_for(
                mirror_for(tmp_path, blobs).publish_grants(CollabGrantStore(conn), store), timeout=3)
            assert result.written
        finally:
            release.set()
            await blocker
    run(registry_pg, body)


# ----------------------------------------------------------------------------- review fixes, round 2

def test_head_files_have_budget_priority_over_older_present_blobs(registry_pg, tmp_path):
    blobs = CountingBlobs()

    async def body(store, conn, blobs):
        aid, v1 = await make_artifact(store, blobs, {"old.md": b"o" * 60})
        mirror = mirror_for(tmp_path, blobs, mirror_max_artifact_bytes=100, mirror_history_versions=1)
        await mirror.refresh(store, aid, gate=await gate_for(store))
        await commit(store, blobs, aid, v1["version_id"], {"new.md": b"n" * 60})
        for _ in range(2):
            doc = await mirror.refresh(store, aid, gate=await gate_for(store))
            head_files = {f["name"]: f for f in doc["versions"][0]["files"]}
            old_files = {f["name"]: f for f in doc["versions"][1]["files"]}
            assert head_files["new.md"]["mirrored"] is True
            assert old_files["old.md"]["mirrored"] is False
            assert blob_names(tmp_path / "mirror", aid) == [hashlib.sha256(b"n" * 60).hexdigest()]
    run(registry_pg, body, blobs)


def test_budget_skip_with_older_blob_kept_is_reconsidered(registry_pg, tmp_path):
    blobs = CountingBlobs()

    async def body(store, conn, blobs):
        aid, v1 = await make_artifact(store, blobs, {"old.md": b"o" * 30})
        await commit(store, blobs, aid, v1["version_id"], {"big.md": b"b" * 90})
        await grant(conn, aid)
        mirror = mirror_for(tmp_path, blobs, mirror_max_artifact_bytes=80, mirror_history_versions=1)
        await mirror.sync(store, gate=await gate_for(store))
        doc = head_doc(tmp_path / "mirror", aid)
        assert doc["versions"][0]["files"][0]["mirrored"] is False and doc["versions"][1]["files"][0]["mirrored"]
        assert aid in (await mirror.sync(store, gate=await gate_for(store)))["refreshed"]
    run(registry_pg, body, blobs)


@pytest.mark.parametrize("tamper", [b"\xff\xfe\xfd\xfc\xfb", b"one\n plus a lot more bytes"])
def test_corrupt_blob_cut_by_validator_or_cap_is_retried_after_repair(registry_pg, tmp_path, tamper):
    blobs = CountingBlobs()

    async def body(store, conn, blobs):
        aid, _ = await make_artifact(store, blobs, {"doc.md": b"one\n"})
        await grant(conn, aid)
        good = hashlib.sha256(b"one\n").hexdigest()
        blobs.objects[blobs.key(good)] = tamper
        clock = Clock()
        mirror = CollabMirror(tmp_path / "mirror", rustfs=blobs, limits=BridgeLimits(), clock=clock)
        await mirror.sync(store, gate=await gate_for(store))
        assert head_doc(tmp_path / "mirror", aid)["versions"][0]["files"][0]["mirrored"] is False
        blobs.objects[blobs.key(good)] = b"one\n"
        clock.t += 3600
        await mirror.sync(store, gate=await gate_for(store))
        f = head_doc(tmp_path / "mirror", aid)["versions"][0]["files"][0]
        assert f["mirrored"] is True and f["text"] is True
        assert blobs.fetches.count(good) == 2
    run(registry_pg, body, blobs)


def test_genuinely_binary_blob_is_cached_and_not_refetched(registry_pg, tmp_path):
    blobs = CountingBlobs()

    async def body(store, conn, blobs):
        aid, _ = await make_artifact(store, blobs, {"img.md": b"\xff" * 20})
        await grant(conn, aid)
        clock = Clock()
        mirror = CollabMirror(tmp_path / "mirror", rustfs=blobs, limits=BridgeLimits(), clock=clock)
        await mirror.sync(store, gate=await gate_for(store))
        clock.t += 7200
        out = await mirror.sync(store, gate=await gate_for(store))
        assert aid not in out["refreshed"]
        assert blobs.fetches.count(hashlib.sha256(b"\xff" * 20).hexdigest()) == 1
        assert head_doc(tmp_path / "mirror", aid)["versions"][0]["files"][0]["text"] is False
    run(registry_pg, body, blobs)


def test_cancelled_refresh_never_republishes_a_retracted_version(registry_pg, tmp_path):
    blobs = CountingBlobs()

    async def body(store, conn, blobs):
        aid, v1 = await make_artifact(store, blobs, {"doc.md": b"one\n"})
        await commit(store, blobs, aid, v1["version_id"], {"doc.md": b"two\n"})
        entered, release = threading.Event(), threading.Event()
        calls = []

        def hold():
            calls.append(1)
            if len(calls) == 1:
                entered.set()
                release.wait(10)
        blobs.hooks[hashlib.sha256(b"two\n").hexdigest()] = hold
        mirror = mirror_for(tmp_path, blobs)
        first = asyncio.create_task(mirror.refresh(store, aid, gate=await gate_for(store)))
        await asyncio.to_thread(entered.wait, 10)
        first.cancel()
        await asyncio.sleep(0.05)
        await store.record_retraction(aid, v1["version_id"])
        conn2 = await asyncpg.connect(registry_pg["dsn"])
        try:
            store2 = ArtifactStore(conn2, blobs)
            second = asyncio.create_task(mirror.refresh(store2, aid, gate=await gate_for(store2)))
            await asyncio.sleep(0.3)
            release.set()
            await second
            with pytest.raises(asyncio.CancelledError):
                await first
            await asyncio.sleep(0.3)                             # any straggling worker thread has finished
        finally:
            await conn2.close()
        listed = [x["version_id"] for x in head_doc(tmp_path / "mirror", aid)["versions"]]
        assert v1["version_id"] not in listed
    run(registry_pg, body, blobs)


def test_heartbeat_skip_is_falsy_and_retracted_set_alone_is_honoured(registry_pg, tmp_path):
    async def body(store, conn, blobs):
        aid, _ = await make_artifact(store, blobs, {"doc.md": b"one\n"})
        await grant(conn, aid, "r1", "read")
        clock = Clock()
        mirror = CollabMirror(tmp_path / "mirror", rustfs=blobs, limits=BridgeLimits(), clock=clock)
        grants = CollabGrantStore(conn)
        assert await mirror.publish_grants(grants, None)
        assert not await mirror.publish_grants(grants, None)
        assert await mirror.publish_grants(grants, None, retracted={aid})
        agents = json.loads((tmp_path / "mirror" / "grants.json").read_text())["agents"]
        assert all(aid not in arts for arts in agents.values())
    run(registry_pg, body)


def test_concurrent_publish_grants_never_leave_a_stale_snapshot(registry_pg, tmp_path):
    async def body(store, conn, blobs):
        aid, _ = await make_artifact(store, blobs, {"doc.md": b"one\n"})
        clock = Clock()
        mirror = CollabMirror(tmp_path / "mirror", rustfs=blobs, limits=BridgeLimits(), clock=clock)
        gate = threading.Event()
        rows_old = [{"artifact_id": aid, "agent_id": "s1", "permission": "read", "expires_at": 2e9}]
        rows_new = [{"artifact_id": aid, "agent_id": "s1", "permission": "write", "expires_at": 2e9}]

        class SlowGrants:
            async def live_grants(self):
                await asyncio.to_thread(gate.wait, 5)
                return rows_old
        stale = asyncio.create_task(mirror.publish_grants(SlowGrants(), None))
        await asyncio.sleep(0.05)
        fresh = asyncio.create_task(mirror.publish_grants(rows_new, None))
        await asyncio.sleep(0.05)
        gate.set()
        await asyncio.gather(stale, fresh)
        doc = json.loads((tmp_path / "mirror" / "grants.json").read_text())
        assert doc["agents"]["s1"][aid]["permission"] == "write"
    run(registry_pg, body)


def test_sync_reports_failed_when_remove_fails_and_never_both(registry_pg, tmp_path, monkeypatch):
    from herdr_engineering.control_plane import collab_mirror as cm

    async def body(store, conn, blobs):
        aid, _ = await make_artifact(store, blobs, {"doc.md": b"one\n"})
        await grant(conn, aid)
        mirror = mirror_for(tmp_path, blobs)
        await mirror.sync(store, gate=await gate_for(store))
        await CollabGrantStore(conn).revoke(aid, AGENT)
        monkeypatch.setattr(cm, "remove_tree_beneath", lambda *a, **kw: False)
        out = await mirror.sync(store, gate=await gate_for(store))
        assert aid in out["failed"] and aid not in out["removed"]
        monkeypatch.undo()
        other, o1 = await make_artifact(store, blobs, {"doc.md": b"other\n"})
        await grant(conn, other)
        real = cm.CollabMirror._refresh
        attempts = []

        async def flaky(self, store, artifact_id, *, gate, fetch):
            if not fetch and not attempts:
                attempts.append(1)
                raise RuntimeError("prune failed once")
            return await real(self, store, artifact_id, gate=gate, fetch=fetch)
        monkeypatch.setattr(cm.CollabMirror, "_refresh", flaky)
        fresh = mirror_for(tmp_path, blobs)
        out = await fresh.sync(store, gate=await gate_for(store))
        assert other in out["refreshed"] and other not in out["failed"]
    run(registry_pg, body)


def test_present_blob_is_kept_even_when_its_sha_failed_elsewhere(registry_pg, tmp_path):
    blobs = CountingBlobs()

    async def body(store, conn, blobs):
        shared = b"shared text\n"
        x, x1 = await make_artifact(store, blobs, {"doc.md": shared})
        clock = Clock()
        mirror = CollabMirror(tmp_path / "mirror", rustfs=blobs, limits=BridgeLimits(), clock=clock)
        await mirror.refresh(store, x, gate=await gate_for(store))            # x holds the blob on disk
        sha = hashlib.sha256(shared).hexdigest()
        blobs.objects[blobs.key(sha)] = b"broken"                             # the object goes bad in RustFS
        y, y1 = await make_artifact(store, blobs, {"copy.md": b"other\n"})
        # y references the same sha: its fetch fails and the sha lands in the bad cache
        await store.commit_version(y, base_version_id=y1["version_id"], source_manifest_blob=None,
                                   output_manifest=[{"name": "s.md", "media_type": "text/plain", "sha256": sha,
                                                     "size_bytes": len(shared), "storage_key": blobs.key(sha)}],
                                   created_by=OWNER)
        await mirror.refresh(store, y, gate=await gate_for(store))
        assert mirror._bad_blob(sha)
        await commit(store, blobs, x, x1["version_id"], {"doc.md": shared, "more.md": b"more\n"})
        doc = await mirror.refresh(store, x, gate=await gate_for(store))
        assert {f["name"]: f["mirrored"] for f in doc["versions"][0]["files"]}["doc.md"] is True
    run(registry_pg, body, blobs)
