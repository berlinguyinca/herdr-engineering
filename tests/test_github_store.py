"""GitHub catalog store: persistence, token tag, revisions, tombstones (Task G1).

Every behaviour test runs against the in-memory store (always) and against a
real Postgres (only when ``HERDR_E2E_PG_DSN`` points at a throwaway database,
the same opt-in the live e2e suite uses). The Postgres tests isolate
themselves in a fresh schema per test and never touch ``public``.
"""
from __future__ import annotations

import os
import threading
import uuid
from pathlib import Path

import pytest

from herdr_engineering.control_plane.github_store import (
    TOMBSTONE_RETENTION,
    GitHubStore,
    GitHubStoreIdentityMismatch,
    GitHubStoreUnavailable,
    MemoryGitHubStore,
    open_github_store,
)

_PG_DSN = os.environ.get("HERDR_E2E_PG_DSN")
_MIGRATIONS_DIR = Path(__file__).parents[1] / "deploy" / "control-plane" / "migrations"
_MIGRATION = _MIGRATIONS_DIR / "0006_github_catalog.sql"
_DETAIL_MIGRATION = _MIGRATIONS_DIR / "0007_github_issue_details.sql"


class Clock:
    def __init__(self, value=1_800_000_000.0):
        self.value = value

    def __call__(self):
        return self.value


def repo(name, **extra):
    owner, short = name.split("/")
    return {"repository_id": f"github.com/{owner}/{short}", "name_with_owner": name,
            "open_issue_count": extra.pop("open", 1), **extra}


def issue(gid, number, updated="2026-10-24T00:00:00Z", **extra):
    return {"github_id": gid, "number": number, "title": f"issue {number}",
            "updated_at": updated, **extra}


A = "github.com/acme/app"
B = "github.com/acme/lib"
C = "github.com/other/tool"


# ---------------------------------------------------------------- backends


class _MemoryBackend:
    name = "memory"

    def __init__(self):
        self.shared = {}

    def restart(self):
        """A new process: the in-memory catalog is gone."""
        self.shared = {}

    def make(self, *, identity="tok-a", viewer="alice", now=None):
        return MemoryGitHubStore(identity=identity, viewer=viewer, now=now or Clock(),
                                 shared=self.shared)

    def close(self):
        pass


class _PgBackend:
    name = "postgres"

    def restart(self):
        """A new process: the database keeps the catalog."""

    def __init__(self, *, migrate=True, details=True):
        import asyncpg

        from herdr_engineering.control_plane.bridge import _LoopRunner
        self._asyncpg = asyncpg
        self._run = _LoopRunner().run
        self.schema = f"ghstore_{uuid.uuid4().hex[:12]}"
        self._stores = []
        conn = self._run(asyncpg.connect(_PG_DSN))
        try:
            self._run(conn.execute(f"CREATE SCHEMA {self.schema}"))
            if migrate:
                self._run(conn.execute(f"SET search_path TO {self.schema}"))
                self._run(conn.execute(_MIGRATION.read_text(encoding="utf-8")))
                if details:
                    self._run(conn.execute(_DETAIL_MIGRATION.read_text(encoding="utf-8")))
        finally:
            self._run(conn.close())

    def factory(self):
        return self._asyncpg.connect(_PG_DSN, server_settings={"search_path": self.schema})

    def make(self, *, identity="tok-a", viewer="alice", now=None):
        store = GitHubStore(self.factory, identity=identity, viewer=viewer,
                            now=now or Clock(), run=self._run)
        self._stores.append(store)
        return store

    def close(self):
        for store in self._stores:
            store.close()
        conn = self._run(self._asyncpg.connect(_PG_DSN))
        try:
            self._run(conn.execute(f"DROP SCHEMA IF EXISTS {self.schema} CASCADE"))
        finally:
            self._run(conn.close())


_needs_pg = pytest.mark.skipif(
    not _PG_DSN, reason="set HERDR_E2E_PG_DSN to a throwaway Postgres to run the store against it")


@pytest.fixture(params=["memory", pytest.param("postgres", marks=_needs_pg)])
def backend(request):
    made = _MemoryBackend() if request.param == "memory" else _PgBackend()
    yield made
    made.close()


# ---------------------------------------------------------------- tests


