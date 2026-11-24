# ruff: noqa: F811  (imported pytest fixtures are used as test arguments)
"""N2: a deleted artifact leaves the collaborator mirror, the published grants, the grant lookups and the MCP read path.

A real CollabMirror over a real PostgreSQL registry, and the real CollabArtifactBridge that serves the MCP artifact tools
reading that mirror (the fixtures of test_collab_artifacts.py supply the collaboration service the bridge belongs to)."""
import asyncio
import os
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from herdr_engineering.control_plane.artifact_collab_store import CollabGrantStore
from herdr_engineering.control_plane.artifacts import ArtifactStore
from herdr_engineering.control_plane.collab import CollabError
from herdr_engineering.control_plane.collab_artifacts import CollabArtifactBridge
from herdr_engineering.control_plane.collab_bridge_files import BridgeLimits
from herdr_engineering.control_plane.collab_mirror import CollabMirror
from tests.control_plane.registry_pg import FakeBlobs, registry_pg  # noqa: F401 - imported fixture
from tests.control_plane.test_collab_artifacts import svc, world  # noqa: F401  (fixtures)
from tests.control_plane.test_collab_mirror import (
    OWNER,
    gate_for,
    grant,
    head_doc,
    make_artifact,
)

OPERATOR = "api-key:abc:operator"


def real_run(pg, body, blobs=None):
    blobs = blobs if blobs is not None else FakeBlobs()

    async def go():
        conn = await asyncpg.connect(pg["dsn"])
        try:
            await conn.execute("DELETE FROM artifact_acl WHERE principal_id LIKE 'collab:%'")
            return await body(ArtifactStore(conn, blobs), conn, blobs)
        finally:
            await conn.close()
    return asyncio.run(go())


def mirror_dir(root, aid):
    return (root / "artifacts" / aid).exists()


def grants_json(root):
    import json
    return json.loads((root / "grants.json").read_text())


def test_a_tombstoned_artifact_leaves_the_mirror_the_published_grants_and_the_grant_lookups(registry_pg, tmp_path):
    async def body(store, conn, blobs):
        aid, _ = await make_artifact(store, blobs, {"doc.md": b"secret body\n"})
        await grant(conn, aid)
        mirror = CollabMirror(tmp_path / "mirror", rustfs=blobs, limits=BridgeLimits())
        root = tmp_path / "mirror"
        grants = CollabGrantStore(conn)
        assert await mirror.refresh(store, aid, gate=await gate_for(store))
        await mirror.publish_grants(grants, store, gate=await gate_for(store))
        assert aid in grants_json(root)["agents"]["agent-m1"] and mirror_dir(root, aid)

        await store.tombstone_artifact(aid, actor=OPERATOR, reason="gone")
        # the grant lookups (the intent applier's write-side re-check) and the live list no longer see it
        assert await grants.grant_for(aid, "agent-m1") is None
        assert aid not in {g["artifact_id"] for g in await grants.live_grants()}
        # the published grants drop it, even from a list snapshot taken earlier
        stale_rows = [{"artifact_id": aid, "agent_id": "agent-m1", "permission": "write",
                       "expires_at": datetime.now(UTC) + timedelta(days=1)}]
        await mirror.publish_grants(stale_rows, store, gate=await gate_for(store), heartbeat=0.0)
        assert "agent-m1" not in grants_json(root)["agents"] or aid not in grants_json(root)["agents"]["agent-m1"]
        # the applier's refresh removes the folder
        assert await mirror.refresh(store, aid, gate=await gate_for(store)) == {}
        assert not mirror_dir(root, aid)

        # restoring brings the grant back and the next refresh mirrors it again
        await store.restore_artifact(aid, actor=OPERATOR)
        assert (await grants.grant_for(aid, "agent-m1"))["permission"] == "write"
        assert head_doc(root, aid) if await mirror.refresh(store, aid, gate=await gate_for(store)) else False
    real_run(registry_pg, body)


