# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""Plan 3, Task 8c: artifact collaboration end to end, in one process.

An agent drives the seven artifact MCP tools (``collab_mcp.handle`` over a real ``CollabService`` and a real
``CollabArtifactBridge``); the worker side is the real ``CollabMirror`` (``publish_grants`` + ``sync``) and the real
``CollabIntentApplier`` (``tick`` + ``sweep_staging``) over a real PostgreSQL and a fake RustFS. The mirror, outbox
and staging are temp dirs; one controllable clock drives the service, the bridge, the mirror and the applier.

Task 9 (building the whole worker from YAML config) does not exist yet, so the wiring Task 9 will do is done by hand
here: ``register_commit()``, a shared ``ArtifactGraphGate``, the mirror passed to the applier (its post-commit
``refresh``) and a recording ``lineage`` hook (no real lineage route is reachable from a unit-level process).
No browser and no extra service: ``python -m pytest tests/e2e/test_artifact_collab_bridge.py -m e2e``."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from herdr_engineering.control_plane import collab_mcp
from herdr_engineering.control_plane.artifact_collab_store import CollabGrantStore
from herdr_engineering.control_plane.artifact_scope import ArtifactGraphGate
from herdr_engineering.control_plane.artifacts import ArtifactStore
from herdr_engineering.control_plane.collab import CollabError
from herdr_engineering.control_plane.collab_artifacts import CollabArtifactBridge
from herdr_engineering.control_plane.collab_bridge_files import (
    CHUNK_BYTES,
    BridgeLimits,
    upload_id_for,
)
from herdr_engineering.control_plane.collab_intents import CollabIntentApplier
from herdr_engineering.control_plane.collab_mirror import CollabMirror
from tests.control_plane.registry_pg import FakeBlobs, registry_pg  # noqa: F401 - imported fixture
from tests.control_plane.test_collab_tasks import W, svc  # noqa: F401 - imported fixture

BASE_DOC = "".join(f"line {i}\n" for i in range(1, 11))
BLOB = bytes((i * 31 + 7) % 256 for i in range(int(1.3 * 1024 * 1024)))          # 1.3 MiB: three 512 KiB chunks
NOTICE = collab_mcp.UNTRUSTED


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def edit(text: str, lineno: int, new: str) -> str:
    lines = text.splitlines(keepends=True)
    lines[lineno - 1] = new + "\n"
    return "".join(lines)