def test_migration_file_follows_numbering():
    names = sorted(p.name for p in _MIGRATION.parent.glob("*.sql"))
    assert names[-2:] == ["0006_github_catalog.sql", "0007_github_issue_details.sql"]
    assert "CONCURRENTLY" not in _MIGRATION.read_text()
    assert "CONCURRENTLY" not in _DETAIL_MIGRATION.read_text()


def test_load_is_none_before_anything_is_stored(backend):
    assert backend.make().load() is None


def test_round_trip_load(backend):
    store = backend.make()
    store.upsert_repositories([repo("acme/app"), repo("acme/lib")], scope=None)
    store.upsert_issues(A, [issue("I1", 1), issue("I2", 2)], watermark="2026-10-24T00:00:00Z")
    snap = backend.make().load()
    assert snap is not None
    assert snap.identity == "tok-a" and snap.viewer == "alice"
    assert set(snap.repositories) == {A, B}
    assert snap.repositories[A]["name_with_owner"] == "acme/app"
    assert set(snap.issues[A]) == {"I1", "I2"}
    assert snap.issues[A]["I2"]["number"] == 2
    assert snap.watermarks == {A: "2026-10-24T00:00:00Z"}
    assert snap.revision == store.revision()
    assert backend.make().watermark(A) == "2026-10-24T00:00:00Z"
    assert backend.make().watermark(B) is None


def test_load_after_identity_change_purges_everything(backend):
    old = backend.make(identity="tok-a")
    old.upsert_repositories([repo("acme/app")], scope=None)
    old.upsert_issues(A, [issue("I1", 1)], watermark="w")
    old.remove_issues(A, ["I1"])  # leaves a tombstone naming a private repo
    before = old.revision()

    new = backend.make(identity="tok-b")
    assert new.load() is None
    # Nothing of the old catalog is readable, tombstones included.
    assert new.watermark(A) is None
    after = new.revision()
    assert after > before  # monotonic across the purge, never reset
    delta = new.changes_since(0)
    assert delta["resync"] is True and delta["upserted"] == [] and delta["removed"] == []
    assert new.changes_since(before)["resync"] is True
    # The new identity now owns the (empty) catalog.
    assert backend.make(identity="tok-b").load() is not None
    assert backend.make(identity="tok-a").load() is None  # and back again purges


def test_load_after_viewer_change_purges(backend):
    backend.make(viewer="alice").upsert_repositories([repo("acme/app")], scope=None)
    assert backend.make(viewer="bob").load() is None
    assert backend.make(viewer="bob").load().repositories == {}


def test_unknown_viewer_trusts_the_fingerprint(backend):
    backend.make(viewer="alice").upsert_repositories([repo("acme/app")], scope=None)
    snap = backend.make(viewer=None).load()
    assert snap is not None and set(snap.repositories) == {A}
    assert snap.viewer == "alice"


def test_stale_identity_cannot_write_into_another_catalog(backend):
    old = backend.make(identity="tok-a")
    old.upsert_repositories([repo("acme/app")], scope=None)
    new = backend.make(identity="tok-b")
    assert new.load() is None
    with pytest.raises(GitHubStoreIdentityMismatch):
        old.upsert_repositories([repo("acme/lib")], scope=None)
    with pytest.raises(GitHubStoreIdentityMismatch):
        old.upsert_issues(A, [issue("I1", 1)], watermark="w")
    assert backend.make(identity="tok-b").load().repositories == {}


def test_revision_strictly_increases_per_change(backend):
    store = backend.make()
    revs = [store.revision()]
    store.upsert_repositories([repo("acme/app")], scope=None)
    revs.append(store.revision())
    store.upsert_issues(A, [issue("I1", 1)], watermark="w1")
    revs.append(store.revision())
    store.upsert_issues(A, [issue("I1", 1, title="renamed")], watermark="w2")
    revs.append(store.revision())
    store.remove_issues(A, ["I1"])
    revs.append(store.revision())
    assert revs == sorted(set(revs)), revs


def test_identical_upsert_does_not_bump_revision(backend):
    store = backend.make()
    store.upsert_repositories([repo("acme/app")], scope=None)
    store.upsert_issues(A, [issue("I1", 1)], watermark="w1")
    rev = store.revision()
    store.upsert_repositories([repo("acme/app")], scope=None)
    store.upsert_issues(A, [issue("I1", 1)], watermark="w2")  # overlap re-read
    assert store.revision() == rev
    assert store.changes_since(rev) == {"revision": rev, "resync": False, "upserted": [], "removed": []}
    assert store.watermark(A) == "w2"  # watermark still advances
    store.remove_issues(A, ["missing"])
    assert store.revision() == rev


