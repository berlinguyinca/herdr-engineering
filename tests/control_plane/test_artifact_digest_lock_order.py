"""I2: digest advisory locks are taken in one global (sorted) order, so a merge and a purge over overlapping digest
sets can never deadlock. Real PostgreSQL, real concurrent connections (no mocks)."""
import asyncio
import hashlib
import threading
import time

import asyncpg
import pytest

from herdr_engineering.control_plane.artifacts import ArtifactStore
from tests.control_plane import registry_pg as _registry_pg_module
from tests.control_plane.registry_pg import FakeBlobs, uid
from tests.control_plane.test_artifact_registry_ext import publish

registry_pg = _registry_pg_module.registry_pg
OWNER = "api-key:abc:operator"
BASE = "".join(f"line {i}\n" for i in range(1, 11))


def edit(text, lineno, new):
    lines = text.splitlines(keepends=True)
    lines[lineno - 1] = new + "\n"
    return "".join(lines)


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


class GatedBlobs(FakeBlobs):
    """``head`` of ``gate_key`` (called under the digest lock) announces itself and then lingers, so a concurrent
    purge reliably runs while the merge holds its first digest lock."""

    def __init__(self):
        super().__init__()
        self.gate_key, self.entered = None, threading.Event()

    def head(self, key):
        present = super().head(key)
        if key == self.gate_key:
            self.entered.set()
            time.sleep(0.4)
        return present


def entry(blobs, texts, name, text):
    key, digest = blobs.put(text.encode())
    texts[digest] = text
    return {"name": name, "media_type": "text/markdown", "sha256": digest, "size_bytes": len(text.encode()),
            "storage_key": key}


def merged_pair(tag, want_first_higher):
    """Merged results of a.md and b.md, picked so the digest of the first-registered file is higher (or lower) than
    the second's: the merge then registers its blobs against the sorted order."""
    for nonce in range(2000):
        a = edit(edit(BASE, 2, f"theirs-a {tag}"), 8, f"mine-a {tag} {nonce}")
        b = edit(edit(BASE, 2, f"theirs-b {tag}"), 8, f"mine-b {tag} {nonce}")
        if (sha(a) > sha(b)) == want_first_higher:
            return a, b, nonce
    raise AssertionError("no nonce found")


def one_round(dsn, tag, want_first_higher):
    """A merge that registers two new text blobs (a.md first) while a purge of a tombstoned artifact holding the same
    two blobs runs on its own connection. Returns (merge result, purge result)."""
    a_merged, b_merged, nonce = merged_pair(tag, want_first_higher)
    texts, merge_blobs, purge_blobs = {}, GatedBlobs(), FakeBlobs()
    box = {}

    async def go():
        c1, c2 = await asyncpg.connect(dsn), await asyncpg.connect(dsn)
        try:
            store = ArtifactStore(c1, merge_blobs)
            aid = (await store.register_logical(original_filename="doc.md", created_by=OWNER))["artifact_id"]
            v1 = await store.commit_version(
                aid, base_version_id=None, source_manifest_blob=None, created_by=OWNER,
                output_manifest=[entry(merge_blobs, texts, "a.md", BASE), entry(merge_blobs, texts, "b.md", BASE)])
            await store.commit_version(
                aid, base_version_id=v1["version_id"], source_manifest_blob=None, created_by=OWNER,
                output_manifest=[entry(merge_blobs, texts, "a.md", edit(BASE, 2, f"theirs-a {tag}")),
                                 entry(merge_blobs, texts, "b.md", edit(BASE, 2, f"theirs-b {tag}"))])
            mine = [{"name": "a.md", "op": "put",
                     "entry": entry(merge_blobs, texts, "a.md", edit(BASE, 8, f"mine-a {tag} {nonce}"))},
                    {"name": "b.md", "op": "put",
                     "entry": entry(merge_blobs, texts, "b.md", edit(BASE, 8, f"mine-b {tag} {nonce}"))}]
            # the purge target owns the very bytes the merge is about to register
            pstore = ArtifactStore(c2, purge_blobs)
            pid, _ = await publish(pstore, purge_blobs, owner=OWNER, name="a.md", media="text/markdown",
                                   data=a_merged.encode())
            pkey, _ = purge_blobs.put(b_merged.encode())
            await c2.execute("INSERT INTO artifact_blobs (scope, blob_sha256, storage_key, size_bytes, media_type) "
                             "VALUES ($1,$2,$3,$4,'text/markdown')", pid, sha(b_merged), pkey, len(b_merged.encode()))
            await pstore.tombstone_artifact(pid, actor=OWNER, reason="r")
            merge_blobs.gate_key = FakeBlobs.key(sha(a_merged))

            async def purge():
                c = await asyncpg.connect(dsn)
                try:
                    await asyncio.get_running_loop().run_in_executor(None, merge_blobs.entered.wait, 15)
                    return await ArtifactStore(c, purge_blobs).purge_now(pid, actor=OWNER, reason="x")
                finally:
                    await c.close()
            thread = threading.Thread(target=lambda: box.update(purge=_run_catching(purge)), daemon=True)
            thread.start()
            try:
                box["merge"] = await store.merge_commit(
                    aid, base_version_id=v1["version_id"], changes=mine, read_text=lambda e: texts[e["sha256"]],
                    message="m", created_by="collab:agent", collaborator_agent_id="agent")
            except BaseException as exc:                    # noqa: BLE001 - handed back to the assertions
                box["merge"] = exc
            merge_blobs.entered.set()
            thread.join(30)
        finally:
            await c1.close()
            await c2.close()
    asyncio.run(go())
    return box["merge"], box["purge"]


def _run_catching(coro_fn):
    try:
        return asyncio.run(coro_fn())
    except BaseException as exc:                            # noqa: BLE001
        return exc


@pytest.mark.parametrize("first_is_higher", [True, False])
def test_i2_a_merge_and_a_purge_over_the_same_digests_never_deadlock(registry_pg, first_is_higher):
    for round_no in range(6):
        merge, purge = one_round(registry_pg["dsn"], f"{uid('t')}-{round_no}", first_is_higher)
        assert not isinstance(merge, BaseException), merge
        assert not isinstance(purge, BaseException), purge
        assert merge["result"] == "merged" and purge["result"] == "deleted"
        # the merge's references won: the purge kept every byte the new head uses
        assert len(purge["retained_shared"]) == 2