class Rig:
    """The single-process worker + collab-public wiring, by hand."""

    def __init__(self, svc, tmp_path, pool):
        self.pool, self.clock, self.svc = pool, svc.clock, svc
        self.clock.t = time.time()                    # DB grants expire on real time; the clock only moves forward
        self.w = W(svc)
        self.agent, self.info = self.w.agent("alice", name="agent-a")
        self.mirror_root, self.outbox = tmp_path / "mirror", tmp_path / "outbox"
        self.outbox.mkdir()
        self.limits = BridgeLimits()
        self.blobs = FakeBlobs()
        self.gate = ArtifactGraphGate(None)
        self.mirror = CollabMirror(self.mirror_root, rustfs=self.blobs, limits=self.limits, clock=self.clock)
        self.lineage: list[dict] = []
        self.applier = CollabIntentApplier(
            self.outbox, self.mirror, rustfs=self.blobs, limits=self.limits, spool_dir=tmp_path / "spool",
            lineage=lambda version: self.lineage.append(version) or {}, clock=self.clock, gate=self.gate)
        self.applier.register_commit()
        self.bridge = CollabArtifactBridge(svc, self.mirror_root, self.outbox, clock=self.clock, feed_interval=3600.0)
        svc.artifact_bridge = self.bridge
        self.aid = ""

    # -- worker side ----------------------------------------------------------------------------------------
    async def heartbeat(self, advance: float = 0.0) -> None:
        """What the worker's timers do: agents.json, grants.json and a mirror sync."""
        self.clock.t += advance
        self.bridge.write_agents_feed()
        async with self.pool.acquire() as conn:
            store = ArtifactStore(conn, self.blobs)
            await self.mirror.publish_grants(CollabGrantStore(conn), store, gate=self.gate)
            await self.mirror.sync(store, gate=self.gate)

    async def cycle(self) -> dict:
        """One applier pass: tick, wait for the lanes and the mirror refresher, tick again."""
        first = await self.applier.tick(self.pool.acquire, wait=30)
        await self.applier.drain(timeout=30)
        later = await self.applier.tick(self.pool.acquire, wait=30)
        await self.applier.drain(timeout=30)
        return {k: first[k] + later[k] for k in first}

    # -- owner side (ArtifactStore directly) ---------------------------------------------------------------
    def entry(self, name: str, data: bytes, media_type: str = "text/markdown") -> dict:
        key, digest = self.blobs.put(data)
        return {"name": name, "media_type": media_type, "sha256": digest, "size_bytes": len(data), "storage_key": key}

    async def owner_commit(self, base: str | None, text: str) -> str:
        async with self.pool.acquire() as conn:
            store = ArtifactStore(conn, self.blobs)
            if base is None:
                self.aid = (await store.register_logical(original_filename="doc.md", created_by="owner"))["artifact_id"]
            version = await store.commit_version(
                self.aid, base_version_id=base, source_manifest_blob=None,
                output_manifest=[self.entry("doc.md", text.encode())], created_by="owner")
        return version["version_id"]

    async def grant(self, permission: str = "write") -> None:
        async with self.pool.acquire() as conn:
            await CollabGrantStore(conn).grant(self.aid, self.agent.id, permission,
                                               expires_at=datetime.now(UTC) + timedelta(days=1), granted_by="owner")

    # -- agent side (collab_mcp.handle) ---------------------------------------------------------------------
    def mcp(self, name: str, /, **args) -> tuple[bool, dict]:
        status, body = collab_mcp.handle(
            self.svc, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}},
            f"Agent {self.info['credential']}", self.info["device_id"])
        assert status == 200 and "result" in body, body
        out = body["result"]
        text = out["content"][0]["text"]
        if text.startswith(NOTICE):
            text = text[len(NOTICE):].lstrip("\n")
        return out["isError"], json.loads(text)

    def ok(self, name: str, /, **args) -> dict:
        is_error, doc = self.mcp(name, **args)
        assert not is_error, doc
        return doc

    def commit(self, base: str, changes: list[dict], message: str = "collab edit") -> str:
        doc = self.ok("artifact_commit", artifact_id=self.aid, base_version_id=base, message=message, changes=changes)
        assert doc["status"] == "pending"
        return doc["ticket"]

    def result(self, ticket: str) -> dict:
        doc = self.ok("artifact_commit_result", ticket=ticket, seconds=0)
        assert doc.get("status") != "pending", "the worker has not decided the ticket"
        return doc


async def sql(pool, query, *args):
    async with pool.acquire() as conn:
        return await conn.fetch(query, *args)


async def staged_dirs(rig) -> list[str]:
    root = rig.outbox / "staging" / f"{rig.agent.id}"
    return sorted(p.name for p in root.glob("*")) if root.exists() else []