def test_changes_since_returns_newer_rows_and_tombstones(backend):
    store = backend.make()
    store.upsert_repositories([repo("acme/app")], scope=None)
    store.upsert_issues(A, [issue("I1", 1), issue("I2", 2)], watermark="w")
    mark = store.revision()
    store.upsert_issues(A, [issue("I2", 2, title="changed")], watermark="w")
    store.remove_issues(A, ["I1"])
    delta = store.changes_since(mark)
    assert delta["resync"] is False and delta["revision"] == store.revision()
    assert [(row["kind"], row["key"]) for row in delta["upserted"]] == [("issue", "I2")]
    assert delta["upserted"][0]["data"]["title"] == "changed"
    assert delta["upserted"][0]["repository_id"] == A
    assert all(row["rev"] > mark for row in delta["upserted"] + delta["removed"])
    assert [(row["kind"], row["key"], row["repository_id"]) for row in delta["removed"]] == [("issue", "I1", A)]
    assert store.changes_since(store.revision())["upserted"] == []


def test_changes_since_the_starting_revision_is_the_full_list(backend):
    store = backend.make()
    start = store.revision()  # 0 in Postgres; the process base in memory
    store.upsert_repositories([repo("acme/app")], scope=None)
    store.upsert_issues(A, [issue("I1", 1)], watermark="w")
    delta = store.changes_since(start)
    assert delta["resync"] is False
    assert {(row["kind"], row["key"]) for row in delta["upserted"]} == {("repository", A), ("issue", "I1")}


def test_readding_a_removed_row_clears_its_tombstone(backend):
    store = backend.make()
    store.upsert_repositories([repo("acme/app")], scope=None)
    store.upsert_issues(A, [issue("I1", 1)], watermark="w")
    mark = store.revision()
    store.remove_issues(A, ["I1"])
    store.upsert_issues(A, [issue("I1", 1)], watermark="w")
    delta = store.changes_since(mark)
    assert [row["key"] for row in delta["upserted"]] == ["I1"]
    assert delta["removed"] == []


def test_since_older_than_retained_tombstones_resyncs(backend):
    clock = Clock()
    store = backend.make(now=clock)
    store.upsert_repositories([repo("acme/app")], scope=None)
    store.upsert_issues(A, [issue("I1", 1), issue("I2", 2)], watermark="w")
    early = store.revision()
    store.remove_issues(A, ["I1"])
    clock.value += 3 * 86400
    store.remove_issues(A, ["I2"])
    middle = store.revision()
    clock.value += TOMBSTONE_RETENTION.total_seconds() - 86400  # first tombstone now expired
    delta = store.changes_since(early)
    assert delta["resync"] is True
    assert delta["removed"] == []
    assert {(row["kind"], row["key"]) for row in delta["upserted"]} == {("repository", A)}
    assert store.changes_since(middle)["resync"] is False
    # Every tombstone pruned: an old `since` must still resync (nothing retained to compare).
    clock.value += 30 * 86400
    assert store.changes_since(middle - 1)["resync"] is True
    assert store.changes_since(store.revision())["resync"] is False


def test_since_newer_than_current_revision_resyncs(backend):
    store = backend.make()
    store.upsert_repositories([repo("acme/app")], scope=None)
    assert store.changes_since(store.revision() + 10)["resync"] is True


def test_upsert_repositories_scope_removes_missing_repos_of_listed_owners(backend):
    store = backend.make()
    store.upsert_repositories([repo("acme/app"), repo("acme/lib"), repo("other/tool")], scope=None)
    store.upsert_issues(B, [issue("L1", 1)], watermark="w")
    mark = store.revision()
    # acme's listing is complete and lacks lib; other was not listed this pass.
    store.upsert_repositories([repo("acme/app")], scope={"ACME"})
    snap = store.load()
    assert set(snap.repositories) == {A, C}
    assert B not in snap.issues and store.watermark(B) is None
    removed = {(row["kind"], row["key"]) for row in store.changes_since(mark)["removed"]}
    assert removed == {("repository", B), ("issue", "L1")}
    # Empty scope removes nothing; None means every owner was listed completely.
    store.upsert_repositories([], scope=set())
    assert set(store.load().repositories) == {A, C}
    store.upsert_repositories([repo("acme/app")], scope=None)
    assert set(store.load().repositories) == {A}


