"""Parent lists, created_via and the attempt log (migration 0043) on an isolated, Unix-socket-only PostgreSQL."""
import asyncio
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import asyncpg
import pytest

from herdr_engineering.control_plane.artifacts import ArtifactError, ArtifactStore

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "deploy/control-plane/migrations"
PREREQ = ("0001_control_plane.sql", "0003_artifact_registry.sql", "0005_artifact_binding_context.sql",
          "0006_artifact_acl.sql", "0021_artifact_reports.sql", "0031_artifact_registry_extensions.sql")
MIGRATION = "0043_artifact_version_parents.sql"


class _Blobs:
    def head(self, reference):
        return True


@pytest.fixture(scope="module")
def parents_postgres():
    if os.getuid() == 0:
        pytest.skip("isolated initdb cannot run as root")
    candidates = []
    config = shutil.which("pg_config")
    if config:
        candidates.append(Path(subprocess.check_output([config, "--bindir"], text=True).strip()) / "initdb")
    candidates += sorted(Path("/usr/lib/postgresql").glob("*/bin/initdb"), reverse=True)
    initdb = next((c for c in candidates if c.is_file()), None)
    if initdb is None:
        pytest.skip("PostgreSQL server binaries unavailable")
    bindir = initdb.parent
    with tempfile.TemporaryDirectory(prefix="parents-pg-") as folder:
        area = Path(folder)
        data = area / "data"
        subprocess.run([str(initdb), "-D", str(data), "-A", "trust", "--no-locale", "-E", "UTF8", "--no-sync"],
                       check=True, capture_output=True)
        subprocess.run([str(bindir / "pg_ctl"), "-D", str(data), "-l", str(area / "server.log"),
                        "-o", f"-c listen_addresses='' -k {area}", "-w", "start"],
                       check=True, capture_output=True)
        try:
            dsn = f"postgresql:///postgres?host={area}"

            async def initialize():
                conn = await asyncpg.connect(dsn)
                try:
                    for name in PREREQ:
                        await conn.execute((MIGRATIONS / name).read_text())
                    # A version that predates 0043, to prove the backfill.
                    await conn.execute("INSERT INTO artifacts (artifact_id,created_by) VALUES ('legacy','o')")
                    await conn.execute("INSERT INTO artifact_versions (version_id,artifact_id,version_number,state) "
                                       "VALUES ('legacy-1','legacy',1,'ready')")
                    await conn.execute("INSERT INTO artifact_versions (version_id,artifact_id,version_number,state,"
                                       "parent_version_id) VALUES ('legacy-2','legacy',2,'ready','legacy-1')")
                    await conn.execute((MIGRATIONS / MIGRATION).read_text())
                    await conn.execute((MIGRATIONS / MIGRATION).read_text())  # idempotent: second run is a no-op
                finally:
                    await conn.close()

            asyncio.run(initialize())
            yield dsn
        finally:
            subprocess.run([str(bindir / "pg_ctl"), "-D", str(data), "-m", "fast", "-w", "stop"],
                           check=True, capture_output=True)


def run(dsn, body):
    async def go():
        conn = await asyncpg.connect(dsn)
        try:
            return await body(ArtifactStore(conn, _Blobs()), conn)
        finally:
            await conn.close()
    return asyncio.run(go())


async def new_artifact(store):
    return (await store.register_logical(original_filename="a.md", created_by="owner"))["artifact_id"]


async def commit(store, aid, base, **kwargs):
    return await store.commit_version(aid, base_version_id=base, source_manifest_blob=None, output_manifest=[],
                                      created_by="owner", **kwargs)


