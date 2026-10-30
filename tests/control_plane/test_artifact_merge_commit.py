"""merge_commit: collaborator commits against a possibly stale base (fast-forward, clean merge, conflict)."""
import asyncio
import hashlib
import json

import asyncpg
import pytest

from herdr_engineering.control_plane.artifact_merge import FileConflict
from herdr_engineering.control_plane.artifacts import (
    ArtifactError,
    ArtifactMergeConflict,
    ArtifactStore,
)
from tests.control_plane import test_artifact_version_parents as _parents

_Blobs = _parents._Blobs
parents_postgres = _parents.parents_postgres  # the isolated 0042 Postgres fixture, shared

AGENT = "agent-c"
COLLAB = f"collab:{AGENT}"


class Texts:
    """In-memory blob text store standing in for RustFS: keyed by sha256."""

    def __init__(self):
        self.by_sha = {}
        self.stored = []

    def entry(self, name, text, media_type="text/markdown"):
        digest = hashlib.sha256(text.encode()).hexdigest()
        self.by_sha[digest] = text
        return {"name": name, "media_type": media_type, "sha256": digest, "size_bytes": len(text.encode()),
                "storage_key": f"sha256/{digest[:2]}/{digest[2:4]}/{digest}"}

    def read_text(self, entry):
        return self.by_sha[entry["sha256"]]

    def store_text(self, name, template):
        self.stored.append(name)
        entry = {k: v for k, v in template.items() if k not in ("text", "sha256", "storage_key", "size_bytes")}
        return {**entry, **{k: v for k, v in self.entry(name, template["text"], template.get("media_type")).items()
                            if k != "name"}, "name": name}


def run(dsn, body):
    async def go():
        conn = await asyncpg.connect(dsn)
        try:
            return await body(ArtifactStore(conn, _Blobs()), conn)
        finally:
            await conn.close()
    return asyncio.run(go())


BASE_DOC = "".join(f"line {i}\n" for i in range(1, 11))


async def setup(store, texts, files=None):
    aid = (await store.register_logical(original_filename="doc.md", created_by="owner"))["artifact_id"]
    manifest = files if files is not None else [texts.entry("doc.md", BASE_DOC)]
    v1 = await store.commit_version(aid, base_version_id=None, source_manifest_blob=None,
                                    output_manifest=manifest, created_by="owner")
    return aid, v1


def edit(text, lineno, new):
    lines = text.splitlines(keepends=True)
    lines[lineno - 1] = new + "\n"
    return "".join(lines)


async def owner_commit(store, aid, base, manifest):
    return await store.commit_version(aid, base_version_id=base, source_manifest_blob=None,
                                      output_manifest=manifest, created_by="owner")


async def merge(store, texts, aid, base, changes, **kwargs):
    kwargs.setdefault("message", "collab edit")
    return await store.merge_commit(aid, base_version_id=base, changes=changes, read_text=texts.read_text,
                                    store_text=texts.store_text, created_by=COLLAB,
                                    collaborator_agent_id=AGENT, **kwargs)


async def attempts(conn, aid):
    return await conn.fetch("SELECT * FROM artifact_collab_attempts WHERE artifact_id=$1", aid)


def test_fast_forward_is_created_with_head_as_sole_parent(parents_postgres):
    texts = Texts()

    async def body(store, conn):
        aid, v1 = await setup(store, texts)
        new = texts.entry("doc.md", edit(BASE_DOC, 2, "mine"))
        out = await merge(store, texts, aid, v1["version_id"], [{"name": "doc.md", "op": "put", "entry": new}])
        assert out["result"] == "created" and out["merged_names"] == []
        version = out["version"]
        assert version["parent_version_ids"] == [v1["version_id"]]
        assert version["created_via"] == "collaborator" and version["collaborator_agent_id"] == AGENT
        assert [e["sha256"] for e in version["output_manifest"]] == [new["sha256"]]
        row = await conn.fetchrow("SELECT current_version_id FROM artifacts WHERE artifact_id=$1", aid)
        assert row["current_version_id"] == version["version_id"]
        assert texts.stored == []
    run(parents_postgres, body)