def test_purge_owners_not_in_removes_rows_and_records_tombstones(backend):
    store = backend.make()
    store.upsert_repositories([repo("acme/app"), repo("other/tool")], scope=None)
    store.upsert_issues(C, [issue("T1", 1)], watermark="w")
    mark = store.revision()
    store.purge_owners_not_in({"Acme"})
    assert set(store.load().repositories) == {A}
    assert store.watermark(C) is None
    delta = store.changes_since(mark)
    assert {(row["kind"], row["key"]) for row in delta["removed"]} == {("repository", C), ("issue", "T1")}
    assert delta["resync"] is False
    rev = store.revision()
    store.purge_owners_not_in(set())  # empty allowlist means "all owners": never a wipe
    assert set(store.load().repositories) == {A} and store.revision() == rev


def test_upsert_issues_needs_a_known_repository(backend):
    store = backend.make()
    with pytest.raises(KeyError):
        store.upsert_issues(A, [issue("I1", 1)], watermark="w")


def test_remove_issues_is_scoped_to_the_repository(backend):
    store = backend.make()
    store.upsert_repositories([repo("acme/app"), repo("acme/lib")], scope=None)
    store.upsert_issues(A, [issue("X", 1)], watermark="w")
    store.upsert_issues(B, [issue("X", 1)], watermark="w")
    store.remove_issues(B, ["X"])
    snap = store.load()
    assert set(snap.issues[A]) == {"X"} and not snap.issues.get(B)


def test_advisory_lock_is_exclusive(backend):
    one, two = backend.make(), backend.make()
    with one.advisory_lock() as held:
        assert held is True
        with two.advisory_lock() as other:
            assert other is False
        seen = []
        worker = threading.Thread(target=lambda: seen.append(two.advisory_lock().__enter__()))
        worker.start()
        worker.join()
        assert seen == [False]
    with two.advisory_lock() as held_after:
        assert held_after is True


def test_memory_stores_in_one_process_exclude_each_other_by_default():
    one = MemoryGitHubStore(identity="a", viewer="v")
    two = MemoryGitHubStore(identity="a", viewer="v")
    with one.advisory_lock() as held, two.advisory_lock() as other:
        assert (held, other) == (True, False)


def test_open_github_store_without_dsn_is_in_memory():
    store = open_github_store(None, identity="fp", viewer=None)
    assert isinstance(store, MemoryGitHubStore)
    assert store.persisted is False
    assert store.load() is None


def test_store_never_holds_the_token():
    store = MemoryGitHubStore(identity="f" * 64, viewer="alice")
    assert "secret" not in repr(store)
    with pytest.raises(TypeError):
        MemoryGitHubStore(token="secret", identity="x", viewer="y")  # no token parameter


@_needs_pg
def test_missing_table_is_tolerated_during_a_roll():
    made = _PgBackend(migrate=False)
    try:
        store = made.make()
        assert store.load() is None
        assert store.watermark(A) is None
        with pytest.raises(GitHubStoreUnavailable):
            store.upsert_repositories([repo("acme/app")], scope=None)
        with pytest.raises(GitHubStoreUnavailable):
            store.changes_since(0)
    finally:
        made.close()


@_needs_pg
def test_postgres_store_reconnects_after_a_dropped_connection():
    made = _PgBackend()
    try:
        store = made.make()
        store.upsert_repositories([repo("acme/app")], scope=None)
        store._run(store._conn.close())  # simulate a dropped data connection
        store.upsert_repositories([repo("acme/lib")], scope=set())
        assert set(made.make().load().repositories) == {A, B}
    finally:
        made.close()


# ---------------------------------------------------------------- issue detail (G6)


def detail(gid, number, updated="2026-10-24T00:00:00Z"):
    return dict(issue(gid, number, updated), body=f"body of {number}", body_truncated=False)