def test_migration_backfills_existing_versions_and_creates_attempt_log(parents_postgres):
    async def body(store, conn):
        rows = {r["version_id"]: r for r in await conn.fetch(
            "SELECT version_id, parent_version_ids, created_via, collaborator_agent_id FROM artifact_versions "
            "WHERE artifact_id='legacy'")}
        assert rows["legacy-1"]["parent_version_ids"] == []
        assert rows["legacy-2"]["parent_version_ids"] == ["legacy-1"]
        assert rows["legacy-2"]["created_via"] == "owner" and rows["legacy-2"]["collaborator_agent_id"] is None
        await conn.execute("INSERT INTO artifact_collab_attempts (attempt_id, artifact_id, collaborator_agent_id) "
                           "VALUES ('att-1','legacy','agent-x')")
        row = await conn.fetchrow("SELECT conflict_names, created_at FROM artifact_collab_attempts")
        assert row["created_at"] is not None
        assert await conn.fetchval("SELECT count(*) FROM pg_indexes WHERE tablename='artifact_collab_attempts' "
                                   "AND indexname='artifact_collab_attempts_artifact_idx'") == 1
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute("INSERT INTO artifact_versions (version_id,artifact_id,version_number,state,created_via) "
                               "VALUES ('legacy-bad','legacy',9,'ready','other')")
    run(parents_postgres, body)


def test_linear_commits_record_base_as_sole_parent(parents_postgres):
    async def body(store, conn):
        aid = await new_artifact(store)
        first = await commit(store, aid, None)
        second = await commit(store, aid, first["version_id"])
        assert first["parent_version_ids"] == [] and first["created_via"] == "owner"
        assert second["parent_version_ids"] == [first["version_id"]]
        assert second["parent_version_id"] == first["version_id"] == second["base_version_id"]
        assert second["collaborator_agent_id"] is None
        got = await store.get_version(second["version_id"])
        assert got["parent_version_ids"] == [first["version_id"]]
        assert [v["parent_version_ids"] for v in await store.list_versions(aid)] == [[first["version_id"]], []]
        assert [v["parent_version_ids"] for v in await store.list_version_summaries(aid)] == [
            [first["version_id"]], []]
    run(parents_postgres, body)


def test_merge_commit_stores_both_parents_in_order(parents_postgres):
    async def body(store, conn):
        aid = await new_artifact(store)
        first = await commit(store, aid, None)
        second = await commit(store, aid, first["version_id"])
        merged = await commit(store, aid, second["version_id"], created_via="collaborator",
                              collaborator_agent_id="agent-7",
                              parent_version_ids=[second["version_id"], first["version_id"]])
        assert merged["parent_version_ids"] == [second["version_id"], first["version_id"]]
        assert merged["parent_version_id"] == second["version_id"]
        assert merged["created_via"] == "collaborator" and merged["collaborator_agent_id"] == "agent-7"
        got = await store.get_version(merged["version_id"])
        assert got["parent_version_ids"] == [second["version_id"], first["version_id"]]
        assert got["created_via"] == "collaborator" and got["collaborator_agent_id"] == "agent-7"
        summary = (await store.list_version_summaries(aid))[0]
        assert summary["parent_version_ids"] == [second["version_id"], first["version_id"]]
        assert summary["created_via"] == "collaborator"
    run(parents_postgres, body)


def test_parent_from_another_artifact_is_refused(parents_postgres):
    async def body(store, conn):
        mine, other = await new_artifact(store), await new_artifact(store)
        foreign = await commit(store, other, None)
        first = await commit(store, mine, None)
        with pytest.raises(ArtifactError, match="parent"):
            await commit(store, mine, first["version_id"], parent_version_ids=[first["version_id"],
                                                                                foreign["version_id"]])
        with pytest.raises(ArtifactError, match="parent"):
            await commit(store, mine, first["version_id"], parent_version_ids=["version_" + uuid.uuid4().hex])
        assert len(await store.list_versions(mine)) == 1
    run(parents_postgres, body)


def test_collaborator_commit_requires_agent_id_and_valid_origin(parents_postgres):
    async def body(store, conn):
        aid = await new_artifact(store)
        with pytest.raises(ArtifactError, match="collaborator_agent_id"):
            await commit(store, aid, None, created_via="collaborator")
        with pytest.raises(ArtifactError, match="created_via"):
            await commit(store, aid, None, created_via="stranger")
        assert await store.list_versions(aid) == []
    run(parents_postgres, body)


