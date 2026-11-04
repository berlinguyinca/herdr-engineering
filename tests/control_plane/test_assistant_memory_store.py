"""Durable journal contracts exercised against a disposable PostgreSQL cluster."""

import asyncio
import shutil
import tempfile
from pathlib import Path

import asyncpg
import pytest

from tests.e2e.pg import ThrowawayPostgres, find_bins


@pytest.fixture(scope="module")
def pg(tmp_path_factory):
    bins = find_bins({})
    if bins is None:
        pytest.fail("PostgreSQL binaries required")
    cluster = ThrowawayPostgres(bins, tmp_path_factory.mktemp("memory-pg"))
    # Keep socket paths below PostgreSQL's Unix socket length limit.
    shutil.rmtree(cluster.sock)
    cluster.sock = Path(tempfile.mkdtemp(prefix="memory-pg-", dir="/tmp"))
    try:
        cluster.start()
        yield cluster
    finally:
        cluster.close()


def test_durable_cas_scope_outbox_and_restart(pg):
    async def exercise():
        from herdr_engineering.control_plane.assistant_memory_store import (
            AssistantMemoryStore,
            RevisionConflict,
        )

        conn = await asyncpg.connect(pg.dsn)
        other = await asyncpg.connect(pg.dsn)
        try:
            store = AssistantMemoryStore(conn, "tenant", "user", "assistant")
            peer = AssistantMemoryStore(other, "tenant", "user", "assistant")
            payload = dict(
                content="  Exact\n秘密 ",
                provenance={"source": "thread:1"},
                capture_kind="fact",
                approval_metadata={"actor": "server"},
            )
            first = await store.record("doc", expected_revision=0, **payload)
            assert first["revision"] == 1 and first["content"] == payload["content"]
            assert first["approval_metadata"] == payload["approval_metadata"]
            assert (
                await AssistantMemoryStore(other, "wrong", "user", "assistant").get("doc") is None
            )
            with pytest.raises(RevisionConflict):
                await AssistantMemoryStore(other, "wrong", "user", "assistant").record(
                    "doc", expected_revision=0, **payload
                )
            results = await asyncio.gather(
                store.record("doc", expected_revision=1, **payload),
                peer.record("doc", expected_revision=1, **payload),
                return_exceptions=True,
            )
            assert sum(isinstance(r, RevisionConflict) for r in results) == 1
            assert (await store.get("doc"))["revision"] == 2
            assert len(await store.history("doc")) == 2
            assert not await store.ack_delivery("doc", 1)
            assert await store.forget("doc", expected_revision=2) == 3
            assert await store.get("doc") is None
            assert not await store.ack_delivery("doc", 2)
            pending = await store.fetch_pending_deliveries()
            assert (
                len(pending) == 1
                and pending[0]["operation"] == "DELETE"
                and pending[0]["revision"] == 3
            )
            assert pending[0]["content"] == ""
            with pytest.raises(RevisionConflict):
                await peer.record("doc", expected_revision=3, **payload)
            status = await store.status()
            assert status == {"active": 0, "deleted": 1, "pending": 1}
        finally:
            await conn.close()
            await other.close()

    asyncio.run(exercise())
    pg.stop()
    pg.start_existing()

    async def verify():
        from herdr_engineering.control_plane.assistant_memory_store import (
            AssistantMemoryStore,
            RevisionConflict,
        )

        conn = await asyncpg.connect(pg.dsn)
        try:
            store = AssistantMemoryStore(conn, "tenant", "user", "assistant")
            assert await store.get("doc") is None
            assert len(await store.history("doc")) == 3
            assert not await store.ack_delivery("doc", 2)
            assert await store.ack_delivery("doc", 3)
            assert await store.fetch_pending_deliveries() == []
            with pytest.raises(RevisionConflict):
                await store.record(
                    "doc",
                    expected_revision=0,
                    content="resurrect",
                    provenance={},
                    capture_kind="fact",
                    approval_metadata={},
                )
        finally:
            await conn.close()

    asyncio.run(verify())