def test_detail_round_trip_never_bumps_the_revision(backend):
    store = backend.make()
    store.upsert_repositories([repo("acme/app")], scope=None)
    store.upsert_issues(A, [issue("I1", 1)], watermark=None)
    revision = store.revision()
    assert store.detail(A, 1) is None
    store.put_detail(A, detail("I1", 1))
    assert backend.make().detail(A, 1)["body"] == "body of 1"
    store.put_detail(A, detail("I1", 1, updated="2026-10-25T00:00:00Z"))
    assert store.detail(A, 1)["updated_at"] == "2026-10-25T00:00:00Z"
    assert store.revision() == revision  # bodies are not part of the listed catalog
    assert store.changes_since(revision)["upserted"] == []


def test_detail_goes_with_its_issue_repository_and_owner(backend):
    store = backend.make()
    store.upsert_repositories([repo("acme/app"), repo("acme/lib"), repo("other/tool")], scope=None)
    for rid, gid in ((A, "I1"), (A, "I2"), (B, "I3"), (C, "I4")):
        store.upsert_issues(rid, [issue(gid, int(gid[1:]))], watermark=None)
        store.put_detail(rid, detail(gid, int(gid[1:])))
    store.remove_issues(A, ["I1"])
    assert store.detail(A, 1) is None and store.detail(A, 2) is not None
    store.upsert_repositories([repo("acme/app"), repo("other/tool")], scope=None)  # acme/lib is gone
    assert store.detail(B, 3) is None
    store.purge_owners_not_in({"acme"})
    assert store.detail(C, 4) is None and store.detail(A, 2) is not None


def test_detail_is_dropped_with_the_catalog_on_an_identity_change(backend):
    store = backend.make()
    store.upsert_repositories([repo("acme/app")], scope=None)
    store.upsert_issues(A, [issue("I1", 1)], watermark=None)
    store.put_detail(A, detail("I1", 1))
    other = backend.make(identity="tok-b")
    assert other.load() is None
    assert other.detail(A, 1) is None  # a private body never survives a token change
    with pytest.raises(GitHubStoreIdentityMismatch):
        store.detail(A, 1)


@_needs_pg
def test_missing_detail_table_degrades_without_breaking_the_catalog():
    made = _PgBackend(details=False)
    try:
        store = made.make()
        store.upsert_repositories([repo("acme/app")], scope=None)
        store.upsert_issues(A, [issue("I1", 1)], watermark=None)
        with pytest.raises(GitHubStoreUnavailable):
            store.put_detail(A, detail("I1", 1))
        with pytest.raises(GitHubStoreUnavailable):
            store.detail(A, 1)
        store.remove_issues(A, ["I1"])  # removal still works without the detail table
        assert made.make(identity="tok-b").load() is None  # and so does the identity purge
    finally:
        made.close()


# ---------------------------------------------------------------- review fixes


@_needs_pg
def test_reads_neither_lock_the_meta_row_nor_prune():
    made = _PgBackend()
    try:
        clock = Clock()
        store = made.make(now=clock)
        store.upsert_repositories([repo("acme/app")], scope=None)
        store.upsert_issues(A, [issue("I1", 1)], watermark="w")
        store.put_detail(A, detail("I1", 1))
        store.remove_issues(A, ["I1"])
        clock.value += TOMBSTONE_RETENTION.total_seconds() + 60
        blocker = made._run(made.factory())
        try:
            made._run(blocker.execute("BEGIN"))
            made._run(blocker.execute("SELECT 1 FROM github_catalog_meta FOR UPDATE"))
            seen = []
            reader = threading.Thread(target=lambda: seen.append(
                (store.revision(), store.changes_since(0)["resync"], store.watermark(A), store.detail(A, 1))))
            reader.start()
            reader.join(timeout=5)
            answered_under_lock = list(seen)
            made._run(blocker.execute("ROLLBACK"))
            reader.join(timeout=5)
            assert answered_under_lock and answered_under_lock[0][1] is True  # a writer held the meta row
        finally:
            made._run(blocker.close())
        check = made._run(made.factory())
        try:  # the expired tombstone is pruned by the next write, not by a read
            assert made._run(check.fetchval("SELECT count(*) FROM github_catalog_tombstones")) == 1
        finally:
            made._run(check.close())
        store.upsert_repositories([repo("acme/app"), repo("acme/lib")], scope=None)
        assert store.changes_since(0)["resync"] is True
    finally:
        made.close()