def test_idempotency_key_distinguishes_parent_lists(parents_postgres):
    async def body(store, conn):
        aid = await new_artifact(store)
        first = await commit(store, aid, None)
        second = await commit(store, aid, first["version_id"])
        kw = dict(idempotency_key="k-" + uuid.uuid4().hex)
        merged = await commit(store, aid, second["version_id"], parent_version_ids=[second["version_id"],
                                                                                   first["version_id"]], **kw)
        again = await commit(store, aid, second["version_id"], parent_version_ids=[second["version_id"],
                                                                                  first["version_id"]], **kw)
        assert again["version_id"] == merged["version_id"]
        with pytest.raises(ArtifactError, match="idempotency"):
            await commit(store, aid, second["version_id"], parent_version_ids=[second["version_id"]], **kw)
    run(parents_postgres, body)


def test_parents_must_start_with_base(parents_postgres):
    async def body(store, conn):
        aid = await new_artifact(store)
        v1 = await commit(store, aid, None)
        v2 = await commit(store, aid, v1["version_id"])
        h = v2["version_id"]
        for base, parents in [(h, [v1["version_id"]]), (h, []), (h, [v1["version_id"], h])]:
            with pytest.raises(ArtifactError, match="start with base_version_id"):
                await commit(store, aid, base, parent_version_ids=parents)
        other = await new_artifact(store)
        with pytest.raises(ArtifactError, match="empty for a first version"):
            await commit(store, other, None, parent_version_ids=[v1["version_id"]])
        merged = await commit(store, aid, h, parent_version_ids=[h, v1["version_id"]])
        assert merged["parent_version_ids"] == [h, v1["version_id"]]
        assert len(await store.list_versions(aid)) == 3
    run(parents_postgres, body)


def test_owner_cannot_carry_agent_id_and_bare_string_parents_are_refused(parents_postgres):
    async def body(store, conn):
        aid = await new_artifact(store)
        with pytest.raises(ArtifactError, match="owner commits cannot"):
            await commit(store, aid, None, collaborator_agent_id="agent-1")
        v1 = await commit(store, aid, None)
        with pytest.raises(ArtifactError, match="not a single string"):
            await commit(store, aid, v1["version_id"], parent_version_ids=v1["version_id"])
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute("INSERT INTO artifact_versions (version_id,artifact_id,version_number,state,"
                               "created_via,collaborator_agent_id) VALUES ('cv-a',$1,50,'ready','owner','x')", aid)
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute("INSERT INTO artifact_versions (version_id,artifact_id,version_number,state,"
                               "created_via) VALUES ('cv-b',$1,51,'ready','collaborator')", aid)
    run(parents_postgres, body)


def test_migrator_applies_0043_and_leaves_freeze_trigger_enabled(parents_postgres, tmp_path):
    from herdr_engineering.control_plane.db import Migrator
    for name in (*PREREQ, MIGRATION):
        (tmp_path / name).write_text((MIGRATIONS / name).read_text())

    async def go():
        conn = await asyncpg.connect(parents_postgres)
        try:
            await conn.execute("CREATE SCHEMA mig")
            await conn.execute("SET search_path TO mig")
            assert MIGRATION in await Migrator(conn).apply(str(tmp_path))
            enabled = await conn.fetchval(
                "SELECT tgenabled FROM pg_trigger WHERE tgname='artifact_versions_freeze' "
                "AND tgrelid='mig.artifact_versions'::regclass")
            assert enabled in ("O", b"O")
            await conn.execute("INSERT INTO artifacts (artifact_id,created_by) VALUES ('m','o')")
            await conn.execute("INSERT INTO artifact_versions (version_id,artifact_id,version_number,state) "
                               "VALUES ('m-1','m',1,'ready')")
            with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
                await conn.execute("UPDATE artifact_versions SET parent_version_ids=ARRAY['x'] WHERE version_id='m-1'")
        finally:
            await conn.close()
    asyncio.run(go())