def test_atomic_rollback_and_bounds(pg):
    async def exercise():
        from herdr_engineering.control_plane.assistant_memory_store import AssistantMemoryStore

        conn = await asyncpg.connect(pg.dsn)
        try:
            store = AssistantMemoryStore(conn, "t2", "u", "a", max_content_bytes=10)
            payload = dict(
                content="abc", provenance={}, capture_kind="summary", approval_metadata={}
            )
            with pytest.raises(ValueError):
                await store.record(
                    "bounded", expected_revision=0, **{**payload, "content": "x" * 11}
                )
            with pytest.raises(ValueError):
                await store.record(
                    "bounded", expected_revision=0, **{**payload, "capture_kind": "invalid"}
                )
            async with conn.transaction():
                await conn.execute(
                    "ALTER TABLE pa_memory_delivery ADD CONSTRAINT reject_test CHECK (memory_id <> 'rollback')"
                )
            with pytest.raises(asyncpg.CheckViolationError):
                await store.record("rollback", expected_revision=0, **payload)
            assert await store.get("rollback") is None
            assert await store.history("rollback") == []
            await conn.execute("ALTER TABLE pa_memory_delivery DROP CONSTRAINT reject_test")
            await asyncio.gather(
                *(store.record(f"parallel{i}", expected_revision=0, **payload) for i in range(3))
            )
            assert len(await store.fetch_pending_deliveries(limit=2)) == 2
            for limit in (0, 1001):
                with pytest.raises(ValueError):
                    await store.fetch_pending_deliveries(limit=limit)
        finally:
            await conn.close()

    asyncio.run(exercise())


def test_scope_isolation_and_forget_rollback(pg):
    async def exercise():
        from herdr_engineering.control_plane.assistant_memory_store import (
            AssistantMemoryStore,
            RevisionConflict,
        )

        conn = await asyncpg.connect(pg.dsn)
        peer_conn = await asyncpg.connect(pg.dsn)
        try:
            store = AssistantMemoryStore(conn, "isolation", "u", "a")
            payload = dict(
                content="keep",
                provenance={"exact": ["a", 1]},
                capture_kind="summary",
                approval_metadata={"decision": "approved"},
            )
            await store.record("isolated", expected_revision=0, **payload)
            for scope in [
                ("other", "u", "a"),
                ("isolation", "other", "a"),
                ("isolation", "u", "other"),
            ]:
                peer = AssistantMemoryStore(peer_conn, *scope)
                assert await peer.get("isolated") is None
                assert await peer.history("isolated") == []
                assert await peer.fetch_pending_deliveries() == []
                assert not await peer.ack_delivery("isolated", 1)
                assert await peer.status() == {"active": 0, "deleted": 0, "pending": 0}
                with pytest.raises(RevisionConflict):
                    await peer.forget("isolated", expected_revision=1)
                with pytest.raises(RevisionConflict):
                    await peer.record("isolated", expected_revision=1, **payload)
            await conn.execute(
                "ALTER TABLE pa_memory_delivery ADD CONSTRAINT reject_delete CHECK (operation <> 'DELETE') NOT VALID"
            )
            try:
                with pytest.raises(asyncpg.CheckViolationError):
                    await store.forget("isolated", expected_revision=1)
                assert (await store.get("isolated"))["revision"] == 1
                assert len(await store.history("isolated")) == 1
                pending = await store.fetch_pending_deliveries()
                assert pending[0]["operation"] == "RETAIN"
                assert pending[0]["provenance"] == payload["provenance"]
                assert pending[0]["capture_kind"] == "summary"
            finally:
                await conn.execute("ALTER TABLE pa_memory_delivery DROP CONSTRAINT reject_delete")
            assert await store.ack_delivery("isolated", 1)
            assert not await store.ack_delivery("isolated", 1)
            await conn.execute(
                (
                    Path(__file__).parents[2]
                    / "deploy/control-plane/migrations/0015_assistant_memory_sync.sql"
                ).read_text()
            )
        finally:
            await conn.close()
            await peer_conn.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("race", ["create", "update_first", "forget_first"])