def test_unreachable_database_raises_from_load_instead_of_reading_as_purged():
    from herdr_engineering.control_plane.bridge import _LoopRunner

    async def refuse():
        raise ConnectionRefusedError("down")
    store = GitHubStore(refuse, identity="tok-a", viewer="alice", run=_LoopRunner().run)
    with pytest.raises(GitHubStoreUnavailable):
        store.load()


def _fail_removal(store):
    """Make the removal step inside a store operation fail (an injected database error)."""
    if isinstance(store, GitHubStore):
        async def boom(*args, **kwargs):
            raise RuntimeError("injected removal failure")
    else:
        def boom(*args, **kwargs):
            raise RuntimeError("injected removal failure")
    store._drop_issues = boom


def test_apply_repository_commits_row_issues_removals_and_watermark_together(backend):
    store = backend.make()
    store.upsert_repositories([repo("acme/app", synced={"open": 2})], scope=None)
    store.upsert_issues(A, [issue("I1", 1), issue("I2", 2)], watermark="w1")
    before = store.revision()
    store.apply_repository(repo("acme/app", synced={"open": 1}), [issue("I2", 2, title="edited")], ["I1"], "w2")
    snap = backend.make().load()
    assert set(snap.issues[A]) == {"I2"} and snap.issues[A]["I2"]["title"] == "edited"
    assert snap.watermarks[A] == "w2" and snap.repositories[A]["synced"] == {"open": 1}
    assert store.revision() == before + 1  # one commit, one revision
    delta = store.changes_since(before)
    assert [r["key"] for r in delta["removed"]] == ["I1"]
    assert {(r["kind"], r["key"]) for r in delta["upserted"]} == {("repository", A), ("issue", "I2")}
    store.apply_repository(repo("acme/app", synced={"open": 1}), [issue("I2", 2, title="edited")], [], "w3")
    assert store.revision() == before + 1 and store.watermark(A) == "w3"  # nothing visible changed
    store.apply_repository(repo("acme/new"), [issue("N1", 1)], [], "w")  # a repository not stored yet
    assert backend.make().load().issues["github.com/acme/new"]["N1"]["number"] == 1


def test_apply_repository_is_all_or_nothing(backend):
    store = backend.make()
    store.upsert_repositories([repo("acme/app", synced={"open": 2})], scope=None)
    store.upsert_issues(A, [issue("I1", 1), issue("I2", 2)], watermark="w1")
    before = store.revision()
    _fail_removal(store)
    with pytest.raises(RuntimeError):
        store.apply_repository(repo("acme/app", synced={"open": 1}), [issue("I2", 2, title="edited")], ["I1"], "w2")
    snap = backend.make().load()
    assert set(snap.issues[A]) == {"I1", "I2"} and snap.issues[A]["I2"]["title"] == "issue 2"
    assert snap.watermarks[A] == "w1" and snap.repositories[A]["synced"] == {"open": 2}
    assert backend.make().revision() == before


def test_retired_store_can_neither_purge_nor_claim(backend):
    old = backend.make(identity="tok-a")
    old.upsert_repositories([repo("acme/app")], scope=None)
    old.retire()
    new = backend.make(identity="tok-b")
    assert new.load() is None
    new.upsert_repositories([repo("other/tool")], scope=None)
    for call in (old.load, old.revision, lambda: old.changes_since(0),
                 lambda: old.upsert_repositories([repo("acme/lib")], scope=None)):
        with pytest.raises(GitHubStoreUnavailable):
            call()
    assert set(backend.make(identity="tok-b").load().repositories) == {C}
    with old.advisory_lock() as held:  # the pass that holds it can still release it
        assert held in (True, False)
    old.close()


def test_purge_drops_the_catalog_but_keeps_the_identity(backend):
    store = backend.make()
    store.upsert_repositories([repo("acme/app")], scope=None)
    store.upsert_issues(A, [issue("I1", 1), issue("I2", 2)], watermark="w")
    store.put_detail(A, detail("I1", 1))
    store.remove_issues(A, ["I2"])  # a tombstone naming a private repository
    before = store.revision()
    store.purge()
    snap = backend.make().load()
    assert snap is not None and snap.repositories == {} and snap.issues == {}
    assert store.revision() > before
    assert store.detail(A, 1) is None
    delta = store.changes_since(before)
    assert delta["resync"] is True and delta["upserted"] == [] and delta["removed"] == []
    assert store.changes_since(0)["resync"] is True
    other = backend.make(identity="tok-b")
    other.purge()  # never another credential's catalog
    assert backend.make().load() is not None