def test_stale_base_with_disjoint_edits_merges_with_two_parents(parents_postgres):
    texts = Texts()

    async def body(store, conn):
        aid, v1 = await setup(store, texts)
        head_text = edit(BASE_DOC, 2, "owner")
        v2 = await owner_commit(store, aid, v1["version_id"], [texts.entry("doc.md", head_text)])
        mine = texts.entry("doc.md", edit(BASE_DOC, 9, "mine"))
        out = await merge(store, texts, aid, v1["version_id"], [{"name": "doc.md", "op": "put", "entry": mine}])
        assert out["result"] == "merged" and out["merged_names"] == ["doc.md"]
        version = out["version"]
        assert version["parent_version_ids"] == [v2["version_id"], v1["version_id"]]
        assert version["base_version_id"] == v2["version_id"]
        (entry,) = version["output_manifest"]
        merged = texts.read_text(entry)
        assert "owner\n" in merged and "mine\n" in merged
        assert entry["sha256"] == hashlib.sha256(merged.encode()).hexdigest()
        assert entry["size_bytes"] == len(merged.encode()) and "text" not in entry
        assert (await store.get_version(version["version_id"]))["provenance"]["message"] == "collab edit"
        assert await attempts(conn, aid) == []
    run(parents_postgres, body)


def test_overlapping_edits_conflict_store_nothing_and_log_one_attempt(parents_postgres):
    texts = Texts()

    async def body(store, conn):
        aid, v1 = await setup(store, texts)
        v2 = await owner_commit(store, aid, v1["version_id"], [texts.entry("doc.md", edit(BASE_DOC, 5, "owner"))])
        mine = texts.entry("doc.md", edit(BASE_DOC, 5, "mine"))
        with pytest.raises(ArtifactMergeConflict) as caught:
            await merge(store, texts, aid, v1["version_id"], [{"name": "doc.md", "op": "put", "entry": mine}])
        err = caught.value
        assert isinstance(err, ArtifactError)
        assert err.head_version_id == v2["version_id"]
        (conflict,) = err.conflicts
        assert isinstance(conflict, FileConflict) and conflict.kind == "modify_modify_text"
        assert "<<<<<<<" in conflict.merged_text and "=======" in conflict.merged_text
        assert ">>>>>>>" in conflict.merged_text
        assert texts.stored == []
        assert len(await store.list_versions(aid)) == 2
        row = await conn.fetchrow("SELECT current_version_id FROM artifacts WHERE artifact_id=$1", aid)
        assert row["current_version_id"] == v2["version_id"]
        (attempt,) = await attempts(conn, aid)
        assert attempt["collaborator_agent_id"] == AGENT
        assert attempt["base_version_id"] == v1["version_id"] and attempt["head_version_id"] == v2["version_id"]
        assert attempt["message"] == "collab edit"
        assert json.loads(attempt["conflict_names"]) == [{"name": "doc.md", "kind": "modify_modify_text"}]
    run(parents_postgres, body)


def test_delete_versus_modify_is_a_conflict(parents_postgres):
    texts = Texts()

    async def body(store, conn):
        other = texts.entry("notes.txt", "keep\n")
        aid, v1 = await setup(store, texts, [texts.entry("doc.md", BASE_DOC), other])
        v2 = await owner_commit(store, aid, v1["version_id"],
                                [texts.entry("doc.md", edit(BASE_DOC, 1, "owner")), other])
        with pytest.raises(ArtifactMergeConflict) as caught:
            await merge(store, texts, aid, v1["version_id"], [{"name": "doc.md", "op": "delete"}])
        assert [(c.name, c.kind) for c in caught.value.conflicts] == [("doc.md", "delete_modify")]
        assert caught.value.head_version_id == v2["version_id"]
        assert caught.value.clean_names == ["notes.txt"]
        assert len(await attempts(conn, aid)) == 1
    run(parents_postgres, body)


def test_delete_of_absent_name_is_a_no_op_and_put_needs_entry(parents_postgres):
    texts = Texts()

    async def body(store, conn):
        aid, v1 = await setup(store, texts)
        out = await merge(store, texts, aid, v1["version_id"], [{"name": "ghost.md", "op": "delete"}])
        assert [e["name"] for e in out["version"]["output_manifest"]] == ["doc.md"]
        with pytest.raises(ArtifactError):
            await merge(store, texts, aid, out["version"]["version_id"], [{"name": "x.md", "op": "put"}])
        with pytest.raises(ArtifactError):
            await merge(store, texts, aid, out["version"]["version_id"],
                        [{"name": "x.md", "op": "put", "entry": texts.entry("y.md", "y\n")}])
    run(parents_postgres, body)