def test_competing_instances_have_one_cas_winner(pg, race):
    """Queue both real sessions on the document lock before allowing either write."""

    async def exercise():
        from herdr_engineering.control_plane.assistant_memory_store import (
            AssistantMemoryStore,
            RevisionConflict,
        )

        connections = [await asyncpg.connect(pg.dsn) for _ in range(3)]
        blocker, left_conn, right_conn = connections
        scope = ("race", race, "assistant")
        left = AssistantMemoryStore(left_conn, *scope)
        right = AssistantMemoryStore(right_conn, *scope)
        memory_id = f"race-{race}"
        payload = dict(
            content="original",
            provenance={"source": race},
            capture_kind="fact",
            approval_metadata={"actor": "server"},
        )
        tasks = []
        try:
            if race != "create":
                await left.record(memory_id, expected_revision=0, **payload)
            revision = 0 if race == "create" else 1
            await blocker.execute("SELECT pg_advisory_lock(hashtextextended($1, 0))", memory_id)

            async def wait_for_lock(conn):
                async def waiting():
                    while not await blocker.fetchval(
                        "SELECT EXISTS (SELECT FROM pg_locks WHERE pid=$1 AND locktype='advisory' AND NOT granted)",
                        conn.get_server_pid(),
                    ):
                        await asyncio.sleep(0.01)

                await asyncio.wait_for(waiting(), timeout=5)

            first = (
                left.forget(memory_id, expected_revision=revision)
                if race == "forget_first"
                else left.record(
                    memory_id, expected_revision=revision, **{**payload, "content": "winner"}
                )
            )
            tasks.append(asyncio.create_task(first))
            await wait_for_lock(left_conn)
            second = (
                right.forget(memory_id, expected_revision=revision)
                if race == "update_first"
                else right.record(
                    memory_id, expected_revision=revision, **{**payload, "content": "loser"}
                )
            )
            tasks.append(asyncio.create_task(second))
            await wait_for_lock(right_conn)
            await blocker.execute("SELECT pg_advisory_unlock(hashtextextended($1, 0))", memory_id)
            results = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True), timeout=5
            )
            assert not isinstance(results[0], BaseException)
            assert isinstance(results[1], RevisionConflict)
            assert sum(not isinstance(result, BaseException) for result in results) == 1
            final_revision = revision + 1
            deleted = race == "forget_first"
            operation = "DELETE" if deleted else "RETAIN"
            content = "" if deleted else "winner"
            canonical = await blocker.fetchrow(
                "SELECT * FROM pa_memory WHERE memory_id=$1", memory_id
            )
            assert canonical["revision"] == final_revision
            assert canonical["content"] == content
            assert (canonical["deleted_at"] is not None) == deleted
            assert (await left.get(memory_id) is None) == deleted
            history = await left.history(memory_id)
            assert [row["revision"] for row in history] == list(range(1, final_revision + 1))
            assert history[-1]["operation"] == operation
            assert history[-1]["content"] == content
            pending = await left.fetch_pending_deliveries()
            assert len(pending) == 1
            assert pending[0] == history[-1]
            assert await left.status() == {
                "active": int(not deleted),
                "deleted": int(deleted),
                "pending": 1,
            }
            if deleted:
                with pytest.raises(RevisionConflict):
                    await right.record(memory_id, expected_revision=final_revision, **payload)
            assert not await left.ack_delivery(memory_id, revision)
            assert len(await left.fetch_pending_deliveries()) == 1
        finally:
            await blocker.execute("SELECT pg_advisory_unlock_all()")
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for conn in connections:
                await conn.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("content", ["", " \t\n", "\u2003"])
def test_record_rejects_blank_content_without_writes(pg, content):
    async def exercise():
        from herdr_engineering.control_plane.assistant_memory_store import AssistantMemoryStore

        conn = await asyncpg.connect(pg.dsn)
        try:
            store = AssistantMemoryStore(conn, "blank", content or "empty", "assistant")
            memory_id = f"blank-record-{content.encode().hex()}"
            with pytest.raises(ValueError, match="nonblank"):
                await store.record(
                    memory_id,
                    expected_revision=0,
                    content=content,
                    provenance={},
                    capture_kind="fact",
                    approval_metadata={},
                )
            assert await store.get(memory_id) is None
            assert await store.history(memory_id) == []
            assert await store.status() == {"active": 0, "deleted": 0, "pending": 0}
        finally:
            await conn.close()

    asyncio.run(exercise())


