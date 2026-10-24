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
_MIGRATION = (Path(__file__).parents[1] / "deploy" / "control-plane" / "migrations"
              / "0006_github_catalog.sql")


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
    def __init__(self):
        self.shared = {}

    def make(self, *, identity="tok-a", viewer="alice", now=None):
        return MemoryGitHubStore(identity=identity, viewer=viewer, now=now or Clock(),
                                 shared=self.shared)

    def close(self):
        pass


class _PgBackend:
    def __init__(self, *, migrate=True):
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
    assert names[-1] == "0006_github_catalog.sql"
    assert "CONCURRENTLY" not in _MIGRATION.read_text()


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


def test_changes_since_zero_is_the_full_list(backend):
    store = backend.make()
    store.upsert_repositories([repo("acme/app")], scope=None)
    store.upsert_issues(A, [issue("I1", 1)], watermark="w")
    delta = store.changes_since(0)
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