def test_replay_with_same_idempotency_key_returns_same_version(parents_postgres):
    texts = Texts()

    async def body(store, conn):
        aid, v1 = await setup(store, texts)
        await owner_commit(store, aid, v1["version_id"], [texts.entry("doc.md", edit(BASE_DOC, 2, "owner"))])
        changes = [{"name": "doc.md", "op": "put", "entry": texts.entry("doc.md", edit(BASE_DOC, 9, "mine"))}]
        first = await merge(store, texts, aid, v1["version_id"], changes, idempotency_key="k1")
        again = await merge(store, texts, aid, v1["version_id"], changes, idempotency_key="k1")
        assert again["version"]["version_id"] == first["version"]["version_id"]
        assert again["result"] == first["result"] == "merged"
        assert again["merged_names"] == first["merged_names"] == ["doc.md"]
        assert len(await store.list_versions(aid)) == 3
        with pytest.raises(ArtifactError, match="idempotency"):
            await merge(store, texts, aid, v1["version_id"], [{"name": "doc.md", "op": "delete"}],
                        idempotency_key="k1")
    run(parents_postgres, body)


def test_concurrent_identical_calls_produce_one_version(parents_postgres):
    texts = Texts()

    async def go():
        a = await asyncpg.connect(parents_postgres)
        b = await asyncpg.connect(parents_postgres)
        try:
            sa, sb = ArtifactStore(a, _Blobs()), ArtifactStore(b, _Blobs())
            aid, v1 = await setup(sa, texts)
            await owner_commit(sa, aid, v1["version_id"], [texts.entry("doc.md", edit(BASE_DOC, 2, "owner"))])
            changes = [{"name": "doc.md", "op": "put", "entry": texts.entry("doc.md", edit(BASE_DOC, 9, "mine"))}]
            one, two = await asyncio.gather(
                merge(sa, texts, aid, v1["version_id"], changes, idempotency_key="race"),
                merge(sb, texts, aid, v1["version_id"], changes, idempotency_key="race"))
            assert one["version"]["version_id"] == two["version"]["version_id"]
            assert len(await sa.list_versions(aid)) == 3
        finally:
            await a.close()
            await b.close()
    asyncio.run(go())


def test_report_and_renderer_artifacts_are_not_writable(parents_postgres):
    texts = Texts()

    async def body(store, conn):
        aid, v1 = await setup(store, texts)
        await conn.execute("UPDATE artifacts SET artifact_type='report' WHERE artifact_id=$1", aid)
        with pytest.raises(ArtifactError, match="not writable by collaboration"):
            await merge(store, texts, aid, v1["version_id"], [{"name": "doc.md", "op": "delete"}])
        rendered = (await store.register_logical(original_filename="r.md", created_by="owner"))["artifact_id"]
        source = texts.entry("source.json", "{}")
        r1 = await store.commit_version(rendered, base_version_id=None, source_manifest_blob=source["storage_key"],
                                        output_manifest=[texts.entry("r.md", "x\n")], created_by="owner")
        with pytest.raises(ArtifactError, match="not writable by collaboration"):
            await merge(store, texts, rendered, r1["version_id"], [{"name": "r.md", "op": "delete"}])
        assert await attempts(conn, aid) == [] and await attempts(conn, rendered) == []
    run(parents_postgres, body)


def test_retracted_artifact_refuses_merge(parents_postgres):
    texts = Texts()

    async def body(store, conn):
        aid, v1 = await setup(store, texts)
        await store.record_retraction(aid)
        with pytest.raises(ArtifactError, match="retracted"):
            await merge(store, texts, aid, v1["version_id"], [{"name": "doc.md", "op": "delete"}])
        assert len(await store.list_versions(aid)) == 1
    run(parents_postgres, body)


def test_base_must_belong_to_the_artifact(parents_postgres):
    texts = Texts()

    async def body(store, conn):
        aid, v1 = await setup(store, texts)
        other, o1 = await setup(store, texts)
        with pytest.raises(ArtifactError):
            await merge(store, texts, aid, o1["version_id"], [{"name": "doc.md", "op": "delete"}])
    run(parents_postgres, body)