def test_sync_removes_the_folder_of_a_tombstoned_artifact_and_a_purge_keeps_it_gone(registry_pg, tmp_path):
    async def body(store, conn, blobs):
        aid, v1 = await make_artifact(store, blobs, {"doc.md": b"secret body\n"})
        await grant(conn, aid)
        mirror = CollabMirror(tmp_path / "mirror", rustfs=blobs, limits=BridgeLimits())
        root = tmp_path / "mirror"
        await mirror.sync(store, gate=await gate_for(store))
        assert mirror_dir(root, aid) and os.listdir(root / "artifacts" / aid / "blobs")

        await store.tombstone_artifact(aid, actor=OPERATOR, reason="gone")
        out = await mirror.sync(store, gate=await gate_for(store))
        assert not mirror_dir(root, aid) and aid not in out["refreshed"]

        # even if the worker restarted with no memory of the artifact, a stray folder is swept
        await store.restore_artifact(aid, actor=OPERATOR)
        await mirror.sync(store, gate=await gate_for(store))
        assert mirror_dir(root, aid)
        assert (await store.purge_now(aid, actor=OPERATOR, reason="x"))["result"] == "deleted"
        await mirror.sync(store, gate=await gate_for(store))
        assert not mirror_dir(root, aid)
        assert await mirror.refresh(store, aid, gate=await gate_for(store)) == {}
        assert not mirror_dir(root, aid)                                  # nothing of the bytes outlives the purge
    real_run(registry_pg, body)


def test_a_pending_invite_grant_does_not_resolve_onto_a_tombstoned_artifact(registry_pg):
    async def body(store, conn, blobs):
        aid, _ = await make_artifact(store, blobs, {"doc.md": b"x\n"})
        grants = CollabGrantStore(conn)
        invite = "inv-" + aid[-8:]
        await grants.add_invite_grant(invite, aid, "write", expires_at=datetime.now(UTC) + timedelta(days=1),
                                      granted_by=OWNER)
        await store.tombstone_artifact(aid, actor=OPERATOR, reason="gone")
        assert await grants.resolve_invite_grants({invite: {"agent_id": "agent-late", "hard_expiry": None}}) == 0
        assert await grants.grant_for(aid, "agent-late") is None
    real_run(registry_pg, body)


def test_the_mcp_artifact_tools_stop_serving_a_tombstoned_artifact(registry_pg, world, tmp_path):
    """Through the real bridge: list, head, history, read and annotations all stop after the delete."""
    pr = world.a

    async def body(store, conn, blobs):
        aid, _ = await make_artifact(store, blobs, {"doc.md": b"secret body\n"})
        # the service's fake clock sits at 2027-01-15: the grant must outlive it (the store allows up to 90 days)
        await CollabGrantStore(conn).grant(aid, pr.id, "read", expires_at=datetime.now(UTC) + timedelta(days=85),
                                           granted_by=OWNER)
        mirror = CollabMirror(tmp_path / "real", rustfs=blobs, limits=BridgeLimits(), clock=world.clock)
        grants = CollabGrantStore(conn)
        await mirror.sync(store, gate=await gate_for(store))
        await mirror.publish_grants(grants, store, gate=await gate_for(store))
        outbox = tmp_path / "real-outbox"
        outbox.mkdir()
        bridge = CollabArtifactBridge(world.s, mirror.root, outbox, clock=world.clock, feed_interval=3600.0)
        try:
            assert [a["artifact_id"] for a in bridge.list(pr)["artifacts"]] == [aid]
            assert bridge.head(pr, aid)["files"][0]["name"] == "doc.md"
            assert bridge.read(pr, aid, "doc.md")["text"].startswith("secret body")
            assert bridge.history(pr, aid)["versions"]

            await store.tombstone_artifact(aid, actor=OPERATOR, reason="gone")
            await mirror.sync(store, gate=await gate_for(store))
            await mirror.publish_grants(grants, store, gate=await gate_for(store), heartbeat=0.0)
            bridge.stop()
            bridge = CollabArtifactBridge(world.s, mirror.root, outbox, clock=world.clock, feed_interval=3600.0)

            assert bridge.list(pr)["artifacts"] == []
            for call in (lambda: bridge.head(pr, aid), lambda: bridge.history(pr, aid),
                         lambda: bridge.read(pr, aid, "doc.md"), lambda: bridge.annotations(pr, aid)):
                with pytest.raises(CollabError) as caught:
                    call()
                assert caught.value.status in (403, 404)
        finally:
            bridge.stop()
    real_run(registry_pg, body)