def test_store_connections_carry_connect_and_command_timeouts(monkeypatch):
    import asyncpg

    from herdr_engineering.control_plane import github_store
    seen = []

    async def fake_connect(dsn, **kwargs):
        seen.append(kwargs)
        raise ConnectionRefusedError("not a database")
    monkeypatch.setattr(asyncpg, "connect", fake_connect)
    for store in (github_store.catalog_store_factory("postgresql://x@127.0.0.1:1/db")("fp", None),
                  github_store.open_github_store("postgresql://x@127.0.0.1:1/db", identity="fp")):
        with pytest.raises(GitHubStoreUnavailable):
            store.revision()
    assert seen and all(kw.get("command_timeout") == github_store.STORE_COMMAND_TIMEOUT
                        and kw.get("timeout") == github_store.STORE_CONNECT_TIMEOUT for kw in seen)


@_needs_pg
def test_a_hung_query_times_out_as_unavailable_and_the_store_recovers():
    from herdr_engineering.control_plane.github_store import catalog_store_factory
    store = catalog_store_factory(_PG_DSN, command_timeout=0.3)("tok-a", "alice")
    try:
        with pytest.raises(GitHubStoreUnavailable):
            store._call(lambda conn: conn.execute("SELECT pg_sleep(3)"))
        assert store._call(lambda conn: conn.fetchval("SELECT 1")) == 1
    finally:
        store.close()


def test_purging_an_empty_catalog_does_not_move_the_revision(backend):
    store = backend.make()
    store.upsert_repositories([repo("acme/app")], scope=None)
    store.purge()
    revision = store.revision()
    store.purge()  # a revoked token is retried every minute: no new revision each time
    assert store.revision() == revision


def test_a_new_memory_catalog_starts_above_every_earlier_one():
    first = MemoryGitHubStore(identity="tok-a", shared={})
    for name in ("acme/app", "acme/lib", "acme/tool"):
        first.upsert_repositories([repo(name)], scope=None)
    second = MemoryGitHubStore(identity="tok-a", shared={})  # a restarted process
    assert second.revision() > first.revision()
    assert second.changes_since(first.revision())["resync"] is True


def _incremental():
    import importlib.util
    spec = importlib.util.spec_from_file_location("_github_incremental_fakes_store",
                                                  Path(__file__).with_name("test_github_incremental.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_restart_never_answers_an_old_since_with_a_silent_delta(backend):
    from herdr_engineering.github import GitHubMonitor
    fakes = _incremental()
    fake = fakes.seeded()

    def monitor():
        return GitHubMonitor(provider_factory=lambda: fake, organizations=[],
                             store=lambda identity, viewer=None: backend.make(identity=identity, viewer=viewer))
    before = monitor()
    before.refresh()
    since = before.revision()
    before.close()
    gid = next(iter(fake.repos["acme/app"]))
    fake.tick(600)
    fake.edit("acme/app", gid, title="changed while down")
    backend.restart()
    after = monitor()
    after.refresh()
    delta = after.changes(kind="issues", since=since, start=False)
    after.close()
    if backend.name == "memory":  # nothing survived the restart: the old since cannot be answered
        assert delta["resync"] is True and "changed while down" in {row["title"] for row in delta["issues"]}
    else:  # the stored catalog survived: a true delta
        assert delta["resync"] is False
        assert [row["title"] for row in delta["upserted"]] == ["changed while down"]


def test_a_stored_detail_carries_when_it_was_fetched(backend):
    clock = Clock()
    store = backend.make(now=clock)
    store.upsert_repositories([repo("acme/app")], scope=None)
    store.upsert_issues(A, [issue("I1", 1)], watermark="w")
    store.put_detail(A, detail("I1", 1))
    data, fetched_at = store.stored_detail(A, 1)
    assert data["body"] == "body of 1" and fetched_at == clock.value
    store.put_detail(A, detail("I1", 1), fetched_at=clock.value - 500)  # the monitor's own clock
    assert store.stored_detail(A, 1)[1] == clock.value - 500
    assert store.stored_detail(A, 2) is None