def test_trusted_domains_share_project_without_replacing_actual_authors(pg):
    async def exercise():
        from herdr_engineering.control_plane.assistant_memory_store import (
            AssistantMemoryStore,
            RevisionConflict,
        )

        conn = await asyncpg.connect(pg.dsn)
        other = await asyncpg.connect(pg.dsn)
        try:
            first = AssistantMemoryStore(
                conn,
                "domain-tenant",
                "alice",
                "cli",
                domain_type="project",
                domain_id="project-one",
            )
            second = AssistantMemoryStore(
                other, "domain-tenant", "bob", "web", domain_type="project", domain_id="project-one"
            )
            payload = dict(
                content="project decision",
                provenance={},
                capture_kind="fact",
                approval_metadata={"actor": "gateway"},
            )
            created = await first.record("domain-project-note", expected_revision=0, **payload)
            assert (created["user_id"], created["assistant_id"]) == ("alice", "cli")
            assert (created["domain_type"], created["domain_id"]) == ("project", "project-one")
            assert (await second.get("domain-project-note"))["revision"] == 1
            updated = await second.record(
                "domain-project-note",
                expected_revision=1,
                **{**payload, "content": "shared update"},
            )
            assert updated["revision"] == 2
            assert (await first.get("domain-project-note"))["content"] == "shared update"
            history = await first.history("domain-project-note")
            assert [(r["user_id"], r["assistant_id"]) for r in history] == [
                ("alice", "cli"),
                ("bob", "web"),
            ]
            assert await second.history("domain-project-note") == history
            pending = await first.fetch_pending_deliveries()
            assert pending == await second.fetch_pending_deliveries()
            assert [(r["revision"], r["user_id"], r["assistant_id"]) for r in pending] == [
                (2, "bob", "web")
            ]
            delivery = await conn.fetchrow(
                "SELECT * FROM pa_memory_delivery WHERE memory_id=$1", "domain-project-note"
            )
            assert (delivery["user_id"], delivery["assistant_id"]) == ("bob", "web")
            assert await second.status() == {"active": 1, "deleted": 0, "pending": 1}
            for tenant, domain_type, domain_id in [
                ("domain-tenant", "project", "project-two"),
                ("other-tenant", "project", "project-one"),
                ("domain-tenant", "personal", "project-one"),
                ("domain-tenant", "assistant", None),
            ]:
                denied = AssistantMemoryStore(
                    other, tenant, "alice", "cli", domain_type=domain_type, domain_id=domain_id
                )
                assert await denied.get("domain-project-note") is None
                assert await denied.history("domain-project-note") == []
                assert await denied.fetch_pending_deliveries() == []
                assert not await denied.ack_delivery("domain-project-note", 2)
                assert await denied.status() == {"active": 0, "deleted": 0, "pending": 0}
                with pytest.raises(RevisionConflict):
                    await denied.record("domain-project-note", expected_revision=2, **payload)
                with pytest.raises(RevisionConflict):
                    await denied.forget("domain-project-note", expected_revision=2)
            assert not await first.ack_delivery("domain-project-note", 1)
            assert await second.forget("domain-project-note", expected_revision=2) == 3
            assert await first.get("domain-project-note") is None
            assert (await first.history("domain-project-note"))[-1]["operation"] == "DELETE"
            assert not await first.ack_delivery("domain-project-note", 2)
            assert await first.ack_delivery("domain-project-note", 3)
            with pytest.raises(RevisionConflict):
                await second.record("domain-project-note", expected_revision=3, **payload)

            personal = AssistantMemoryStore(
                conn,
                "domain-tenant",
                "alice",
                "cli",
                domain_type="personal",
                domain_id="alice-private",
            )
            personal_peer = AssistantMemoryStore(
                other,
                "domain-tenant",
                "alice",
                "web",
                domain_type="personal",
                domain_id="alice-private",
            )
            private_other = AssistantMemoryStore(
                other,
                "domain-tenant",
                "bob",
                "web",
                domain_type="personal",
                domain_id="bob-private",
            )
            await personal.record("domain-personal-note", expected_revision=0, **payload)
            assert (await personal_peer.get("domain-personal-note"))["revision"] == 1
            assert await private_other.get("domain-personal-note") is None
            assert await second.get("domain-personal-note") is None
            assert await private_other.status() == {"active": 0, "deleted": 0, "pending": 0}
            legacy = AssistantMemoryStore(conn, "domain-tenant", "alice", "cli")
            await legacy.record("domain-legacy-note", expected_revision=0, **payload)
            assert await first.get("domain-legacy-note") is None
            assert await personal_peer.get("domain-legacy-note") is None
        finally:
            await conn.close()
            await other.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "domain_type,domain_id",
    [("project", None), ("personal", ""), ("unknown", "x"), ("project", "x" * 513)],
)
def test_invalid_domain_configuration(pg, domain_type, domain_id):
    from herdr_engineering.control_plane.assistant_memory_store import AssistantMemoryStore

    with pytest.raises(ValueError, match="domain"):
        AssistantMemoryStore(None, "t", "u", "a", domain_type=domain_type, domain_id=domain_id)