async def scenario(rig: Rig, pool) -> None:
    a = rig.agent
    # ------------------------------------------------------------------ (1) A holds a write grant; the owner commits v2
    v1 = await rig.owner_commit(None, BASE_DOC)
    await rig.grant("write")
    await rig.heartbeat()
    listed = rig.ok("artifacts_list")["artifacts"]
    assert [(r["artifact_id"], r["permission"], r["head_version_id"]) for r in listed] == [(rig.aid, "write", v1)]
    v2 = await rig.owner_commit(v1, edit(BASE_DOC, 2, "owner two"))
    await rig.heartbeat(advance=1)
    head = rig.ok("artifact_head", artifact_id=rig.aid)
    assert head["head_version_id"] == v2 and head["head_version_number"] == 2 and head["writable"] is True

    # ------------------------------------------------------------------ (2) A read v1 (stale), disjoint edit -> merged
    old = rig.ok("artifact_read", artifact_id=rig.aid, name="doc.md", version_id=v1)
    assert old["text"] == BASE_DOC and old["version_id"] == v1
    ticket = rig.commit(v1, [{"name": "doc.md", "op": "put", "text": edit(BASE_DOC, 9, "mine nine")}])
    assert rig.ok("artifact_commit_result", ticket=ticket, seconds=0) == {"status": "pending"}
    await rig.heartbeat(advance=1)                       # the feeds stay fresh while the worker is busy
    counts = await rig.cycle()
    assert counts["applied"] == 1
    merged = rig.result(ticket)
    assert merged["result"] == "merged" and merged["merged_names"] == ["doc.md"]
    m1 = merged["version_id"]
    await rig.heartbeat(advance=1)
    history = rig.ok("artifact_history", artifact_id=rig.aid)["versions"]
    assert [h["version_id"] for h in history][:3] == [m1, v2, v1]
    assert history[0]["parent_version_ids"] == [v2, v1] and history[0]["merge"] is True
    text = rig.ok("artifact_read", artifact_id=rig.aid, name="doc.md")["text"]
    assert "owner two\n" in text and "mine nine\n" in text and rig.ok("artifact_head", artifact_id=rig.aid)[
        "head_version_id"] == m1
    assert any(v.get("version_id") == m1 for v in rig.lineage)         # the lineage hook fired for the merge

    # ------------------------------------------------------------------ (3) overlapping edit -> conflict -> resolve
    await rig.heartbeat(advance=61)                      # past the per-minute commit throttle
    ticket = rig.commit(v1, [{"name": "doc.md", "op": "put", "text": edit(BASE_DOC, 2, "mine two")}])
    counts = await rig.cycle()
    assert counts["conflicts"] == 1
    conflict = rig.result(ticket)
    assert conflict["result"] == "conflict" and conflict["head_version_id"] == m1
    (c,) = conflict["conflicts"]
    assert c["name"] == "doc.md" and c["kind"] == "modify_modify_text"
    assert "<<<<<<<" in c["merged_text"] and "mine two" in c["merged_text"] and "owner two" in c["merged_text"]
    resolved_text = edit(text, 2, "owner two and mine two")
    await rig.heartbeat(advance=61)
    ticket = rig.commit(conflict["head_version_id"], [{"name": "doc.md", "op": "put", "text": resolved_text}])
    await rig.cycle()
    created = rig.result(ticket)
    assert created["result"] == "created"
    r1 = created["version_id"]
    async with pool.acquire() as conn:
        attempts = await CollabGrantStore(conn).attempts(rig.aid)
    assert [(x["ticket_id"], x["conflict_names"]) for x in attempts] == [
        (conflict["ticket"], [{"name": "doc.md", "kind": "modify_modify_text"}])]

    # ------------------------------------------------------------------ (4) a 1.3 MiB binary, interrupted and resumed
    await rig.heartbeat(advance=61)
    upl = upload_id_for(a.id, rig.aid, "big.bin", sha(BLOB), len(BLOB))
    total = -(-len(BLOB) // CHUNK_BYTES)
    assert total == 3

    def chunk(i: int) -> dict:
        piece = BLOB[i * CHUNK_BYTES:(i + 1) * CHUNK_BYTES]
        return {"artifact_id": rig.aid, "name": "big.bin", "size_bytes": len(BLOB), "file_sha256": sha(BLOB),
                "total": total, "index": i, "data_b64": base64.b64encode(piece).decode(), "sha256": sha(piece)}

    for i in (0, 1):                                     # interrupted after the second chunk
        assert rig.ok("artifact_stage_chunk", **chunk(i))["index"] == i
    is_error, incomplete = rig.mcp("artifact_commit", artifact_id=rig.aid, base_version_id=r1, message="big",
                                   changes=[{"name": "big.bin", "op": "put", "upload_id": upl}])
    assert is_error and incomplete["status"] == 409 and incomplete["missing"] == [2]
    resumed = rig.ok("artifact_stage_chunk", **chunk(incomplete["missing"][0]))      # resume from `missing`
    assert resumed["index"] == 2 and resumed["complete"] is True
    ticket = rig.commit(r1, [{"name": "big.bin", "op": "put", "upload_id": upl}], message="big")
    await rig.cycle()
    big = rig.result(ticket)
    assert big["result"] == "created", big
    await rig.heartbeat(advance=1)
    files = {f["name"]: f for f in rig.ok("artifact_head", artifact_id=rig.aid)["files"]}
    assert files["big.bin"]["sha256"] == sha(BLOB) and files["big.bin"]["size_bytes"] == len(BLOB)
    assert files["doc.md"]["sha256"] == sha(resolved_text.encode())
    async with pool.acquire() as conn:
        version = await ArtifactStore(conn, rig.blobs).get_version(big["version_id"])
    (stored,) = [e for e in version["output_manifest"] if e["name"] == "big.bin"]
    assert rig.blobs.get(stored["storage_key"], stored["sha256"]) == BLOB

    # ------------------------------------------------------------------ (5) the owner revokes
    other = b"leftover staging for X" * 100
    leftover = upload_id_for(a.id, rig.aid, "left.bin", sha(other), len(other))
    rig.ok("artifact_stage_chunk", artifact_id=rig.aid, name="left.bin", size_bytes=len(other), file_sha256=sha(other),
           total=1, index=0, data_b64=base64.b64encode(other).decode(), sha256=sha(other))
    assert leftover in await staged_dirs(rig)
    await rig.heartbeat(advance=61)
    head_now = rig.ok("artifact_head", artifact_id=rig.aid)["head_version_id"]
    queued = rig.commit(head_now, [{"name": "doc.md", "op": "put", "text": "queued before the revoke\n"}])
    async with pool.acquire() as conn:
        assert await CollabGrantStore(conn).revoke(rig.aid, a.id)
    await rig.cycle()                                    # tick 1: the queued commit meets the DB, not grants.json
    rejected = rig.result(queued)
    assert rejected["result"] == "rejected" and rejected["reason"] == "no write grant"
    await rig.heartbeat(advance=1)                       # tick 2 of the worker: grants.json drops X
    assert rig.bridge.list(a) == {"artifacts": []}          # collab-public's own answer
    assert "artifacts_list" not in {t["name"] for t in collab_mcp.tools_for(rig.svc, a)}      # the tools vanish
    is_error, gone = rig.mcp("artifacts_list")
    assert is_error and gone["status"] == 404
    is_error, refused = rig.mcp("artifact_commit", artifact_id=rig.aid, base_version_id=head_now, message="late",
                                changes=[{"name": "doc.md", "op": "put", "text": "late\n"}])
    assert is_error and refused["status"] == 404
    with pytest.raises(CollabError) as err:                 # the bridge itself: 404, not 403 (existence not revealed)
        rig.bridge.commit(a, {"artifact_id": rig.aid, "base_version_id": head_now, "message": "late",
                              "changes": [{"name": "doc.md", "op": "put", "text": "late\n"}]})
    assert (err.value.status, err.value.message) == (404, "not found")
    feed = json.loads((rig.outbox / "agents.json").read_text())["agents"]
    async with pool.acquire() as conn:
        swept = await rig.applier.sweep_staging(conn, agents=feed, grants=await CollabGrantStore(conn).live_grants())
    assert swept >= 1 and not await staged_dirs(rig)
    assert (await sql(pool, "SELECT current_version_id FROM artifacts WHERE artifact_id=$1", rig.aid))[0][
        "current_version_id"] == big["version_id"]


def test_artifact_collaboration_end_to_end(registry_pg, svc, tmp_path):
    async def go():
        pool = await asyncpg.create_pool(registry_pg["dsn"], min_size=1, max_size=10)
        rig = Rig(svc, tmp_path, pool)
        try:
            await asyncio.wait_for(scenario(rig, pool), 120)
        finally:
            await rig.applier.aclose(timeout=10)
            rig.bridge.stop()
            rig.mirror.close()
            await asyncio.wait_for(pool.close(), 10)
    asyncio.run(go())