def test_scoped_record_pages_kind_counts_and_exact_pending(pg):
    async def exercise():
        from herdr_engineering.control_plane.assistant_memory_store import AssistantMemoryStore

        conn = await asyncpg.connect(pg.dsn)
        other = await asyncpg.connect(pg.dsn)
        try:
            alice = AssistantMemoryStore(
                conn, "read-seams", "alice", "cli", domain_type="project", domain_id="shared"
            )
            bob = AssistantMemoryStore(
                other, "read-seams", "bob", "web", domain_type="project", domain_id="shared"
            )
            payload = dict(
                content="exact content",
                provenance={"source": "test"},
                approval_metadata={"actor": "server"},
            )
            for store, name, kind in [
                (alice, "seam-c", "fact"),
                (bob, "seam-a", "summary"),
                (alice, "seam-b", "fact"),
                (bob, "seam-deleted", "summary"),
            ]:
                await store.record(name, expected_revision=0, capture_kind=kind, **payload)
            assert await alice.is_pending("seam-a", 1)
            assert await bob.is_pending("seam-b", 1)
            assert not await bob.is_pending("seam-b", 0)
            assert not await bob.is_pending("absent", 1)
            await bob.record("seam-b", expected_revision=1, capture_kind="fact", **payload)
            assert not await alice.is_pending("seam-b", 1)
            assert await alice.is_pending("seam-b", 2)
            await alice.forget("seam-deleted", expected_revision=1)
            assert not await bob.is_pending("seam-deleted", 1)
            assert await bob.is_pending("seam-deleted", 2)
            assert await alice.status() == {"active": 3, "deleted": 1, "pending": 4}
            assert await bob.status(capture_kind="summary") == {
                "active": 1,
                "deleted": 1,
                "pending": 2,
            }
            assert await alice.status(capture_kind="fact") == {
                "active": 2,
                "deleted": 0,
                "pending": 2,
            }
            rows = await bob.list_records()
            assert [r["memory_id"] for r in rows] == ["seam-a", "seam-b", "seam-c"]
            assert rows == await alice.list_records()
            assert rows[0]["provenance"] == payload["provenance"]
            assert rows[0]["approval_metadata"] == payload["approval_metadata"]
            assert [(r["user_id"], r["assistant_id"]) for r in rows] == [
                ("bob", "web"),
                ("alice", "cli"),
                ("alice", "cli"),
            ]
            assert [r["memory_id"] for r in await alice.list_records(limit=1, offset=1)] == [
                "seam-b"
            ]
            assert [
                r["memory_id"]
                for r in await bob.list_records(capture_kind="fact", limit=1, offset=1)
            ] == ["seam-c"]
            assert [r["memory_id"] for r in await bob.list_records(capture_kind="summary")] == [
                "seam-a"
            ]
            assert await bob.list_records(limit=1000, offset=100000) == []
            for tenant, kind, domain in [
                ("read-seams", "project", "other"),
                ("other", "project", "shared"),
                ("read-seams", "personal", "shared"),
                ("read-seams", "assistant", None),
            ]:
                denied = AssistantMemoryStore(
                    other, tenant, "alice", "cli", domain_type=kind, domain_id=domain
                )
                assert await denied.list_records() == []
                assert not await denied.is_pending("seam-b", 2)
                assert await denied.status(capture_kind="fact") == {
                    "active": 0,
                    "deleted": 0,
                    "pending": 0,
                }
            assert await bob.ack_delivery("seam-a", 1)
            assert not await alice.is_pending("seam-a", 1)
            assert await alice.status(capture_kind="summary") == {
                "active": 1,
                "deleted": 1,
                "pending": 1,
            }
            for kwargs in [
                {"limit": 0},
                {"limit": 1001},
                {"limit": True},
                {"offset": -1},
                {"offset": 100001},
                {"offset": True},
                {"capture_kind": "invalid"},
            ]:
                with pytest.raises(ValueError):
                    await alice.list_records(**kwargs)
            with pytest.raises(ValueError):
                await alice.status(capture_kind="invalid")
        finally:
            await conn.close()
            await other.close()

    asyncio.run(exercise())


def test_exact_pending_is_not_limited_to_first_delivery_page(pg):
    async def exercise():
        from herdr_engineering.control_plane.assistant_memory_store import AssistantMemoryStore

        conn = await asyncpg.connect(pg.dsn)
        try:
            store = AssistantMemoryStore(conn, "pending-pages", "user", "assistant")
            payload = dict(
                content="synthetic", provenance={}, capture_kind="fact", approval_metadata={}
            )
            for i in range(1001):
                await store.record(f"pending-page-{i:04}", expected_revision=0, **payload)
            assert len(await store.list_records()) == 50
            assert len(await store.list_records(limit=1000)) == 1000
            assert [r["memory_id"] for r in await store.list_records(offset=1000)] == [
                "pending-page-1000"
            ]
            page = await store.fetch_pending_deliveries(limit=1000)
            assert len(page) == 1000
            assert "pending-page-1000" not in {row["memory_id"] for row in page}
            assert await store.is_pending("pending-page-1000", 1)
            assert await store.ack_delivery("pending-page-1000", 1)
            assert not await store.is_pending("pending-page-1000", 1)
        finally:
            await conn.close()

    asyncio.run(exercise())
